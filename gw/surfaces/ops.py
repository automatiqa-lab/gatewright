"""Service operations: chaser and outbox, approvals, semantic engine, signals,
heat maps, retrieval, scout, brief, reporter, forecast and the morning run.

Mixed into `gw.surfaces.service.Service`; every surface calls these through it.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gw.agents.base import RunResult, run_agent
from gw.errors import Conflict, GwError, NotAuthorised, NotFound, Unprocessable
from gw.governance import core as gov
from gw.record import clock, repo
from gw.record import models as m
from gw.record.ids import new_id
from gw.record.store import Store

if TYPE_CHECKING:
    from gw.harness.registry import Registry


def _j(x: Any) -> Any:
    return x.model_dump(mode="json") if hasattr(x, "model_dump") else x


def _d(x: Any) -> dict[str, Any]:
    return dict(_j(x))


_ASK_LOCK = threading.Lock()
_ASK_IN_FLIGHT: set[str] = set()


class Ops:
    """Operations mixin; expects `self.store`, `self.registry`, `self.user()`, `self.policies()`."""

    store: Store
    registry: Registry

    # --- chaser and outbox --------------------------------------------------------------

    def chase_plan(self) -> dict[str, Any]:
        """`gw chase --dry-run`: what the chaser would queue today, without writing anything."""
        from gw.agents.chaser import Chaser

        ch = Chaser()
        snap = ch.snapshot(self.store, self.policies())  # type: ignore[attr-defined]
        plans, overflow = ch.plan(snap)
        rows = [{"commitment": p.commitment.code, "party": p.party.name, "kind": str(p.party.kind), "level": p.level,
                 "planned_for": p.planned_for.isoformat(), "reason": p.reason} for p in plans]
        by_level: dict[str, int] = {}
        for r in rows:
            by_level[f"L{r['level']}"] = by_level.get(f"L{r['level']}", 0) + 1
        return {"today": rows, "by_level": by_level, "carried": len(overflow)}

    def run_chaser(self, trigger: str = "schedule", project: str | None = None) -> dict[str, Any]:
        from gw.agents.chaser import Chaser

        res = run_agent(self.store, Chaser(project), trigger=trigger)
        sent = self.dispatch_outbox()
        return {"run": res.run.id, "outcome": res.run.outcome, "summary": res.run.summary, "dispatched": sent}

    def commitment_chase(self, user: m.User, commitment: str, level: int | None = None) -> dict[str, Any]:
        """gw.commitment.chase: queue a chase or draft an escalation per the ladder."""
        from gw.agents.chaser import Chaser

        gov.require(user, {"project_manager", "portfolio_lead"}, "commitment.chase")
        c = self.commitment(commitment)  # type: ignore[attr-defined]
        if c.status not in ("open", "flagged", "overdue"):
            raise Conflict(f"{c.code}: status {c.status} is not chased", ["open", "flagged", "overdue"])
        res = run_agent(self.store, Chaser(only=c.code, force_level=level), trigger="manual")
        self.dispatch_outbox()
        return {"run": res.run.id, "outputs": [{"type": o.output_type, "disposition": o.disposition,
                                                 "level": o.payload.get("level")} for o in res.outputs],
                "approvals": [a.id for a in res.approvals]}

    def _chase_envelope(self, ch: m.Chase) -> dict[str, Any] | None:
        """The Art. 50 envelope of the output that drafted this chase's text, if a model wrote it."""
        if not ch.agent_run_id:
            return None
        for o in self.store.find(m.AgentOutput, agent_run_id=ch.agent_run_id):
            env = o.payload.get("ai_generated")
            if o.payload.get("gw_ref") == ch.gw_ref and env and "text" in env.get("scope", []):
                return dict(env)
        return None

    def dispatch_outbox(self, now: datetime | None = None) -> int:
        """Send queued chases whose slot has come, inside the party's window (hard limit 2 in the harness)."""
        from gw.agents.chaser import in_window

        now = now or clock.now()
        pol = self.policies()  # type: ignore[attr-defined]
        conns = [c for c in self.registry.list("MessageChannel") if "send_as" in c.scopes]
        cards = [c for c in self.registry.list("MessageChannel") if "post_cards" in c.scopes]
        if not conns and not cards:
            return 0
        sent = 0
        for ch in self.store.find(m.Chase, status="queued"):
            party = self.store.get(m.Party, ch.party_id)
            if ch.planned_for > now or not in_window(pol, party.tz, now):
                continue
            from gw.harness.contracts import OutboundMessage

            if ch.channel in ("teams", "slack") and cards:
                via = self.post_chase_card(ch, party, cards[0].id)
            elif conns:
                msg = OutboundMessage(to=ch.to, cc=ch.cc, subject=ch.subject, text=ch.text, gw_ref=ch.gw_ref,
                                      party_id=ch.party_id, approval_id=ch.approval_id,
                                      ai_generated=self._chase_envelope(ch))
                self.registry.get(conns[0].id).send(msg)
                via = conns[0].id
            else:
                continue
            ch.status = "sent"
            ch.sent_at = now
            self.store.update(ch, actor="agent:chaser", kind="sent", agent_run_id=ch.agent_run_id,
                              payload={"gw_ref": ch.gw_ref, "conn": via})
            sent += 1
        return sent

    def post_chase_card(self, ch: m.Chase, party: m.Party, conn_id: str) -> str:
        """A Teams / Slack chase card with CONFIRM and FLAG buttons.

        Hard limit 2 applies to cards as to mail: a sponsor or authority needs an approved Approval.
        """
        from gw.errors import Forbidden
        from gw.harness.contracts import ApprovalCard
        from gw.surfaces.webhooks import chase_card_token

        if party.kind in ("authority", "sponsor"):
            ap = self.store.find_one(m.Approval, id=ch.approval_id) if ch.approval_id else None
            if ap is None or ap.decision not in ("approved", "approved_edited"):
                raise Forbidden(f"{ch.gw_ref}: card to {party.kind} {party.name} needs an approved approval",
                                limit="approval_required")
        card = ApprovalCard(approval_id=ch.id, title=ch.subject, body=ch.text[:1500], actions=["CONFIRM", "FLAG"],
                            token=chase_card_token(ch.id))
        self.registry.get(conn_id).post_card(card)
        return conn_id

    def chase_card_reply(self, chase_id: str, token: str, action: str, new_date: str | None = None,
                         reason: str | None = None) -> dict[str, Any]:
        """A CONFIRM / FLAG button press takes the same path as a mail reply."""
        from gw.errors import Forbidden
        from gw.surfaces.webhooks import verify_chase_card_token

        if not verify_chase_card_token(chase_id, token):
            raise Forbidden("card token invalid", limit="card_token")
        ch = self.store.get(m.Chase, chase_id)
        act = action.strip().upper()
        if act == "CONFIRM":
            text = "CONFIRM"
        elif act == "FLAG":
            if not new_date:
                raise Unprocessable("FLAG needs a new date (YYYY-MM-DD)", "/new_date")
            text = f"FLAG - {new_date[:10]} {reason or ''}".strip()
        else:
            raise Unprocessable("action must be CONFIRM or FLAG", "/action")
        party = self.store.get(m.Party, ch.party_id)
        item = m.InboundItem(id=new_id("InboundItem"), conn_id="card", external_id=f"card-{chase_id}-{act}-{clock.now().isoformat()}",
                             kind="message", received_at=clock.now(),
                             payload={"from": party.contacts[0].email if party.contacts else "", "subject": f"card {ch.gw_ref}",
                                      "text": text, "gw_ref": ch.gw_ref, "thread_id": None, "channel": "card"})
        self.store.insert(item, actor="system:card")
        res = self.process_inbound(trigger="event")
        it = self.store.get(m.InboundItem, item.id)
        return {"chase": chase_id, "action": act, "processed": res,
                "classification": {k: (it.classification or {}).get(k) for k in ("label", "confidence", "commitment_id")},
                "review": it.review_state == "pending"}

    def chase_status(self, user: m.User, project: str | None = None, party: str | None = None) -> dict[str, Any]:
        """gw.chase.status: sent / confirmed / flagged / silent and next steps."""
        silent_after = int((self.policies().thresholds("silence_days") or (3, 5))[0])  # type: ignore[attr-defined]
        from gw.signals.calendars import business_days_between

        self._viewer(user)  # type: ignore[attr-defined]
        cs = [c for c in self.store.all(m.Commitment) if c.status in ("open", "flagged", "overdue", "confirmed")]
        if project:
            pid = self.project(project).id  # type: ignore[attr-defined]
            cs = [c for c in cs if c.project_id == pid]
        parties = {p.id: p for p in self.store.all(m.Party)}
        if party:
            pt = next((p for p in parties.values() if p.name == party or p.id == party), None)
            cs = [c for c in cs if pt and c.party_id == pt.id]
        chases: dict[str, list[m.Chase]] = {}
        for ch in self.store.all(m.Chase):
            chases.setdefault(ch.commitment_id, []).append(ch)
        now = clock.now()
        rows: list[dict[str, Any]] = []
        for c in sorted(cs, key=lambda x: x.due_at):
            if not chases.get(c.id):
                continue
            pt = parties[c.party_id]
            last = max(chases[c.id], key=lambda x: x.planned_for)
            silence = business_days_between(c.first_unanswered_chase_at, now, pt.country) if c.first_unanswered_chase_at else 0
            tag = ("flagged" if c.status == "flagged" else "confirmed" if c.status == "confirmed" else
                   "silent" if silence >= silent_after else "awaiting")
            rows.append({"commitment": c.code, "commitment_id": c.id, "title": c.title, "party": pt.name,
                         "party_kind": str(pt.kind), "due": c.due_at.isoformat(), "status": c.status,
                         "escalation_level": c.escalation_level, "silence_days": silence, "tag": tag,
                         "steps": [{"level": x.level, "status": x.status, "at": (x.sent_at or x.planned_for).isoformat(),
                                    "reply": x.reply_intent} for x in sorted(chases[c.id], key=lambda x: x.planned_for)],
                         "last": {"status": last.status, "level": last.level, "gw_ref": last.gw_ref}})
        summary: dict[str, int] = {}
        for r in rows:
            summary[r["tag"]] = summary.get(r["tag"], 0) + 1
        return {"summary": summary, "commitments": rows}

    # --- approvals ----------------------------------------------------------------------------

    def approval_decide(self, user: m.User, approval: str, decision: str, edited_output: str | None = None) -> dict[str, Any]:
        """gw.approval.decide: approve (applies the drafted output), approve edited, reject, or hold."""
        ap = self.store.get(m.Approval, approval)
        if decision == "hold":
            return _d(ap)
        if ap.decision != "pending":
            raise Conflict(f"{ap.id} already {ap.decision}", [])
        roles = {"escalation_l3": {"project_manager", "portfolio_lead"}, "gate_pack": {"project_manager", "portfolio_lead"},
                 "send_external": {"project_manager", "portfolio_lead"}, "dependency": {"project_manager", "portfolio_lead"},
                 "review_queue_item": {"project_manager", "portfolio_lead"}, "policy_change": {"portfolio_lead"}}
        gov.require(user, roles.get(str(ap.kind), {"portfolio_lead"}), f"approval {ap.kind}")
        if decision == "approved" and edited_output:
            decision = "approved_edited"
        if decision not in ("approved", "approved_edited", "rejected"):
            raise Unprocessable("decision must be approved, approved_edited, rejected or hold", "/decision")
        out = self.store.get(m.AgentOutput, ap.output_id) if ap.output_id else None
        if out is not None and decision == "approved_edited" and edited_output:
            from gw.governance import disclosure

            payload = {**out.payload, "text": edited_output, "edited_by": user.id}
            if disclosure.marked(payload):  # the person rewrote it: editorial carve-out, no AI label
                env = payload.pop("ai_generated")
                rest = [s for s in env.get("scope", []) if s != "text"]
                payload = disclosure.mark(payload, disclosure.envelope(rest, len(rest), review_state="accepted"))
            out.payload = payload
            self.store.update(out, actor=f"user:{user.id}", kind="edited")
            ap.edited_output_ref = out.id
        elif out is not None and decision == "approved":
            from gw.governance import disclosure

            if disclosure.marked(out.payload):
                out.payload = disclosure.set_review_state(out.payload, "accepted")
                self.store.update(out, actor=f"user:{user.id}", kind="accepted")
        ap.decision = m.ApprovalDecision(decision)
        ap.decided_by_user_id = user.id
        ap.decided_at = clock.now()
        self.store.update(ap, actor=f"user:{user.id}", kind="decided", payload={"decision": decision})
        if out is not None and decision in ("approved", "approved_edited"):
            self._release(user, ap, out)
        elif out is not None and str(ap.kind) in ("escalation_l3", "send_external"):
            for ch in self.store.find(m.Chase, approval_id=ap.id, status="drafted"):
                ch.status = "rejected"
                self.store.update(ch, actor=f"user:{user.id}", kind="rejected")
        return _d(ap)

    def _release(self, user: m.User, ap: m.Approval, out: m.AgentOutput) -> None:
        """Apply an approved draft: queue and send the escalation, issue the pack."""
        if str(ap.kind) in ("escalation_l3", "send_external"):
            from gw.agents.chaser import queue_chase

            run = self.store.get(m.AgentRun, ap.agent_run_id)
            for ch in self.store.find(m.Chase, approval_id=ap.id, status="drafted"):
                ch.status = "superseded"
                self.store.update(ch, actor=f"user:{user.id}", kind="approved")
            pl = dict(out.payload)
            pl["planned_for"] = clock.now().isoformat()
            out.payload = pl
            queue_chase(self.store, run, out, approval_id=ap.id)
            self.dispatch_outbox()
        elif str(ap.kind) == "gate_pack":
            self.store.log(f"user:{user.id}", "AgentOutput", out.id, "issued", {"approval": ap.id})
        elif str(ap.kind) == "policy_change" and out.output_type == "PolicyChangeProposal":
            from gw.governance.promotion import raise_autonomy_yaml
            from gw.policies.store import current_revision, set_policies

            pl = out.payload
            text = raise_autonomy_yaml(current_revision(self.store).yaml_text, pl["agent"], pl["to"])
            set_policies(self.store, text, f"user:{user.id}",
                         note=f"promotion approved: {pl['agent']} {pl['from']} -> {pl['to']} ({ap.id})")

    # --- semantic engine ------------------------------------------------------------------------

    def _pipeline(self) -> Any:
        from gw.engines.semantic.classifier import load_current
        from gw.engines.semantic.pipeline import Linker, Pipeline

        heads = load_current(self.store)
        if heads is None:
            self.semantic_retrain()
            heads = load_current(self.store)
        linker = Linker(self.store.all(m.Commitment), self.store.all(m.Party), self.store.all(m.Project))
        pol = self.policies()  # type: ignore[attr-defined]
        from gw.engines.semantic.taxonomy import current as current_taxonomy

        return Pipeline(heads, linker, clock.today(), float(pol.get("semantic.auto_apply", 0.8)), llm=self._llm(),
                        taxonomy=current_taxonomy(self.store))

    def _llm(self) -> Any:
        """The extraction LLM through the gateway (redactor attached; hard limit 3)."""
        from gw.engines.models import ModelGateway, Redactor

        for conn in self.registry.list("ModelProvider"):
            prov = self.registry.get(conn.id).adapter
            if getattr(prov, "kind", "") != "llm":
                continue
            if prov.is_external() and not self.policies().get("semantic.external_llm_allowed", False):  # type: ignore[attr-defined]
                continue
            return ModelGateway(prov, Redactor.from_record(self.store.all(m.User), self.store.all(m.Party)))
        return None

    def corpus_path(self) -> Path:
        base = self.store.get_meta("data_dir") or "demo-data"
        return Path(base) / "corpus" / "corpus.jsonl"

    def semantic_retrain(self) -> dict[str, Any]:
        """`gw semantic retrain`: refit both heads; promotion gate on F1."""
        from gw.engines.semantic.classifier import train

        run = m.AgentRun(id=new_id("AgentRun"), agent=m.AgentName.semantic, trigger="manual", started_at=clock.now(),
                         input_snapshot_ref=self.store.last_seq(), output_type="ClassifierRevision",
                         autonomy_level=m.AutonomyLevel.act)
        self.store.insert(run, actor="agent:semantic", agent_run_id=run.id)
        try:
            entry = train(self.store, self.corpus_path(), max_drop=0.02)
            run.outcome = m.RunOutcome.applied if entry["promoted"] else m.RunOutcome.rejected
            run.summary = f"rev {entry['rev']} intent F1 {entry['f1_intent']:.3f} doc F1 {entry['f1_doc_type']:.3f}" + \
                          ("" if entry["promoted"] else " - not promoted")
        except Exception as e:
            run.outcome, run.error, entry = m.RunOutcome.failed, str(e), {"error": str(e)}
        run.finished_at = clock.now()
        self.store.update(run, actor="agent:semantic", kind="finished", agent_run_id=run.id)
        return entry

    def reply_parse(self, user: m.User, text: str, thread_ref: str | None = None) -> dict[str, Any]:
        """gw.reply.parse (no side effect)."""
        cl = self._pipeline().message("", text, None, clock.now(), {}, thread_ref)
        return dict(cl.as_dict())

    def doc_classify(self, user: m.User, text: str = "", name: str = "") -> dict[str, Any]:
        """gw.doc.classify (no side effect)."""
        return dict(self._pipeline().document(name, text, clock.now()).as_dict())

    def process_inbound(self, trigger: str = "event") -> dict[str, Any]:
        """Classify every unprocessed inbound item; apply high-confidence changes, queue the rest."""
        items = [i for i in self.store.find(m.InboundItem, order_by="received_at", kind=["message", "document"])
                 if i.processed_at is None]
        run = m.AgentRun(id=new_id("AgentRun"), agent=m.AgentName.semantic, trigger=trigger, started_at=clock.now(),
                         input_snapshot_ref=self.store.last_seq(), output_type="ProposedChange",
                         autonomy_level=m.AutonomyLevel.act)
        self.store.insert(run, actor="agent:semantic", agent_run_id=run.id)
        applied = queued = dropped = 0
        if items:
            pl = self._pipeline()
            max_bytes = int(self.policies().get("semantic.max_doc_bytes", 200000))  # type: ignore[attr-defined]
            for it in items:
                cl = self._classify_item(pl, it, max_bytes)
                it.classification = cl.as_dict()
                it.processed_at = clock.now()
                it.commitment_id = cl.commitment_id
                if cl.confidence >= pl.auto_apply:
                    ok = self._apply_changes(cl, it, run)
                    it.review_state = None if ok else "pending"
                    applied += ok
                    queued += not ok
                    dropped += ok and all(ch.kind == "drop" for ch in cl.changes)
                else:
                    it.review_state = "pending"
                    queued += 1
                self.store.update(it, actor="agent:semantic", kind="classified", agent_run_id=run.id,
                                  payload={"label": cl.label, "confidence": round(cl.confidence, 3)})
        run.outcome = m.RunOutcome.applied
        run.finished_at = clock.now()
        run.summary = f"{len(items)} inbound items | {applied - dropped} applied | {dropped} dropped | {queued} to review"
        self.store.update(run, actor="agent:semantic", kind="finished", agent_run_id=run.id)
        return {"items": len(items), "applied": applied - dropped, "dropped": dropped, "review": queued}

    def _classify_item(self, pl: Any, it: m.InboundItem, max_bytes: int) -> Any:
        pay = it.payload
        if it.kind == "message":
            return pl.message(pay.get("subject", ""), pay.get("text", ""), pay.get("from"), it.received_at,
                              {"x-gw-ref": pay.get("gw_ref") or ""}, pay.get("thread_id"))
        text = ""
        if it.kind == "document" and pay.get("ref"):
            from gw.harness.contracts import ExternalRef

            try:  # body read for classification only, never stored
                text = self.registry.get(it.conn_id).read_text(ExternalRef.model_validate(pay["ref"]), max_bytes)
            except Exception:
                text = ""
        return pl.document(pay.get("name", ""), text, it.received_at)

    def _apply_changes(self, cl: Any, it: m.InboundItem, run: m.AgentRun) -> bool:
        """The service applies proposed changes; invalid transitions go to review instead."""
        actor = "agent:semantic"
        for ch in cl.changes:
            try:
                if ch.kind == "drop":
                    continue
                c = self.store.get(m.Commitment, ch.commitment_id)
                if ch.kind == "set_status":
                    status = ch.args["status"]
                    if c.status in ("submitted", "accepted", "waived", "cancelled"):
                        continue
                    if status == "flagged" and not ch.args.get("new_due"):
                        return False
                    nd = datetime.fromisoformat(ch.args["new_due"]).replace(tzinfo=clock.now().tzinfo, hour=17) \
                        if ch.args.get("new_due") else None
                    if status == "confirmed" and c.status == "confirmed":
                        continue
                    repo.set_commitment_status(self.store, c, status, actor, ch.args.get("reason"), nd, run.id)
                    c = self.store.get(m.Commitment, c.id)
                    c.last_reply_at = it.received_at
                    c.first_unanswered_chase_at = None
                    self.store.update(c, actor=actor, kind="replied", agent_run_id=run.id)
                    for chase in self.store.find(m.Chase, commitment_id=c.id, status="sent"):
                        if chase.replied_at is None:
                            chase.replied_at = it.received_at
                            chase.reply_intent = cl.label
                            self.store.update(chase, actor=actor, kind="reply", agent_run_id=run.id)
                elif ch.kind == "link_evidence":
                    if c.status in ("submitted", "accepted", "waived", "cancelled"):
                        continue
                    ref = (it.payload.get("ref") or {})
                    sha = None
                    try:
                        from gw.harness.contracts import ExternalRef

                        src = self.registry.get(it.conn_id)
                        er = ExternalRef.model_validate(ref)
                        sha, uri = src.hash(er), src.link(er)
                    except Exception:
                        uri = f"{it.conn_id}:{ref.get('external_id', it.external_id)}"
                    gi = self.store.get(m.GateInstance, c.gate_instance_id) if c.gate_instance_id else None
                    kind = next((d.evidence for d in gi.deliverables if d.id == c.deliverable_id), "document") if gi else "document"
                    ev = m.Evidence(id=new_id("Evidence"), commitment_id=c.id, kind=m.EvidenceKind(kind), uri=uri,
                                    title=ch.args.get("name", ""), sha256=sha, received_at=it.received_at,
                                    source_conn_id=it.conn_id, revision=cl.entities.get("revision"))
                    repo.link_evidence(self.store, c, ev, actor, run.id)
                elif ch.kind == "retarget_chaser":
                    self.store.log(actor, "Commitment", c.id, "retargeted", {"delegate": ch.args.get("delegate")}, run.id)
                elif ch.kind == "create_risk":
                    self.store.insert(m.Risk(id=new_id("Risk"), project_id=c.project_id, title=ch.args.get("text", "risk"),
                                             linked_commitment_ids=[c.id], owner_user_id=c.owner_user_id),
                                      actor=actor, agent_run_id=run.id)
            except GwError:
                return False
        return True

    def review_queue(self, user: m.User) -> list[dict[str, Any]]:
        out = []
        for it in self.store.find(m.InboundItem, review_state="pending", order_by="-received_at"):
            cl = it.classification or {}
            out.append({"id": it.id, "kind": it.kind, "received_at": it.received_at.isoformat(),
                        "summary": it.payload.get("subject") or it.payload.get("name") or "",
                        "text": (it.payload.get("text") or "")[:400], "from": it.payload.get("from"),
                        "label": cl.get("label"), "confidence": cl.get("confidence"),
                        "candidates": cl.get("candidates", []), "entities": cl.get("entities", {})})
        return out

    def review_decide(self, user: m.User, item: str, label: str, commitment: str | None = None,
                      accept: bool = True) -> dict[str, Any]:
        """One-click review: writes a labelled example; applies the corrected change."""
        from gw.engines.semantic.classifier import doc_text, intent_text
        from gw.engines.semantic.pipeline import Classification, ProposedChange

        gov.require(user, {"project_manager", "portfolio_lead"}, "review queue")
        it = self.store.get(m.InboundItem, item)
        pay = it.payload
        text = intent_text(pay.get("subject", ""), pay.get("text", "")) if it.kind == "message" else doc_text(pay.get("name", ""), "")
        c = self.commitment(commitment) if commitment else None  # type: ignore[attr-defined]
        self.store.insert(m.SemanticExample(id=new_id("SE"), text_hash=hashlib.sha256(text.encode()).hexdigest(),
                                            text=text, head="intent" if it.kind == "message" else "doc_type",
                                            label=label, chosen_link=c.id if c else None, user_id=user.id, at=clock.now()),
                          actor=f"user:{user.id}")
        if accept and c is not None:
            ents = (it.classification or {}).get("entities", {})
            cl = Classification(it.kind, label, 1.0, 1.0, c.id, [], ents, "review")
            if it.kind == "message":
                if label == "CONFIRM":
                    cl.changes = [ProposedChange("set_status", c.id, {"status": "confirmed"})]
                elif label == "FLAG":
                    cl.changes = [ProposedChange("set_status", c.id, {"status": "flagged", "new_due": ents.get("new_due_date"),
                                                                       "reason": ents.get("reason")})]
            else:
                cl.changes = [ProposedChange("link_evidence", c.id, {"name": pay.get("name", "")})]
            run = m.AgentRun(id=new_id("AgentRun"), agent=m.AgentName.semantic, trigger="manual", started_at=clock.now(),
                             output_type="ReviewDecision", autonomy_level=m.AutonomyLevel.act,
                             outcome=m.RunOutcome.applied, finished_at=clock.now(), summary=f"review {it.id} by {user.short}")
            self.store.insert(run, actor=f"user:{user.id}", agent_run_id=run.id)
            self._apply_changes(cl, it, run)
        it.review_state = "resolved"
        it.commitment_id = c.id if c else None
        self.store.update(it, actor=f"user:{user.id}", kind="reviewed", payload={"label": label})
        return {"item": it.id, "label": label, "commitment": c.code if c else None}

    def semantic_metrics(self) -> dict[str, Any]:
        from gw.engines.semantic.classifier import revisions

        revs = revisions(self.store)
        items = [i for i in self.store.all(m.InboundItem) if i.classification and i.classification.get("source") not in ("history", "case")]
        auto = sum(1 for i in items if i.review_state is None)
        return {"revisions": revs, "current": next((r for r in reversed(revs) if r.get("promoted")), None),
                "processed": len(items), "auto_apply_rate": round(auto / len(items), 3) if items else None,
                "queue_depth": self.store.count(m.InboundItem, review_state="pending"),
                "corrections": self.store.count(m.SemanticExample)}

    # --- signals, heat maps, search ----------------------------------------------------------------

    def recompute_signals(self) -> int:
        from gw.signals.core import recompute

        return len(recompute(self.store))

    def signal_query(self, user: m.User, signal: str, scope: str, scope_id: str | None = None,
                     history_days: int | None = None) -> list[dict[str, Any]]:
        """gw.signal.query."""
        self._viewer(user)  # type: ignore[attr-defined]
        if scope_id and scope == "project":
            scope_id = self.project(scope_id).id  # type: ignore[attr-defined]
        if history_days and scope_id:
            rows = self.store.signal_history(signal, scope, scope_id, clock.now() - timedelta(days=history_days))
        else:
            rows = self.store.latest_signals(signal, scope, scope_id)
        return [r.model_dump(mode="json") for r in rows]

    def heatmap(self, user: m.User, mode: str = "exposure") -> dict[str, Any]:
        """gw.heatmap.get: exposure | trend | forecast | load."""
        from gw.signals.core import commitment_load
        from gw.standards import gatemodels as gm

        self._viewer(user)  # type: ignore[attr-defined]
        if mode == "load":
            return {"mode": "load", **commitment_load(self.store)}
        latest = {(s.signal_id, s.scope_id): s for s in self.store.latest_signals(scope="project")}
        projects = self.store.find(m.Project, order_by="code")
        dims: list[str] = []
        for p in projects:
            for d in gm.get(self.store, p.gate_model_id, p.gate_model_rev).dimensions:
                if d not in dims:
                    dims.append(d)
        if mode == "forecast":
            dims = ["forecast"]
        rows = []
        for p in projects:
            gi = self.store.get(m.GateInstance, p.current_gate_instance_id) if p.current_gate_instance_id else None
            cells: list[dict[str, Any]] = []
            for d in dims:
                s = latest.get((f"exposure.{d}", p.id))
                val = None if s is None else (s.trend_7d if mode == "trend" else s.value)
                cells.append({"dim": d, "value": None if val is None else round(val), "drivers": (s.inputs.get("drivers") if s else []) or []})
            rows.append({"code": p.code, "site": p.site, "gate": re.split(r"[|\u00b7]", gi.name)[0].strip() if gi else "", "cells": cells})
        computed = max((s.computed_at for s in latest.values()), default=None)
        return {"mode": mode, "dimensions": dims, "rows": rows, "computed_at": computed.isoformat() if computed else None}

    def search(self, user: m.User, query: str, k: int = 10, sources: list[str] | None = None) -> list[dict[str, Any]]:
        """gw.search: local index plus federated RetrievalSources, hits with citations."""
        from gw.engines.retrieval.index import search

        self._viewer(user)  # type: ignore[attr-defined]
        return search(self.store, self.registry, query, k, sources)

    # --- dependencies -------------------------------------------------------------------------------

    def deps(self, user: m.User, project: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        self._viewer(user)  # type: ignore[attr-defined]
        wps = {w.id: w for w in self.store.all(m.WorkPackage)}
        codes = {p.id: p.code for p in self.store.all(m.Project)}
        out = []
        where: dict[str, Any] = {"status": status} if status else {}
        for d in self.store.find(m.Dependency, order_by="-created_at", **where):
            a, b = wps.get(d.from_work_package_id), wps.get(d.to_work_package_id)
            if project and not ((a and codes[a.project_id] == project) or (b and codes[b.project_id] == project)):
                continue
            out.append({**_j(d), "from": a.code if a else None, "to": b.code if b else None,
                        "from_name": a.name if a else "", "to_name": b.name if b else "",
                        "projects": sorted({codes[x.project_id] for x in (a, b) if x})})
        return out

    def deps_decide(self, user: m.User, dependency: str, accept: bool, reason: str = "") -> dict[str, Any]:
        """gw.deps.decide ( Dependency.status)."""
        gov.require(user, {"project_manager", "portfolio_lead"}, "deps.decide")
        d = self.store.get(m.Dependency, dependency)
        allowed = {"proposed": {"accepted", "rejected"}, "accepted": {"rejected"}}.get(str(d.status), set())
        new = "accepted" if accept else "rejected"
        if new not in allowed:
            raise Conflict(f"{d.id}: {d.status} -> {new} not allowed", sorted(allowed))
        if not accept and not reason and d.status == "accepted":
            raise Unprocessable("rejecting an accepted dependency needs a reason", "/reason")
        d.status = m.DependencyStatus(new)
        d.decided_by_user_id = user.id
        d.decided_at = clock.now()
        d.reason = d.reason + (f" | {reason}" if reason else "")
        self.store.update(d, actor=f"user:{user.id}", kind="decision", payload={"status": new, "reason": reason})
        return _d(d)

    # --- agents -----------------------------------------------------------------------------------

    def run_scout(self, trigger: str = "schedule") -> RunResult:
        from gw.agents.scout import Scout

        return run_agent(self.store, Scout(), trigger=trigger)

    def run_brief(self, user_key: str | None = None, trigger: str = "schedule", deliver: bool = True) -> RunResult:
        from gw.agents.brief import Brief

        res = run_agent(self.store, Brief(user_key), trigger=trigger)
        if deliver:
            for out in res.outputs:
                if out.output_type == "Brief":
                    self.deliver_brief(out)
        return res

    def deliver_brief(self, out: m.AgentOutput) -> str | None:
        """Deliver a brief to its internal recipient by Teams card or mail; recorded as an event.

        Recipients are users, never parties; the harness refuses an internal send to any non-user address.
        """
        from gw.agents.brief import render_text
        from gw.harness.contracts import ApprovalCard, OutboundMessage

        key = out.payload.get("user")
        user = next((u for u in self.store.all(m.User) if u.short == key), None) if key else None
        if user is None:
            return None
        pol = self.policies()  # type: ignore[attr-defined]
        prefer = str(pol.get("channels.by_party_kind.internal_team", "mail"))
        conns = self.registry.list("MessageChannel")
        text = render_text(out.payload)
        via = None
        card_conn = next((c for c in conns if "post_cards" in c.scopes), None)
        mail_conn = next((c for c in conns if "send_as" in c.scopes), None)
        if prefer in ("teams", "slack") and card_conn is not None:
            self.registry.get(card_conn.id).post_card(ApprovalCard(
                approval_id=out.id, title=f"Morning brief {out.payload.get('date', '')}", body=text[:3000],
                actions=["Open"], token=""))
            via = f"card:{card_conn.id}"
        elif mail_conn is not None:
            self.registry.get(mail_conn.id).send(OutboundMessage(
                to=[user.email], subject=f"Morning brief {out.payload.get('date', '')}", text=text,
                gw_ref=f"brief/{out.id}", party_id="", ai_generated=out.payload.get("ai_generated")))
            via = f"mail:{mail_conn.id}"
        if via:
            self.store.log("agent:brief", "AgentOutput", out.id, "brief_delivered", {"user": user.id, "via": via},
                           out.agent_run_id)
        return via

    def run_reporter(self, period: str = "weekly", trigger: str = "schedule") -> RunResult:
        from gw.agents.reporter import Reporter

        return run_agent(self.store, Reporter(period), trigger=trigger)

    def run_reporter_monthly_if_due(self, trigger: str = "schedule") -> RunResult | None:
        """Monthly report on the first business day of the month, holiday-aware."""
        from datetime import timedelta

        from gw.signals.calendars import extra_from, is_business_day

        today = clock.today()
        pol = self.policies()  # type: ignore[attr-defined]
        country = next((p.country for p in self.store.find(m.Project, order_by="code")), "NL")
        d = today.replace(day=1)
        while not is_business_day(d, country, extra_from(pol)):
            d += timedelta(days=1)
        if d != today:
            return None
        return self.run_reporter("monthly", trigger=trigger)

    def ask(self, user: m.User, question: str, conversation: str | None = None) -> dict[str, Any]:
        """gw.ask: the assistant answers one question with the record's tools."""
        from gw.agents.assistant import Assistant

        question = (question or "").strip()
        if not question or len(question) > 2000:
            raise Unprocessable("ask a question of 1 to 2000 characters", "/question")
        pol = self.policies()  # type: ignore[attr-defined]
        cfg = dict(pol.get("models.assistant", {}) or {})
        if not cfg.get("connection"):
            raise Unprocessable("the assistant is off: set policies.models.assistant.connection to a chat connection")
        mine = self.store.events(entity_id=user.id, entity_type="User", since=clock.now() - timedelta(hours=1),
                                 limit=5000)
        if sum(1 for e in mine if e["kind"] == "ask") >= int(cfg.get("per_user_per_hour", 30)):
            raise Conflict(f"{user.name}: hourly question limit reached; try again later", [])
        with _ASK_LOCK:
            if user.id in _ASK_IN_FLIGHT:
                raise Conflict(f"{user.name}: one question at a time; the last one is still being answered", [])
            _ASK_IN_FLIGHT.add(user.id)
        try:
            # the reservation counts towards the hourly limit before any model call
            self.store.log(f"user:{user.id}", "User", user.id, "ask", {"conversation": conversation})
            history = [(str(o.payload.get("question", "")), str(o.payload.get("answer", "")))
                       for o in self.answers(user, conversation)][-4:] if conversation else []
            res = run_agent(self.store, Assistant(self, user, question, history, conversation), trigger="user")
            if not res.outputs:
                raise GwError(f"assistant run {res.run.id} failed: {res.run.error or 'no answer'}")
            out = res.outputs[0]
            self.store.log(f"user:{user.id}", "User", user.id, "answered",
                           {"conversation": conversation, "output": out.id}, res.run.id)
            return {**out.payload, "output_id": out.id, "run": res.run.id}
        finally:
            with _ASK_LOCK:
                _ASK_IN_FLIGHT.discard(user.id)

    def answers(self, user: m.User, conversation: str | None = None, limit: int = 50) -> list[m.AgentOutput]:
        """A user's answers, oldest first, from their own `answered` events (indexed by entity)."""
        evs = [e for e in self.store.events(entity_id=user.id, entity_type="User", limit=5000) if e["kind"] == "answered"
               and (conversation is None or (e["payload_json"] or {}).get("conversation") == conversation)][:limit]
        outs = [self.store.find_one(m.AgentOutput, id=(e["payload_json"] or {}).get("output")) for e in reversed(evs)]
        return [o for o in outs if o is not None]

    def brief(self, user: m.User) -> dict[str, Any]:
        """gw.brief.today: today's brief for the user (rendered now if none today)."""
        today = clock.today()
        outs = [o for o in self.store.find(m.AgentOutput, output_type="Brief", order_by="-created_at")
                if o.payload.get("user") == user.short and o.created_at.date() == today]
        if not outs:
            self.run_brief(user.short, trigger="manual")
            outs = [o for o in self.store.find(m.AgentOutput, output_type="Brief", order_by="-created_at")
                    if o.payload.get("user") == user.short]
        if not outs:
            raise NotFound(f"no brief for {user.short}")
        return {**outs[0].payload, "output_id": outs[0].id, "citations": outs[0].citations}

    def brief_text(self, user: m.User) -> str:
        from gw.agents.brief import render_text
        from gw.governance.disclosure import with_readable_line

        b = self.brief(user)
        return with_readable_line(render_text(b), b.get("ai_generated"))

    def report(self, period: str = "weekly", user: m.User | None = None) -> dict[str, Any]:
        """The latest weekly or monthly report. Reading never starts a run (POST /reports/{period}/run)."""
        if user is not None:
            self._viewer(user)  # type: ignore[attr-defined]
        if period not in ("weekly", "monthly"):
            raise Unprocessable("period must be weekly or monthly", "/period")
        outs = [o for o in self.store.find(m.AgentOutput, output_type="Report", order_by="-created_at")
                if o.payload.get("period") == period]
        if not outs:
            raise NotFound(f"no {period} report yet; it is rendered on schedule or with POST /reports/{period}/run")
        return dict(outs[0].payload)

    def run_tender(self, package: str, trigger: str = "manual") -> RunResult:
        from gw.agents.tender import Tender

        return run_agent(self.store, Tender(package), trigger=trigger)

    def tender_check(self, user: m.User, package: str) -> dict[str, Any]:
        """gw.tender.check: package completeness and bid comparison (commercial locked until sign-off)."""
        gov.require(user, {"project_manager", "portfolio_lead", "sme:procurement", "sme:engineering"}, "tender.check")
        if self.store.find_one(m.WorkPackage, code=package) is None:
            raise NotFound(f"package {package} not found")
        res = self.run_tender(package)
        if res.run.outcome == "failed":
            raise Conflict(f"{package}: tender check failed: {res.run.error}", [])
        out = {o.output_type: o.payload for o in res.outputs}
        return {"package": package, "run": res.run.id, "check": out.get("PackageCheck"),
                "bids": out.get("BidComparison"), "advisory": True}

    def tender_signoff(self, user: m.User, package: str, note: str = "") -> dict[str, Any]:
        """A person records the technical sign-off; two different accepting roles unlock commercials."""
        from gw.agents.tender import SIGNOFF_ROLES, signoffs

        wp = self.store.find_one(m.WorkPackage, code=package)
        if wp is None:
            raise NotFound(f"package {package} not found")
        roles = sorted(set(user.roles) & SIGNOFF_ROLES)
        if not roles:
            raise NotAuthorised(f"{package}: technical sign-off needs one of {sorted(SIGNOFF_ROLES)}")
        done = signoffs(self.store, wp)
        if any(s["role"] == roles[0] for s in done):
            raise Conflict(f"{package}: {roles[0]} has already signed; the second sign-off needs a different role",
                           sorted(SIGNOFF_ROLES - {s["role"] for s in done}))
        self.store.insert(m.Decision(id=new_id("Decision"), project_id=wp.project_id, title=f"technical sign-off {wp.code}",
                                     decided_by_user_id=user.id, decided_at=clock.now(), chosen=roles[0],
                                     rationale=note or "technical evaluation accepted"), actor=f"user:{user.id}")
        return {"package": package, "signoffs": signoffs(self.store, wp)}

    # --- forecast ----------------------------------------------------------------------------------

    def run_forecast(self, trigger: str = "schedule") -> dict[str, Any]:
        """Score open commitments and pending gates (advisory); recorded as a forecast AgentRun."""
        from gw.engines.forecast.engine import Engine

        run = m.AgentRun(id=new_id("AgentRun"), agent=m.AgentName.forecast, trigger=trigger, started_at=clock.now(),
                         input_snapshot_ref=self.store.last_seq(), output_type="Forecast", autonomy_level=m.AutonomyLevel.act)
        self.store.insert(run, actor="agent:forecast", agent_run_id=run.id)
        try:
            eng = Engine(self.store, self.policies())  # type: ignore[attr-defined]
            res = eng.run(run.id)
            run.outcome = m.RunOutcome.applied
            run.summary = f"{res['scored']} commitments rescored, {res['above_flag']} above flag ({res['model']})"
            if res.get("degraded"):
                self.store.write_signals([m.SignalRow(signal_id="forecast_degraded", scope="portfolio", scope_id="portfolio",
                                                      computed_at=clock.now(), value=1.0, inputs={"reason": res["degraded"]})])
        except Exception as e:
            run.outcome, run.error, res = m.RunOutcome.failed, str(e)[:500], {"error": str(e)}
        run.finished_at = clock.now()
        run.cost_seconds = (run.finished_at - run.started_at).total_seconds()
        self.store.update(run, actor="agent:forecast", kind="finished", agent_run_id=run.id)
        return res

    def forecast_calibrate(self) -> dict[str, Any]:
        from gw.engines.forecast.engine import Engine

        return Engine(self.store, self.policies()).backtest()  # type: ignore[attr-defined]

    def _fc_latest(self, target: str) -> dict[str, m.ForecastRow]:
        from gw.engines.forecast.engine import latest

        return latest(self.store, target)

    def forecast_slip(self, user: m.User, project: str | None = None, commitment: str | None = None) -> list[dict[str, Any]]:
        """gw.forecast.slip: advisory P(slip), P50/P90 and drivers."""
        self._viewer(user)  # type: ignore[attr-defined]
        fc = self._fc_latest("slip")
        cs = {c.id: c for c in self.store.all(m.Commitment)}
        pid = self.project(project).id if project else None  # type: ignore[attr-defined]
        out: list[dict[str, Any]] = []
        for cid, r in fc.items():
            c = cs.get(cid)
            if c is None or c.status in ("accepted", "waived", "cancelled"):
                continue
            if pid and c.project_id != pid:
                continue
            if commitment and commitment not in (c.code, c.id):
                continue
            out.append({"commitment": c.code, "commitment_id": c.id, "title": c.title, "p_slip": r.p,
                        "p50_days": r.p50_days, "p90_days": r.p90_days, "drivers": r.drivers, "model": r.model_id,
                        "advisory": True, "due": c.due_at.isoformat()})
        out.sort(key=lambda x: -x["p_slip"])
        return out

    def forecast_gate(self, user: m.User, project: str, gate: str | None = None) -> dict[str, Any]:
        gi = self.gate_instance(project, gate)  # type: ignore[attr-defined]
        r = self._fc_latest("gate_pass_on_date").get(gi.id)
        return {"project": project, "gate": gi.gate_id, "p_pass_on_date": r.p if r else None,
                "review_at": gi.planned_review_at.isoformat(), "model": r.model_id if r else None, "advisory": True}

    def _target_row(self, target: str) -> tuple[m.Commitment, dict[str, Any]]:
        from gw.engines.forecast.engine import live_rows

        c = self.commitment(target)  # type: ignore[attr-defined]
        row = next((lv.row for lv in live_rows(self.store) if lv.commitment.id == c.id), None)
        if row is None:
            raise Conflict(f"{c.code}: only open commitments are forecast", [])
        return c, row

    def forecast_explain(self, user: m.User, target: str) -> dict[str, Any]:
        """gw.forecast.explain: top three signed drivers."""
        from gw.engines.forecast.engine import Engine, context

        c, row = self._target_row(target)
        eng = Engine(self.store, self.policies())  # type: ignore[attr-defined]
        ctx = context(self.store, self.policies())  # type: ignore[attr-defined]
        p = eng.predict(ctx, [row])
        drivers = eng.drivers(ctx, [row], p)[0]
        return {"commitment": c.code, "p_slip": round(float(p[0]), 3), "drivers": drivers, "model": eng.model_id,
                "advisory": True}

    def forecast_whatif(self, user: m.User, target: str, levers: list[str]) -> dict[str, Any]:
        """gw.forecast.whatif: fixed lever catalogue only."""
        from gw.engines.forecast.engine import WHATIF, Engine, context

        bad = [lv for lv in levers if lv not in WHATIF]
        if bad:
            raise Unprocessable(f"unknown levers {bad}; catalogue: {sorted(WHATIF)}", "/levers")
        c, row = self._target_row(target)
        eng = Engine(self.store, self.policies())  # type: ignore[attr-defined]
        before, after = eng.whatif(context(self.store, self.policies()), row, levers)  # type: ignore[attr-defined]
        return {"commitment": c.code, "levers": levers, "before": round(before, 3), "after": round(after, 3),
                "delta": round(after - before, 3), "advisory": True}

    def calibration(self) -> dict[str, Any] | None:
        rows = self.store.find(m.CalibrationRow, order_by="-computed_at", limit=1)
        return _d(rows[0]) if rows else None

    # --- the day -----------------------------------------------------------------------------------

    def morning(self, trigger: str = "schedule") -> dict[str, Any]:
        """Data flow of one day: sync, classify, signals, forecast, agents, brief."""
        out: dict[str, Any] = {}
        sync = self.registry.sync_all()
        out["synced"] = len(sync)
        out["inbound"] = self.process_inbound(trigger=trigger)
        out["forecast"] = self.run_forecast(trigger=trigger).get("scored", 0)
        out["signals"] = self.recompute_signals()
        out["gatekeeper"] = self.run_gatekeeper(trigger=trigger).run.summary  # type: ignore[attr-defined]
        out["scout"] = self.run_scout(trigger=trigger).run.summary
        tenders = [w.code for w in self.store.all(m.WorkPackage) if w.tender_state != "none"]
        if tenders and self.registry.list("DocumentSource") and any(
                c.config.get("role") == "tender" for c in self.registry.list("DocumentSource")):
            out["tender"] = f"{sum(self.run_tender(code, trigger).run.outcome != 'failed' for code in tenders)} packages checked"
        out["chaser"] = self.run_chaser(trigger=trigger)["summary"]
        self.recompute_signals()
        if not self.store.find(m.CalibrationRow, limit=1):
            cal = self.forecast_calibrate()
            out["calibration"] = {"brier": cal["brier"], "ece": cal["ece"]}
        out["reporter"] = self.run_reporter(trigger=trigger).run.summary
        out["brief"] = self.run_brief(None, trigger=trigger).run.summary
        from gw.engines.retrieval.index import refresh_index

        out["index"] = refresh_index(self.store)
        return out

    # --- governance: readiness snapshot and promotion proposals ------------------

    def nightly_governance(self) -> dict[str, Any]:
        """Snapshot automation readiness per agent; propose promotions where earned (never applied here)."""
        from gw.governance.promotion import propose
        from gw.signals.core import snapshot_automation_readiness

        rows = snapshot_automation_readiness(self.store)
        aps = propose(self.store)
        return {"snapshots": len(rows), "proposals": [a.id for a in aps]}

    # --- taxonomy -----------------------------------------------------------------------

    def taxonomy_get(self) -> dict[str, Any]:
        from gw.engines.semantic.taxonomy import current_text, revisions

        revs = revisions(self.store)
        return {"rev": revs[-1]["rev"] if revs else 0, "yaml": current_text(self.store),
                "history": [{k: r[k] for k in ("rev", "author", "at", "note")} for r in revs]}

    def taxonomy_set(self, user: m.User, text: str, note: str = "") -> dict[str, Any]:
        from gw.engines.semantic.taxonomy import save

        gov.require(user, {"portfolio_lead", "admin"}, "taxonomy.set")
        entry = save(self.store, text, f"user:{user.id}", note)
        return {"rev": entry["rev"], "diff": entry["diff"]}

    # --- live connections -------------------------------------------------------------------------

    def harness_login(self, user: m.User, connection: str, show: Any = print) -> str:
        """Device-code sign-in for a delegated Graph connection (; no secret is stored)."""
        gov.require(user, {"admin", "portfolio_lead"}, "harness.login")
        who = str(self.harness_graph(connection).login(show))
        self.store.log(f"user:{user.id}", "Connection", connection, "signed_in", {})
        return who

    def harness_graph(self, connection: str) -> Any:
        adapter = self.registry.get(connection).adapter
        g = getattr(adapter, "g", None)
        if g is None or not getattr(g, "delegated", False):
            raise Conflict(f"{connection}: not a delegated (device_code) Graph connection", [])
        return g

    def harness_add_live(self, user: m.User, kind: str, conn_id: str, tenant: str, client_id: str,
                         mailbox: str = "", root_path: str = "Gatewright demo") -> dict[str, Any]:
        """Add a delegated Graph connection from the UI (GW-600). Identifiers only; no secret is accepted.

        The values are stored in the record's connections table (gitignored database, never exported)
        and become leak-scan patterns for the public export.
        """
        import re

        gov.require(user, {"admin", "portfolio_lead"}, "harness.add")
        if any("$" in str(v) for v in (conn_id, tenant, client_id, mailbox, root_path)):
            raise Unprocessable("connection fields may not contain '$'", "/")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}", conn_id):
            raise Unprocessable("connection id: lowercase letters, digits and hyphens", "/id")
        guid = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        if not re.fullmatch(guid, client_id.strip()):
            raise Unprocessable("client id must be the application (client) GUID", "/client_id")
        if not (re.fullmatch(guid, tenant.strip()) or re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", tenant.strip())):
            raise Unprocessable("tenant must be the directory (tenant) GUID or a verified domain", "/tenant")
        cfg: dict[str, Any] = {"auth": "device_code", "tenant": tenant.strip(), "client_id": client_id.strip()}
        if kind == "mail":
            cfg.update(mailbox=mailbox.strip(), demo_party_header=True)
            spec = {"id": conn_id, "contract": "MessageChannel", "adapter": "m365_mail_graph",
                    "name": "Masked demo mailbox (live)", "config": cfg, "scopes": ["read", "seed_demo"],
                    "owner": user.short or user.id, "sync_every_minutes": 15}
        elif kind == "docs":
            cfg.update(root_path=root_path.strip() or "Gatewright demo")
            spec = {"id": conn_id, "contract": "DocumentSource", "adapter": "onedrive_graph",
                    "name": f"OneDrive folder '{cfg['root_path']}' (live)", "config": cfg,
                    "scopes": ["read", "link", "seed_demo"], "owner": user.short or user.id, "sync_every_minutes": 15}
        else:
            raise Unprocessable("kind must be mail or docs", "/kind")
        return _d(self.registry.add(spec, actor=f"user:{user.id}"))

    # --- privacy and retention -----------------------------------------------------------------------

    def privacy_export(self, email: str) -> dict[str, Any]:
        """`gw privacy export`: everything stored about one person."""
        users = [_j(u) for u in self.store.all(m.User) if u.email == email]
        contacts = [{"party": p.name, **c.model_dump()} for p in self.store.all(m.Party) for c in p.contacts if c.email == email]
        items = [_j(i) for i in self.store.all(m.InboundItem) if (i.payload or {}).get("from") == email]
        return {"email": email, "users": users, "contacts": contacts, "inbound_items": items}

    def privacy_erase(self, email: str) -> dict[str, Any]:
        """`gw privacy erase`: rewrite payloads and contacts, keep event shells."""
        n = 0
        for i in self.store.all(m.InboundItem):
            if (i.payload or {}).get("from") == email:
                i.payload = {"erased": True}
                self.store.update(i, actor="system:privacy", kind="erased")
                n += 1
        for p in self.store.all(m.Party):
            if any(c.email == email for c in p.contacts):
                p.contacts = [c if c.email != email else m.Contact(name="(erased)", email="erased@invalid") for c in p.contacts]
                self.store.update(p, actor="system:privacy", kind="erased")
                n += 1
        return {"email": email, "rewritten": n}

    def retention_purge(self) -> int:
        """Purge inbound payloads older than policies.retention; derived facts stay."""
        days = int(self.policies().get("retention.inbound_item_payload_days", 90))  # type: ignore[attr-defined]
        cutoff = clock.now() - timedelta(days=days)
        n = 0
        for i in self.store.all(m.InboundItem):
            if i.received_at < cutoff and i.payload and not i.payload.get("purged"):
                i.payload = {"purged": True, "subject": "", "kind": i.kind}
                self.store.update(i, actor="system:retention", kind="purged")
                n += 1
        return n

    def json(self, obj: Any) -> str:
        return json.dumps(obj, default=str)
