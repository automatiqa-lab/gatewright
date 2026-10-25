"""Brief agent.

Three focus items ranked by impact x urgency (deterministic; ties prefer items that
block a gate), changed-since-yesterday with revision marks, an agents-overnight
summary and a not-done-by-agents statement that is checked against the audit log.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from gw.agents.base import AgentOutput, Snapshot
from gw.policies.store import Policies
from gw.record import clock
from gw.record import models as m
from gw.record.store import Store


def not_done_by_agents(store: Store, since_hours: int = 24) -> dict[str, Any]:
    """Facts for the not-done statement, from the record: schedule writes and unapproved sends."""
    since = clock.now() - timedelta(hours=since_hours)
    parties = {p.id: p for p in store.all(m.Party)}
    approvals = {a.id: a for a in store.all(m.Approval)}
    unapproved = []
    for ch in store.all(m.Chase):
        if ch.status != "sent" or not ch.sent_at or ch.sent_at < since or not ch.agent_run_id:
            continue
        if parties[ch.party_id].kind in ("authority", "sponsor"):
            ap = approvals.get(ch.approval_id or "")
            if ap is None or ap.decision not in ("approved", "approved_edited"):
                unapproved.append(ch.gw_ref)
    schedule_writes = [e for e in store.events(since=since, limit=100000) if e["kind"] == "schedule_write"]
    return {"schedule_writes": len(schedule_writes), "unapproved_sends": unapproved,
            "statement": ("Nothing moved in any schedule." if not schedule_writes else
                          f"{len(schedule_writes)} schedule writes recorded.") + " " +
                         ("No message went to a sponsor or an authority without approval." if not unapproved else
                          f"{len(unapproved)} messages to sponsors or authorities lacked approval: {', '.join(unapproved)}.")}


def change_labels(store: Store, events: list[dict[str, Any]]) -> dict[str, str]:
    """Readable names for approvals and dependencies in the changed-since list."""
    ids = {e["entity_id"] for e in events if e["entity_type"] in ("Approval", "Dependency")}
    if not ids:
        return {}
    packages = {w.id: w.code for w in store.all(m.WorkPackage)}
    out: dict[str, str] = {}
    for ap in store.find(m.Approval, id=[i for i in ids if i.startswith("AP-")]):
        out[ap.id] = ap.title or f"{str(ap.kind).replace('_', ' ')} approval"
    for dep in store.find(m.Dependency, id=[i for i in ids if i.startswith("DEP-")]):
        out[dep.id] = (f"{packages.get(dep.from_work_package_id, '?')} → {packages.get(dep.to_work_package_id, '?')} "
                       f"{str(dep.kind).replace('_', ' ')} dependency")
    return out


class Brief:
    name = "brief"
    output_type = "Brief"

    def __init__(self, user_key: str | None = None) -> None:
        self.user_key = user_key
        self.summary = ""

    def snapshot(self, store: Store, policies: Policies, **kw: Any) -> Snapshot:
        from gw.engines.drafting import Drafter

        self.drafter = Drafter(store, policies, "brief")
        now = clock.now()
        users = store.all(m.User)
        user = next((u for u in users if u.short == self.user_key or u.email == self.user_key), None) if self.user_key \
            else next((u for u in users if "portfolio_lead" in u.roles), None)
        projects = {p.id: p for p in store.all(m.Project)}
        if user and "portfolio_lead" not in user.roles:
            projects = {k: v for k, v in projects.items() if v.owner_user_id == user.id}
        exposure: dict[str, dict[str, float]] = {}
        for s in store.latest_signals(scope="project"):
            if s.signal_id.startswith("exposure."):
                exposure.setdefault(s.scope_id, {})[s.signal_id[9:]] = s.value
        assessments = {}
        for o in store.find(m.AgentOutput, output_type="GateAssessment", order_by="created_at"):
            assessments[o.payload.get("project")] = o
        since = now - timedelta(hours=24)
        events = [e for e in store.events(since=since, limit=20000)
                  if e["entity_type"] in ("Commitment", "GateInstance", "Dependency", "Approval") and
                  e["kind"] in ("status", "decision", "chased", "accepted", "rejected", "created")
                  and not str(e["actor"]).startswith("system:")]
        runs = [r for r in store.find(m.AgentRun, order_by="-started_at") if r.started_at >= since]
        inbound = [i for i in store.find(m.InboundItem) if i.processed_at and i.processed_at >= since]
        return Snapshot(seq=store.last_seq(), policies=policies, data={
            "now": now, "user": user, "projects": projects, "exposure": exposure, "assessments": assessments,
            "approvals": [a for a in store.find(m.Approval, decision="pending")],
            "outputs": {o.id: o for o in store.find(m.AgentOutput, disposition="draft")},
            "deps": store.find(m.Dependency, status="proposed"), "packages": {w.id: w for w in store.all(m.WorkPackage)},
            "commitments": {c.id: c for c in store.all(m.Commitment)}, "events": events, "runs": runs,
            "labels": change_labels(store, events),
            "gates": {g.id: g for g in store.all(m.GateInstance)},
            "inbound": inbound, "not_done": not_done_by_agents(store)})

    def candidates(self, snap: Snapshot) -> list[dict[str, Any]]:
        d = snap.data
        now = d["now"]
        out: list[dict[str, Any]] = []
        projects = d["projects"]
        for code, a in d["assessments"].items():
            p = next((x for x in projects.values() if x.code == code), None)
            if p is None:
                continue
            pl = a.payload
            if pl["readiness"] >= pl["threshold"]:
                continue
            gi = d["gates"].get(pl["gate_instance_id"])
            days = max(1, (gi.planned_review_at - now).days) if gi else 30
            exp = max(d["exposure"].get(p.id, {}).values(), default=50.0)
            gaps = pl.get("gaps", [])
            lead = gaps[0] if gaps else None
            out.append({"kind": "decision", "stamp": "FOR DECISION", "project": code,
                        "title": f"{code} | hold the {pl['gate']} review or move it?",
                        "why": f"Readiness {pl['readiness']:.0%} of {pl['threshold']:.0%}; "
                               + (f"{lead['name'].lower()} ({lead['commitment']}) {lead['reason']}." if lead else ""),
                        "depends": f"{code} gate {pl['gate']}", "impact": (pl["threshold"] - pl["readiness"]) * exp,
                        "urgency": 1 / max(days, 1), "blocks_gate": True,
                        "citations": [pl["gate_instance_id"]] + ([lead["commitment_id"]] if lead and lead.get("commitment_id") else [])})
        for ap in d["approvals"]:
            o = d["outputs"].get(ap.output_id or "")
            if o is None:
                continue
            c = d["commitments"].get(o.payload.get("commitment_id", ""))
            p = projects.get(c.project_id) if c else None
            if c is None or p is None:
                continue
            days = max(1, (c.due_at - now).days)
            exp = d["exposure"].get(p.id, {}).get("permits" if o.payload.get("party_kind") == "authority" else "vendors", 50.0)
            out.append({"kind": "approval", "stamp": "APPROVAL", "project": p.code,
                        "title": f"{p.code} | approve the L3 escalation to {o.payload.get('party')}?",
                        "why": f"{c.code} {c.title.lower()} - {o.payload.get('reason')}; the draft waits for you.",
                        "depends": f"{p.code} {c.title}", "impact": exp, "urgency": 1 / days,
                        "blocks_gate": bool(c.gate_instance_id), "citations": [ap.id, c.id], "approval_id": ap.id})
        for dep in d["deps"]:
            a, b = d["packages"].get(dep.from_work_package_id), d["packages"].get(dep.to_work_package_id)
            if a is None or b is None:
                continue
            pa, pb = projects.get(a.project_id), projects.get(b.project_id)
            if pa is None and pb is None:
                continue
            proj = pa or pb
            assert proj is not None
            out.append({"kind": "proposal", "stamp": "PROPOSED", "project": proj.code,
                        "title": f"{a.code} / {b.code} | {dep.kind.replace('_', ' ')}",
                        "why": dep.reason, "depends": f"{a.name} and {b.name}",
                        "impact": 20.0 + (dep.saved_days_estimate or 0), "urgency": 1 / 14, "blocks_gate": False,
                        "citations": [dep.id]})
        for c in d["commitments"].values():
            p = projects.get(c.project_id)
            if p is None or not c.mandatory or c.status not in ("open", "overdue", "flagged"):
                continue
            days = (c.due_at - now).days
            if days > 5:
                continue
            out.append({"kind": "deadline", "stamp": "DEADLINE", "project": p.code,
                        "title": f"{c.code} | {c.title} due {c.due_at:%d %b}",
                        "why": f"Mandatory for {p.code}; status {c.status}" + (", no reply yet." if not c.last_reply_at else "."),
                        "depends": f"{p.code} gate", "impact": c.weight * 3, "urgency": 1 / max(days, 1),
                        "blocks_gate": True, "citations": [c.id]})
        return out

    def run(self, snap: Snapshot) -> list[AgentOutput]:
        d = snap.data
        cands = self.candidates(snap)
        cands.sort(key=lambda x: (-(x["impact"] * x["urgency"]), not x["blocks_gate"], x["title"]))
        seen_projects: set[str] = set()
        top: list[dict[str, Any]] = []
        for c in cands:  # one item per project keeps the three distinct
            if c["project"] in seen_projects:
                continue
            seen_projects.add(c["project"])
            top.append(c)
            if len(top) == 3:
                break
        changes = []
        for e in d["events"][:40]:
            c = d["commitments"].get(e["entity_id"])
            label = c.code + " " + c.title if c else d.get("labels", {}).get(e["entity_id"]) or \
                e["entity_type"] + " " + e["entity_id"][:10]
            changes.append({"rev": "B", "what": f"{label}: {e['kind']} {e['payload_json'].get('to', '') if isinstance(e['payload_json'], dict) else ''}".strip(),
                            "at": e["at"].isoformat() if hasattr(e["at"], "isoformat") else str(e["at"]),
                            "entity_type": e["entity_type"], "entity_id": e["entity_id"]})
        runs = d["runs"]
        per_agent: dict[str, list[m.AgentRun]] = {}
        for r in runs:
            per_agent.setdefault(str(r.agent), []).append(r)
        overnight = {a: {"runs": len(rs), "summary": rs[0].summary, "failed": sum(1 for r in rs if r.outcome == "failed")}
                     for a, rs in per_agent.items()}
        inbound = d["inbound"]
        overnight["semantic"] = {"runs": overnight.get("semantic", {}).get("runs", 0),
                                 "summary": f"{len(inbound)} inbound items processed, "
                                            f"{sum(1 for i in inbound if i.review_state == 'pending')} in review",
                                 "failed": 0}
        payload = {"user": d["user"].short if d["user"] else None, "date": d["now"].date().isoformat(),
                   "items": [{k: v for k, v in x.items() if k not in ("impact", "urgency")} for x in top],
                   "changed": changes, "overnight": overnight, "not_done": d["not_done"]}
        cites = [cid for x in top for cid in x["citations"]] or [d["user"].id if d["user"] else "portfolio"]
        payload["summary"] = summary_text(top, len(changes))
        generated: list[str] = []
        drafter = getattr(self, "drafter", None)
        if drafter is not None and top:
            draft = drafter.rewrite(payload["summary"], purpose="two-sentence opening of a morning brief",
                                    facts={"items": [x["title"] for x in top], "changes": len(changes)})
            payload["summary"], payload["drafting"] = draft.text, draft.audit()
            generated = ["summary"] if draft.generated else []
        self.tokens_used = getattr(drafter, "tokens", 0)
        self.summary = f"{len(top)} focus items, {len(changes)} changes"
        return [AgentOutput(output_type="Brief", title=f"Brief {payload['date']}", payload=payload, citations=cites,
                            generated=generated)]


def summary_text(top: list[dict[str, Any]], changes: int) -> str:
    """The template opening of the brief, from the focus items only."""
    if not top:
        return f"Nothing needs a decision today; {changes} changes since yesterday's issue."
    first = top[0]["title"]
    rest = len(top) - 1
    return (f"{len(top)} item{'s' if len(top) != 1 else ''} need you today, starting with {first}"
            + (f", and {rest} more." if rest else ".") + f" {changes} changes since yesterday's issue.")


def render_text(payload: dict[str, Any]) -> str:
    lines = [f"Morning brief | {payload['date']} | for {payload.get('user') or 'portfolio lead'}", ""]
    if payload.get("summary"):
        lines += [str(payload["summary"]), ""]
    for i, it in enumerate(payload["items"], 1):
        lines += [f"{i}. [{it['stamp']}] {it['title']}", f"   {it['why']}", f"   depends: {it['depends']}"]
    lines += ["", "Changed since yesterday:"]
    lines += [f"  ▲{c['rev']} {c['what']}" for c in payload["changed"][:12]] or ["  nothing"]
    lines += ["", "Agents overnight:"]
    lines += [f"  {a}: {v['summary']}" for a, v in payload["overnight"].items()]
    lines += ["", payload["not_done"]["statement"]]
    return "\n".join(lines)
