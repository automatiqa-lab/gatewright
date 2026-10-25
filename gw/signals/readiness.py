"""Gate readiness, recommendation and gap list.

Readiness is a pure function of the gate instance's copied deliverables and the
commitments that carry them; it never reads the live gate model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gw.record import clock
from gw.record import models as m
from gw.record.store import Store
from gw.standards.checks import CheckContext, evaluate_check

S = m.CommitmentStatus


@dataclass
class DeliverableScore:
    deliverable: m.DeliverableSpec
    commitment: m.Commitment | None
    credit: float  # accepted(d)
    included: bool  # False when waived
    check_result: bool | None = None
    reason: str = ""


@dataclass
class GateScore:
    gate_instance_id: str
    readiness: float
    threshold: float
    recommendation: str
    items: list[DeliverableScore] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate_instance_id": self.gate_instance_id,
            "readiness": round(self.readiness, 4),
            "threshold": self.threshold,
            "recommendation": self.recommendation,
            "gaps": self.gaps,
        }


def credit(status: str, submitted_counts_as: float) -> float:
    """accepted(d)"""
    if status == S.accepted:
        return 1.0
    if status == S.submitted:
        return submitted_counts_as
    return 0.0


def _blocking_reason(c: m.Commitment | None, ev: m.Evidence | None, check: bool | None) -> str:
    if c is None:
        return "no commitment recorded"
    if check is False:
        return "check failed"
    if c.status == S.flagged:
        return f"flagged with date {c.due_at:%d %b}"
    if ev is not None and ev.rejected_reason:
        return f"evidence rejected: {ev.rejected_reason}"
    if c.status == S.submitted:
        return "evidence linked, not yet accepted"
    return "no evidence"


def score_gate(
    gi: m.GateInstance,
    commitments: list[m.Commitment],
    ctx: CheckContext,
    submitted_counts_as: float = 0.5,
    evidence: dict[str, m.Evidence] | None = None,
    parties: dict[str, m.Party] | None = None,
    p_slip: dict[str, float] | None = None,
    now: datetime | None = None,
) -> GateScore:
    """Readiness, recommendation and ordered gap list for one gate instance."""
    now = now or clock.now()
    evidence = evidence or {}
    parties = parties or {}
    p_slip = p_slip or {}
    by_deliv: dict[str, m.Commitment] = {}
    for cm in commitments:
        if cm.kind == m.CommitmentKind.gate_deliverable and cm.gate_instance_id == gi.id:
            by_deliv[cm.deliverable_id] = cm

    items: list[DeliverableScore] = []
    num = den = 0.0
    for d in gi.deliverables:
        c = by_deliv.get(d.id)
        status = str(c.status) if c else S.open
        if status == S.waived and not d.mandatory:
            items.append(DeliverableScore(d, c, 0.0, included=False, reason="waived"))
            continue
        cr = credit(status, submitted_counts_as)
        chk: bool | None = None
        if d.check:
            ev = evidence.get(c.evidence_id) if c and c.evidence_id else None
            ctx.commitment = {"status": status, "escalation_level": c.escalation_level if c else 0,
                              "chase_count": c.chase_count if c else 0,
                              "days_to_due": (c.due_at - now).days if c else 0}
            ctx.evidence_kinds = {str(ev.kind)} if ev else set()
            chk = evaluate_check(d.check, ctx)
            if chk is False:
                cr = 0.0
        items.append(DeliverableScore(d, c, cr, included=True, check_result=chk))
        num += d.weight * cr
        den += d.weight
    readiness = num / den if den else 0.0

    included = [i for i in items if i.included]
    mandatory_ok = all(i.credit >= 1.0 for i in included if i.deliverable.mandatory)
    optional_missing = any(i.credit < 1.0 for i in included if not i.deliverable.mandatory)
    if readiness >= gi.threshold and mandatory_ok:
        rec = "pass_with_actions" if optional_missing else "pass"
    else:
        rec = "hold"

    gaps: list[dict[str, Any]] = []
    for i in included:
        if i.credit >= 1.0:
            continue
        c = i.commitment
        ev = evidence.get(c.evidence_id) if c and c.evidence_id else None
        party = parties.get(c.party_id) if c else None
        i.reason = _blocking_reason(c, ev, i.check_result)
        gaps.append({
            "deliverable_id": i.deliverable.id,
            "name": i.deliverable.name,
            "weight": i.deliverable.weight,
            "mandatory": i.deliverable.mandatory,
            "credit": i.credit,
            "commitment": c.code if c else None,
            "commitment_id": c.id if c else None,
            "status": str(c.status) if c else "missing",
            "party": party.name if party else None,
            "party_kind": str(party.kind) if party else None,
            "due_at": c.due_at.isoformat() if c else None,
            "days_to_due": (c.due_at.date() - now.date()).days if c else None,
            "last_chase_at": c.last_chase_at.isoformat() if c and c.last_chase_at else None,
            "reason": i.reason,
            "p_slip": p_slip.get(c.id) if c else None,
            "tags": i.deliverable.tags,
        })
    gaps.sort(key=lambda g: (not g["mandatory"], -float(g["weight"]), str(g["due_at"] or "9999")))
    return GateScore(gi.id, readiness, gi.threshold, rec, items, gaps)


def check_context(store: Store, project: m.Project) -> CheckContext:
    """Values for check expressions; dcma14 is None for projects without a schedule (never fabricated)."""
    snaps = store.find(m.ScheduleSnapshot, project_id=project.id, order_by="-synced_at", limit=1)
    dcma = {"passed": snaps[0].dcma_passed, "total": 14} if snaps and snaps[0].dcma_passed is not None else None
    return CheckContext(
        project={"estimate_class": project.estimate_class, "capex_amount": project.capex_amount,
                 "contingency_amount": project.contingency_amount,
                 "contingency_drawn": project.contingency_drawn, "archetype": str(project.archetype),
                 "country": project.country, "zone": project.zone},
        dcma14=dcma,
        estimate={"class_": project.estimate_class},
        today=clock.today(),
    )


def score_instance(store: Store, gi: m.GateInstance, submitted_counts_as: float = 0.5,
                   p_slip: dict[str, float] | None = None) -> GateScore:
    """Load what the readiness function needs from the record and score."""
    project = store.get(m.Project, gi.project_id)
    cs = store.find(m.Commitment, gate_instance_id=gi.id)
    ev_ids = [c.evidence_id for c in cs if c.evidence_id]
    evidence = {e.id: e for e in store.find(m.Evidence, id=ev_ids)} if ev_ids else {}
    party_ids = list({c.party_id for c in cs})
    parties = {p.id: p for p in store.find(m.Party, id=party_ids)} if party_ids else {}
    return score_gate(gi, cs, check_context(store, project), submitted_counts_as,
                      evidence, parties, p_slip)
