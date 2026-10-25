"""Scout agent.

Deterministic detectors propose dependencies between work packages; a person
accepts or rejects. A rejection suppresses the same proposal for 90 days.
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import combinations
from typing import Any

import yaml

from gw import paths
from gw.agents.base import AgentOutput, Snapshot
from gw.policies.store import Policies
from gw.record import clock
from gw.record import models as m
from gw.record.ids import new_id
from gw.record.store import Store

CREW_DISCIPLINES = {"slab", "racking", "asrs", "refrigeration", "fire", "dock", "weigh", "pv"}
SUPPRESS_DAYS = 90


def _overlap(a0: date | None, a1: date | None, b0: date | None, b1: date | None) -> int:
    if not (a0 and a1 and b0 and b1):
        return 0
    return max(0, (min(a1, b1) - max(a0, b0)).days)


def sequences() -> list[dict[str, Any]]:
    return list((yaml.safe_load((paths.STANDARDS_DATA / "sequences.yaml").read_text(encoding="utf-8")) or {}).get("rules", []))


def wp_of(c: m.Commitment, wps: list[m.WorkPackage]) -> m.WorkPackage | None:
    if c.work_package_id:
        return next((w for w in wps if w.id == c.work_package_id), None)
    tokens = set(c.deliverable_id.split("_")) | set(c.tags)
    hit = next((w for w in wps if w.discipline in tokens), None)
    return hit or (wps[0] if wps else None)


class Scout:
    name = "scout"
    output_type = "DependencyProposal"

    def __init__(self) -> None:
        self.summary = ""

    def snapshot(self, store: Store, policies: Policies, **kw: Any) -> Snapshot:
        from gw.harness.registry import Registry

        outages: list[dict[str, Any]] = []
        reg = Registry(store)
        now = clock.now()
        for conn in reg.list("CalendarSource"):
            try:
                for ev in reg.get(conn.id).list_events((now, now + timedelta(days=180))):
                    if ev.kind == "outage" or "outage" in ev.title.lower():
                        outages.append({"title": ev.title, "start": ev.start.date(), "end": ev.end.date()})
            except Exception:
                continue
        return Snapshot(seq=store.last_seq(), policies=policies, data={
            "now": now, "projects": {p.id: p for p in store.all(m.Project)},
            "packages": store.all(m.WorkPackage), "parties": {p.id: p for p in store.all(m.Party)},
            "users": {u.id: u for u in store.all(m.User)},
            "commitments": [c for c in store.all(m.Commitment) if c.status in ("open", "flagged", "overdue", "confirmed")],
            "deps": store.all(m.Dependency), "evidence": store.all(m.Evidence), "outages": outages,
            "rules": sequences()})

    def run(self, snap: Snapshot) -> list[AgentOutput]:
        d = snap.data
        now = d["now"]
        projects = d["projects"]
        wps_by_project: dict[str, list[m.WorkPackage]] = {}
        for w in d["packages"]:
            wps_by_project.setdefault(w.project_id, []).append(w)
        existing: dict[tuple[str, ...], Any] = {}  # undirected kinds match either way round; directed ones only one way
        existing_directed: dict[tuple[str, ...], Any] = {}
        for dep in d["deps"]:
            key = (str(dep.kind), *sorted((dep.from_work_package_id, dep.to_work_package_id)))
            existing[key] = dep
            existing_directed[(str(dep.kind), dep.from_work_package_id, dep.to_work_package_id)] = dep
        props: dict[tuple[str, ...], dict[str, Any]] = {}

        def propose(kind: str, a: m.WorkPackage, b: m.WorkPackage, reason: str, refs: list[str], days: float | None,
                    directed: bool = False) -> None:
            key = (kind, a.id, b.id) if directed else (kind, *sorted((a.id, b.id)))
            old = existing_directed.get(key) if directed else existing.get(key)
            if old is not None:
                if old.status in ("proposed", "accepted"):
                    return
                if old.status == "rejected" and old.decided_at and old.decided_at > now - timedelta(days=SUPPRESS_DAYS):
                    return
            if key in props:
                return
            props[key] = {"kind": kind, "from": a, "to": b, "reason": reason, "refs": refs, "days": days}

        # shared_party: one crew-bound vendor on two packages with overlapping windows
        crew = [w for w in d["packages"] if w.discipline in CREW_DISCIPLINES and w.party_id]
        for a, b in combinations(crew, 2):
            if a.project_id == b.project_id or a.party_id != b.party_id:
                continue
            ov = _overlap(a.window_start, a.window_end, b.window_start, b.window_end)
            if ov >= 7:
                party = d["parties"][a.party_id].name
                first, second = sorted((a, b), key=lambda w: w.window_start or date.max)
                propose("shared_party", first, second,
                        f"{party} on {projects[a.project_id].code} and {projects[b.project_id].code} with {ov} d overlap; "
                        f"sequencing {projects[first.project_id].code} first", [a.id, b.id], float(min(ov, 14)))
        # shared_person: one owner with commitments due within 10 days in two projects
        horizon = now + timedelta(days=10)
        by_user: dict[str, list[m.Commitment]] = {}
        for c in d["commitments"]:
            if c.due_at <= horizon and c.kind in ("gate_deliverable", "action"):
                by_user.setdefault(c.owner_user_id, []).append(c)
        for uid, cs in by_user.items():
            per_project: dict[str, m.Commitment] = {}
            for c in sorted(cs, key=lambda x: x.due_at):
                per_project.setdefault(c.project_id, c)
            owned_projects = {p.id for p in projects.values() if p.owner_user_id == uid}
            for (pa, ca), (pb, cb) in combinations(sorted(per_project.items()), 2):
                if pa in owned_projects or pb in owned_projects:
                    continue  # a PM's own project is not a cross-project dependency
                wa, wb = wp_of(ca, wps_by_project.get(pa, [])), wp_of(cb, wps_by_project.get(pb, []))
                if wa and wb:
                    user = d["users"].get(uid)
                    propose("shared_person", wa, wb,
                            f"{user.name if user else uid} owns {ca.code} ({projects[pa].code}) and {cb.code} "
                            f"({projects[pb].code}), both due within 10 days", [ca.id, cb.id], None)
        # sequence: rules from standards/sequences.yaml within a project
        for pid, wps in wps_by_project.items():
            by_disc = {w.discipline: w for w in wps}
            for rule in d["rules"]:
                a, b = by_disc.get(rule["before"]), by_disc.get(rule["after"])
                if a and b and a.window_end and b.window_start and b.window_start < a.window_end:
                    propose("sequence", a, b, f"{projects[pid].code}: {rule['before']} must finish before {rule['after']} - "
                            f"{rule['reason']}" + (f" ({rule['lesson']})" if rule.get("lesson") else ""),
                            [a.id, b.id], float(rule.get("saved_days", 0)) or None, directed=True)
        # outage_window: package activities inside a calendar outage
        for o in d["outages"]:
            hits = [w for w in d["packages"] if _overlap(w.window_start, w.window_end, o["start"], o["end"]) > 0]
            for a, b in combinations(hits, 2):
                if a.project_id == b.project_id:
                    propose("outage_window", a, b, f"both fall in '{o['title']}'", [a.id, b.id], None)
        # permit_office: the same authority on permit deliverables of two projects
        auth: dict[str, list[m.Commitment]] = {}
        for c in d["commitments"]:
            if d["parties"][c.party_id].kind == "authority" and "permits" in c.tags:
                auth.setdefault(c.party_id, []).append(c)
        for party_id, cs in auth.items():
            for ca, cb in combinations(cs, 2):
                if ca.project_id != cb.project_id:
                    wa, wb = wp_of(ca, wps_by_project.get(ca.project_id, [])), wp_of(cb, wps_by_project.get(cb.project_id, []))
                    if wa and wb:
                        propose("permit_office", wa, wb, f"{d['parties'][party_id].name} holds {ca.code} and {cb.code}",
                                [ca.id, cb.id], None)
        outs = []
        for p in props.values():
            a, b = p["from"], p["to"]
            outs.append(AgentOutput(
                output_type="DependencyProposal",
                title=f"{p['kind'].replace('_', ' ')}: {projects[a.project_id].code} {a.code[-4:]} -> {projects[b.project_id].code} {b.code[-4:]}",
                payload={"kind": p["kind"], "from_work_package_id": a.id, "to_work_package_id": b.id,
                         "from": a.code, "to": b.code, "reason": p["reason"], "saved_days_estimate": p["days"],
                         "evidence_refs": p["refs"]},
                citations=[a.id, b.id, *p["refs"]], on_draft=create_proposal))
        self.summary = f"{len(outs)} proposals"
        return outs


def create_proposal(store: Store, run: m.AgentRun, out: m.AgentOutput, approval_id: str | None) -> m.Dependency:
    pl = out.payload
    dep = m.Dependency(id=new_id("Dependency"), from_work_package_id=pl["from_work_package_id"],
                       to_work_package_id=pl["to_work_package_id"], kind=m.DependencyKind(pl["kind"]),
                       proposed_by="scout", reason=pl["reason"], saved_days_estimate=pl.get("saved_days_estimate"),
                       evidence_refs=pl.get("evidence_refs", []), created_at=clock.now())
    store.insert(dep, actor="agent:scout", agent_run_id=run.id)
    return dep
