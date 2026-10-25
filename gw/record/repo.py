"""State machines and invariants of the record.

and (invariants). Every
function writes the entity and its event in one transaction through the Store.
"""

from __future__ import annotations

from datetime import datetime

from gw.errors import Conflict, NotAuthorised
from gw.record import clock
from gw.record import models as m
from gw.record.store import Store

S = m.CommitmentStatus

# Commitment.status
COMMITMENT_TRANSITIONS: dict[str, set[str]] = {
    S.open: {S.confirmed, S.flagged, S.overdue, S.submitted, S.waived, S.cancelled},
    S.confirmed: {S.overdue, S.submitted, S.waived, S.cancelled, S.flagged},
    S.flagged: {S.open, S.overdue, S.submitted, S.waived, S.cancelled, S.confirmed},
    S.overdue: {S.submitted, S.waived, S.cancelled, S.confirmed, S.flagged},
    S.submitted: {S.accepted, S.open, S.waived, S.cancelled},
    S.accepted: {S.waived, S.cancelled},
    S.waived: set(),
    S.cancelled: set(),
}
ESCALATION_ACTIVE = {S.open, S.flagged, S.overdue}
ESCALATION_RESET = {S.confirmed, S.submitted, S.accepted}
CLOSED = {S.accepted, S.waived, S.cancelled}


def has_role(user: m.User, allowed: list[str] | set[str]) -> bool:
    """True when the user holds any allowed role. `sme:<x>` matches exactly; admin is not a wildcard."""
    return bool(set(user.roles) & set(allowed))


def set_commitment_status(store: Store, c: m.Commitment, status: str, actor: str,
                          reason: str | None = None, new_due: datetime | None = None,
                          agent_run_id: str | None = None) -> m.Commitment:
    """Apply one transition of the commitment state machine."""
    current = str(c.status)
    if status == current and status != S.flagged:
        return c
    allowed = COMMITMENT_TRANSITIONS[current]
    if status not in allowed:
        raise Conflict(f"{c.code}: {current} -> {status} is not allowed", sorted(allowed))
    if status == S.waived and c.mandatory:
        raise Conflict(f"{c.code}: mandatory commitments cannot be waived", sorted(allowed - {S.waived}))
    payload: dict[str, object] = {"from": current, "to": status}
    if reason:
        payload["reason"] = reason
    if status == S.flagged and new_due is not None:
        payload["new_due"] = new_due.isoformat()
        c.due_at = new_due
    c.status = m.CommitmentStatus(status)
    if status in ESCALATION_RESET:
        c.escalation_level = 0
        c.first_unanswered_chase_at = None
    if status in CLOSED:
        c.closed_at = clock.now()
    store.update(c, actor=actor, kind="status", payload=payload, agent_run_id=agent_run_id)
    return c


def waive_commitment(store: Store, c: m.Commitment, user: m.User, reason: str) -> m.Commitment:
    """Waive a non-mandatory commitment; needs project_manager."""
    if not has_role(user, {"project_manager", "portfolio_lead"}):
        raise NotAuthorised(f"{c.code}: waiving needs project_manager")
    return set_commitment_status(store, c, S.waived, f"user:{user.id}", reason=reason)


def create_gate_instance(store: Store, gi: m.GateInstance, actor: str = "system") -> m.GateInstance:
    """Create a gate instance; one pending per project, previous must have passed."""
    existing = store.find(m.GateInstance, project_id=gi.project_id)
    if gi.decision == m.GateDecision.pending and any(
        x.decision == m.GateDecision.pending for x in existing
    ):
        raise Conflict(f"project {gi.project_id} already has a pending gate instance", ["decide the current gate first"])
    earlier = [x for x in existing if x.order < gi.order]
    if earlier:
        prev = max(earlier, key=lambda x: x.order)
        if prev.decision not in (m.GateDecision.passed, m.GateDecision.pass_with_actions):
            raise Conflict(f"{gi.gate_id}: previous gate {prev.gate_id} has not passed", ["pass", "pass_with_actions"])
    store.insert(gi, actor=actor)
    return gi


def accept_roles_for(store: Store, c: m.Commitment) -> list[str]:
    """accept_roles of the commitment's deliverable, copied on its gate instance."""
    if c.gate_instance_id:
        gi = store.get(m.GateInstance, c.gate_instance_id)
        for d in gi.deliverables:
            if d.id == c.deliverable_id:
                return list(d.accept_roles)
    return ["project_manager"]


def link_evidence(store: Store, c: m.Commitment, ev: m.Evidence, actor: str,
                  agent_run_id: str | None = None) -> m.Evidence:
    """Link evidence and move the commitment to submitted."""
    store.insert(ev, actor=actor, agent_run_id=agent_run_id)
    c.evidence_id = ev.id
    set_commitment_status(store, c, S.submitted, actor, agent_run_id=agent_run_id)
    return ev


def decide_evidence(store: Store, ev: m.Evidence, user: m.User, accept: bool,
                    reason: str | None = None) -> m.Commitment:
    """Accept or reject evidence; the user must hold a role in accept_roles."""
    c = store.get(m.Commitment, ev.commitment_id)
    roles = accept_roles_for(store, c)
    if not has_role(user, roles):
        raise NotAuthorised(f"{c.code}: accepting needs one of {roles}")
    actor = f"user:{user.id}"
    if accept:
        ev.accepted_by_user_id = user.id
        ev.accepted_at = clock.now()
        ev.rejected_reason = None
        store.update(ev, actor=actor, kind="accepted")
        return set_commitment_status(store, c, S.accepted, actor)
    ev.rejected_reason = reason or "rejected"
    store.update(ev, actor=actor, kind="rejected", payload={"reason": ev.rejected_reason})
    return set_commitment_status(store, c, S.open, actor, reason=ev.rejected_reason)


GATE_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"pass", "pass_with_actions", "hold", "stop"},
    "hold": {"pending"},
}


def decide_gate(store: Store, gi: m.GateInstance, user: m.User, decision: str,
                note: str | None = None) -> m.GateInstance:
    """Stamp a gate decision. The user must hold a role in the model's `decided_by`."""
    allowed = GATE_TRANSITIONS.get(str(gi.decision), set())
    if decision not in allowed or (decision != "pending" and decision not in gi.decisions_allowed):
        raise Conflict(f"{gi.gate_id}: {gi.decision} -> {decision} not allowed",
                       sorted(allowed & (set(gi.decisions_allowed) | {"pending"})))
    if not has_role(user, gi.decided_by_roles):  # reopening a HOLD is a decision too
        raise NotAuthorised(f"{gi.gate_id}: decision needs one of {gi.decided_by_roles}")
    gi.decision = m.GateDecision(decision)
    gi.decided_by_user_id = user.id
    gi.decided_at = clock.now()
    gi.decision_note = note
    if decision != "pending":
        gi.actual_review_at = clock.now()
    store.update(gi, actor=f"user:{user.id}", kind="decision", payload={"decision": decision, "note": note})
    return gi
