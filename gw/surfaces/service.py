"""The one service layer behind every surface.MCP tools, REST routes, the web sheets and the CLI are thin wrappers over these
methods. Authorisation is by role here, not in the surfaces.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from gw.errors import Conflict, NotFound, Unprocessable
from gw.governance import core as gov
from gw.harness.registry import Registry
from gw.policies import store as pol
from gw.record import clock, repo
from gw.record import models as m
from gw.record.ids import new_id
from gw.record.store import Store
from gw.signals.readiness import score_instance
from gw.standards import gatemodels as gm
from gw.surfaces import webhooks as _webhooks  # noqa: F401  (registers the event listener)
from gw.surfaces.ops import Ops

VIEWER = {"viewer", "portfolio_lead", "project_manager", "sme", "admin", "sponsor", "project_director",
          "investment_committee", "site_lead"}


def _d(x: Any) -> dict[str, Any]:
    return dict(_j(x))


def _j(x: Any) -> Any:
    if hasattr(x, "model_dump"):
        return x.model_dump(mode="json")
    if isinstance(x, list):
        return [_j(i) for i in x]
    return x


class Service(Ops):
    """Service facade over one store. `user` is the resolved principal for role checks."""

    def __init__(self, store: Store, registry: Registry | None = None) -> None:
        self.store = store
        self.registry = registry or Registry(store)
        store.registry = self.registry  # engines reach connections (and test overrides) through the service's registry

    @classmethod
    def open(cls, path: str | Path = "gw.duckdb") -> Service:
        return cls(Store.open(path))

    # --- principals ----------------------------------------------------------------------

    def user(self, key: str | None) -> m.User:
        """Resolve a principal by short key, email or id; defaults to the first portfolio lead."""
        users = self.store.all(m.User)
        if key:
            for u in users:
                if key in (u.short, u.email, u.id, u.name):
                    return u
            raise NotFound(f"user {key} not found")
        lead = next((u for u in users if "portfolio_lead" in u.roles), None)
        if lead is None:
            raise NotFound("no users; run `gw init` or `gw demo`")
        return lead

    def _viewer(self, user: m.User) -> None:
        if not (set(user.roles) & VIEWER or any(r.startswith("sme:") for r in user.roles)):
            gov.require(user, VIEWER, "read")

    # --- lookups -----------------------------------------------------------------------

    def project(self, code: str) -> m.Project:
        p = self.store.find_one(m.Project, code=code) or self.store.find_one(m.Project, id=code)
        if p is None:
            raise NotFound(f"project {code} not found")
        return p

    def commitment(self, ref: str) -> m.Commitment:
        ref = ref.split("/")[0]
        c = self.store.find_one(m.Commitment, code=ref) or self.store.find_one(m.Commitment, id=ref)
        if c is None:
            raise NotFound(f"commitment {ref} not found")
        return c

    def gate_instance(self, project: str, gate: str | None = None) -> m.GateInstance:
        p = self.project(project)
        if gate is None:
            if not p.current_gate_instance_id:
                raise NotFound(f"{p.code}: no current gate")
            return self.store.get(m.GateInstance, p.current_gate_instance_id)
        gis = [g for g in self.store.find(m.GateInstance, project_id=p.id) if g.gate_id == gate]
        if not gis:
            raise NotFound(f"{p.code}/{gate}: no gate instance")
        return gis[-1]

    def policies(self) -> pol.Policies:
        return pol.current(self.store)

    # --- portfolio -------------------------------------------------------------------------

    def portfolio_list(self, user: m.User, zone: str | None = None, gate: str | None = None,
                       at_risk_only: bool = False) -> list[dict[str, Any]]:
        """gw.portfolio.list: projects with current gate, readiness, next milestone, signals."""
        self._viewer(user)
        submitted = float(self.policies().get("gates.submitted_counts_as", 0.5))
        fc = {f.target_id: f for f in self.store.find(m.ForecastRow, target="project_slip", order_by="created_at")}
        sig = {(s.signal_id, s.scope_id): s for s in self.store.latest_signals(scope="project")}
        users = {u.id: u for u in self.store.all(m.User)}
        out = []
        for p in self.store.find(m.Project, order_by="code"):
            if zone and p.zone != zone:
                continue
            gi = self.store.get(m.GateInstance, p.current_gate_instance_id) if p.current_gate_instance_id else None
            if gate and (gi is None or gi.gate_id != gate):
                continue
            score = score_instance(self.store, gi, submitted) if gi else None
            ms = [c for c in self.store.find(m.Commitment, project_id=p.id, kind="milestone")
                  if c.status not in ("accepted", "waived", "cancelled")]
            nxt = min(ms, key=lambda c: c.due_at) if ms else None
            days_to_review = (gi.planned_review_at.date() - clock.today()).days if gi else None
            at_risk = bool(score and gi and score.readiness < gi.threshold and days_to_review is not None
                           and days_to_review <= int(self.policies().get("signals.gate_horizon_days", 30)))
            if at_risk_only and not at_risk:
                continue
            owner = users.get(p.owner_user_id or "")
            out.append({
                "code": p.code, "name": p.name, "zone": p.zone, "country": p.country, "archetype": p.archetype,
                "gate_model": p.gate_model_id, "gate": gi.gate_id if gi else None, "gate_name": gi.name if gi else None,
                "readiness": round(score.readiness, 4) if score else None, "threshold": gi.threshold if gi else None,
                "recommendation": score.recommendation if score else None,
                "mandatory_missing": sum(1 for g in score.gaps if g["mandatory"]) if score else 0,
                "review_at": gi.planned_review_at.isoformat() if gi else None, "days_to_review": days_to_review,
                "next_milestone": nxt.title if nxt else None,
                "next_milestone_days": (nxt.due_at.date() - clock.today()).days if nxt else None,
                "owner": owner.name if owner else None, "at_risk": at_risk,
                "p_slip": round(fc[p.id].p, 2) if p.id in fc else None,
                "signals": {sid: s.value for (sid, scope_id), s in sig.items() if scope_id == p.id},
            })
        return out

    def project_get(self, user: m.User, code: str) -> dict[str, Any]:
        """gw.project.get: project, gate instances, packages, open commitments."""
        self._viewer(user)
        p = self.project(code)
        gis = sorted(self.store.find(m.GateInstance, project_id=p.id), key=lambda g: g.order)
        open_cs = [c for c in self.store.find(m.Commitment, project_id=p.id, order_by="due_at")
                   if c.status not in ("accepted", "waived", "cancelled")]
        return {"project": _j(p), "gate_instances": [{k: v for k, v in _j(g).items() if k != "deliverables"} for g in gis],
                "packages": _j(self.store.find(m.WorkPackage, project_id=p.id, order_by="code")),
                "open_commitments": [_j(c) for c in open_cs]}

    # --- gates -------------------------------------------------------------------------------

    def gate_score(self, user: m.User, project: str, gate: str | None = None) -> dict[str, Any]:
        """gw.gate.score: readiness, recommendation, gap list."""
        self._viewer(user)
        gi = self.gate_instance(project, gate)
        p_slip = {f.target_id: f.p for f in self.store.find(m.ForecastRow, target="slip", order_by="created_at")}
        s = score_instance(self.store, gi, float(self.policies().get("gates.submitted_counts_as", 0.5)), p_slip)
        return {"project": self.project(project).code, "gate": gi.gate_id, "name": gi.name, **s.as_dict(),
                "review_at": gi.planned_review_at.isoformat(), "decision": gi.decision}

    def gate_gaps(self, user: m.User, project: str, gate: str | None = None) -> dict[str, Any]:
        """gw.gate.gaps: gaps with reasons, levers and forecast."""
        from gw.agents.gatekeeper import LEVERS, lever_for

        res = self.gate_score(user, project, gate)
        for g in res["gaps"]:
            g["lever"] = lever_for(g)
            g["lever_label"] = LEVERS[g["lever"]]["label"]
        return {"project": res["project"], "gate": res["gate"], "gaps": res["gaps"]}

    def gate_assessment(self, project: str, gate: str | None = None) -> m.AgentOutput | None:
        gi = self.gate_instance(project, gate)
        outs = [o for o in self.store.find(m.AgentOutput, output_type="GateAssessment", order_by="-created_at")
                if o.payload.get("gate_instance_id") == gi.id]
        return outs[0] if outs else None

    def gate_pack(self, user: m.User, project: str, gate: str | None = None) -> dict[str, Any]:
        """gw.gate.pack: latest review pack draft."""
        gov.require(user, {"project_manager", "portfolio_lead"}, "gate.pack")
        gi = self.gate_instance(project, gate)
        outs = [o for o in self.store.find(m.AgentOutput, output_type="ReviewPackDraft", order_by="-created_at")
                if o.payload.get("gate_instance_id") == gi.id]
        if not outs:
            raise NotFound(f"{project}/{gi.gate_id}: no review pack drafted yet (inside {gi.lead_time_days} d of review)")
        ap = self.store.find_one(m.Approval, output_id=outs[0].id)
        return {"markdown": outs[0].payload["markdown"], "output_id": outs[0].id,
                "status": "issued" if ap and ap.decision in ("approved", "approved_edited") else "draft",
                "approval_id": ap.id if ap else None}

    def gate_decide(self, user: m.User, project: str, gate: str, decision: str, note: str = "") -> dict[str, Any]:
        """gw.gate.decide: a person stamps; pass opens the next gate, pass_with_actions creates actions."""
        gi = self.gate_instance(project, gate)
        repo.decide_gate(self.store, gi, user, decision, note)
        p = self.project(project)
        if decision in ("pass", "pass_with_actions"):
            model = gm.get(self.store, gi.gate_model_id, gi.gate_model_rev)
            if decision == "pass_with_actions":
                score = score_instance(self.store, gi)
                for g in [x for x in score.gaps if not x["mandatory"]]:
                    self._new_commitment(p, f"Action from {gi.name}: {g['name']}", f"action:{gi.gate_id}:{g['deliverable_id']}",
                                         m.CommitmentKind.action, p.owner_user_id or user.id,
                                         self._party_id_for(g), clock.now() + timedelta(days=gi.actions_due_days),
                                         actor=f"user:{user.id}")
            nxt = model.next_gate(gi.gate_id)
            latest = gm.get(self.store, gi.gate_model_id)
            if nxt is not None:
                nxt_latest = latest.gate(nxt.id) if any(x.id == nxt.id for x in latest.gates) else nxt
                override = self.policies().get(f"gates.readiness_threshold_override.{latest.id}/{nxt.id}")
                new = gm.instance_from_gate(p, latest, nxt_latest, clock.now() + timedelta(days=int(self.policies().get("gates.next_review_days", 120))), override)
                repo.create_gate_instance(self.store, new, actor=f"user:{user.id}")
                p.current_gate_instance_id = new.id
                p.gate_model_rev = latest.rev
                self.store.update(p, actor=f"user:{user.id}", kind="gate_advanced", payload={"gate": nxt.id})
        return _d(self.store.get(m.GateInstance, gi.id))

    def _party_id_for(self, gap: dict[str, Any]) -> str:
        if gap.get("commitment_id"):
            return self.store.get(m.Commitment, gap["commitment_id"]).party_id
        return self.store.find(m.Party, kind="internal_team", limit=1)[0].id

    def _new_commitment(self, p: m.Project, title: str, deliverable: str, kind: m.CommitmentKind, owner: str,
                        party: str, due: datetime, actor: str) -> m.Commitment:
        from gw.harness.ingest import next_commitment_code

        c = m.Commitment(id=new_id("Commitment"), code=next_commitment_code(self.store), project_id=p.id,
                         deliverable_id=deliverable, title=title, kind=kind, owner_user_id=owner, party_id=party,
                         due_at=due, original_due_at=due, created_at=clock.now())
        self.store.insert(c, actor=actor)
        return c

    # --- commitments and evidence ---------------------------------------------------------------

    def commitment_list(self, user: m.User, project: str | None = None, party: str | None = None,
                        status: str | None = None, due_within_days: int | None = None) -> list[dict[str, Any]]:
        self._viewer(user)
        where: dict[str, Any] = {}
        if project:
            where["project_id"] = self.project(project).id
        if status:
            where["status"] = status
        cs = self.store.find(m.Commitment, order_by="due_at", **where)
        if party:
            pt = self.store.find_one(m.Party, name=party) or self.store.find_one(m.Party, id=party)
            cs = [c for c in cs if pt and c.party_id == pt.id]
        if due_within_days is not None:
            lim = clock.now() + timedelta(days=due_within_days)
            cs = [c for c in cs if c.due_at <= lim and c.status in ("open", "flagged", "overdue", "confirmed")]
        return [_j(c) for c in cs]

    def commitment_set_status(self, user: m.User, commitment: str, status: str, new_due: str | None = None,
                              reason: str | None = None) -> dict[str, Any]:
        gov.require(user, {"project_manager", "portfolio_lead"}, "commitment.set_status")
        c = self.commitment(commitment)
        if status == "waived":
            return _d(repo.waive_commitment(self.store, c, user, reason or "waived"))
        nd = datetime.fromisoformat(new_due) if new_due else None
        if nd is not None and nd.tzinfo is None:
            nd = nd.replace(tzinfo=clock.now().tzinfo)
        return _d(repo.set_commitment_status(self.store, c, status, f"user:{user.id}", reason, nd))

    def evidence_link(self, user: m.User, commitment: str, uri: str, title: str | None = None,
                      kind: str | None = None, conn_id: str | None = None) -> dict[str, Any]:
        """gw.evidence.link: hash computed through the DocumentSource at link time; the body is never stored."""
        if not (set(user.roles) & {"project_manager", "portfolio_lead"} or any(r.startswith("sme:") for r in user.roles)):
            gov.require(user, {"project_manager", "sme"}, "evidence.link")
        c = self.commitment(commitment)
        sha = size = None
        if conn_id:
            from gw.harness.contracts import ExternalRef

            src = self.registry.get(conn_id)
            ref = ExternalRef(conn_id=conn_id, external_id=uri)
            sha, size, uri = src.hash(ref), src.stat(ref).size, src.link(ref)
        gi = self.store.get(m.GateInstance, c.gate_instance_id) if c.gate_instance_id else None
        ev_kind = kind or next((d.evidence for d in gi.deliverables if d.id == c.deliverable_id), "document") if gi else (kind or "link")
        ev = m.Evidence(id=new_id("Evidence"), commitment_id=c.id, kind=m.EvidenceKind(ev_kind), uri=uri,
                        title=title or uri.rsplit("/", 1)[-1], sha256=sha, size=size, received_at=clock.now(),
                        source_conn_id=conn_id)
        repo.link_evidence(self.store, c, ev, f"user:{user.id}")
        return _d(ev)

    def evidence_accept(self, user: m.User, evidence: str, accept: bool, reason: str | None = None) -> dict[str, Any]:
        ev = self.store.find_one(m.Evidence, id=evidence)
        if ev is None:
            c = self.commitment(evidence)
            if not c.evidence_id:
                raise NotFound(f"{c.code}: no evidence linked")
            ev = self.store.get(m.Evidence, c.evidence_id)
        return _d(repo.decide_evidence(self.store, ev, user, accept, reason))

    # --- policies and standards ---------------------------------------------------------------------

    def policy_get(self, user: m.User, section: str | None = None) -> dict[str, Any]:
        self._viewer(user)
        p = self.policies()
        return {"rev": p.rev, "policies": p.section(section) if section else p.data}

    def policy_set(self, user: m.User, yaml_text: str, note: str = "") -> dict[str, Any]:
        gov.require(user, {"portfolio_lead"}, "policy.set")
        rev = pol.set_policies(self.store, yaml_text, f"user:{user.id}", note)
        return {"rev": rev.rev, "diff": rev.diff}

    def standards_validate(self, text: str) -> dict[str, Any]:
        import yaml

        data = yaml.safe_load(text)
        rep = gm.validate(data if isinstance(data, dict) else {})
        return {"ok": rep.ok, "errors": [{"pointer": p, "message": msg} for p, msg in rep.errors], "warnings": rep.warnings}

    def standards_publish(self, user: m.User, text: str) -> dict[str, Any]:
        gov.require(user, {"portfolio_lead"}, "standards.publish")
        model = gm.publish(self.store, text, user.id)
        return {"id": model.id, "rev": model.rev}

    def gatemodels(self) -> list[dict[str, Any]]:
        used: dict[str, int] = {}
        for p in self.store.all(m.Project):
            used[p.gate_model_id] = used.get(p.gate_model_id, 0) + 1
        out = []
        for r in gm.list_models(self.store):
            model = gm.get(self.store, r.model_id, r.rev)
            out.append({"id": r.model_id, "name": model.name, "rev": r.rev, "gates": len(model.gates),
                        "used_by": used.get(r.model_id, 0), "published_at": r.published_at.isoformat(),
                        "published_by": r.published_by, "yaml": r.yaml_text, "model": model.model_dump()})
        return out

    # --- approvals and audit -----------------------------------------------------------------------

    def approval_list(self, user: m.User, mine: bool = False) -> list[dict[str, Any]]:
        self._viewer(user)
        aps = self.store.find(m.Approval, decision="pending", order_by="-requested_at")
        outs = {o.id: o for o in self.store.find(m.AgentOutput, id=[a.output_id for a in aps if a.output_id])} if aps else {}
        res = []
        for a in aps:
            o = outs.get(a.output_id or "")
            res.append({**_j(a), "output": _j(o) if o else None})
        return res

    def audit_query(self, user: m.User, entity: str | None = None, agent: str | None = None,
                    user_key: str | None = None, since_days: int | None = 7) -> dict[str, Any]:
        gov.require(user, {"admin", "portfolio_lead"}, "audit.query")
        if entity and not entity.startswith(("C-0", "P-", "G-", "AR-")):
            try:
                entity = self.commitment(entity).id
            except NotFound:
                with contextlib.suppress(NotFound):
                    entity = self.project(entity).id
        uid = self.user(user_key).id if user_key else None
        return gov.audit(self.store, entity, agent, uid, since_days)

    def readiness(self) -> dict[str, Any]:
        return gov.readiness_score(self.store, self.policies())

    # --- harness ---------------------------------------------------------------------------------------

    def harness_list(self, user: m.User) -> list[dict[str, Any]]:
        gov.require(user, {"admin", "portfolio_lead"}, "harness.list")
        return [_j(c) for c in self.registry.list()]

    def harness_test(self, user: m.User, connection: str) -> dict[str, Any]:
        gov.require(user, {"admin", "portfolio_lead"}, "harness.test")
        return _d(self.registry.test(connection))

    def harness_sync(self, user: m.User, connection: str | None = None) -> dict[str, Any]:
        gov.require(user, {"admin", "portfolio_lead"}, "harness.sync")
        if connection:
            return {connection: _j(self.registry.sync(connection))}
        return {k: _j(v) for k, v in self.registry.sync_all().items()}

    # --- agents --------------------------------------------------------------------------------------

    def run_gatekeeper(self, project: str | None = None, trigger: str = "manual") -> Any:
        from gw.agents.base import run_agent
        from gw.agents.gatekeeper import Gatekeeper

        return run_agent(self.store, Gatekeeper(project), trigger=trigger)

    def require_state(self, cond: bool, msg: str) -> None:
        if not cond:
            raise Conflict(msg)

    def unprocessable(self, msg: str, pointer: str = "") -> None:
        raise Unprocessable(msg, pointer)
