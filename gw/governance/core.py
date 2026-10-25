"""Governance: roles, autonomy, approvals, automation readiness, audit.

Reads policies; the three hard limits are code
(here, in `gw.harness.registry` and in the model gateway), not YAML.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from gw.errors import NotAuthorised
from gw.policies.store import Policies
from gw.record import clock
from gw.record import models as m
from gw.record.ids import new_id
from gw.record.store import Store

LEVELS = ["observe", "propose", "draft", "act"]

# Output types that never act on their own, whatever the agent's autonomy.
MAX_LEVEL_FOR_OUTPUT = {
    "EscalationDraft": "draft",
    "WarRoomItem": "draft",
    "ReviewPackDraft": "draft",
    "GateAssessment": "draft",
    "DependencyProposal": "propose",
    "PackageCheck": "draft",
    "BidComparison": "draft",
}
DISPOSITION = {"observe": "observe", "propose": "propose", "draft": "draft", "act": "apply"}


def require(user: m.User | None, roles: list[str] | set[str], what: str) -> m.User:
    """Raise NotAuthorised unless the user holds one of the roles. Admin is not a wildcard."""
    if user is None:
        raise NotAuthorised(f"{what}: no principal")
    if not set(user.roles) & set(roles):
        raise NotAuthorised(f"{what}: needs one of {sorted(roles)}; {user.name} holds {user.roles}")
    return user


def decide(policies: Policies, agent: str, output_type: str, hard_limited: bool = False) -> str:
    """Autonomy check: apply | draft | propose | observe."""
    level = policies.autonomy(agent)
    cap = MAX_LEVEL_FOR_OUTPUT.get(output_type)
    if cap and LEVELS.index(level) > LEVELS.index(cap):
        level = cap
    if hard_limited and LEVELS.index(level) > LEVELS.index("draft"):
        level = "draft"
    return DISPOSITION[level]


def request_approval(store: Store, run: m.AgentRun, kind: str, title: str, output_id: str | None) -> m.Approval:
    ap = m.Approval(id=new_id("Approval"), agent_run_id=run.id, output_id=output_id, kind=m.ApprovalKind(kind),
                    title=title, requested_at=clock.now())
    store.insert(ap, actor=f"agent:{run.agent}", agent_run_id=run.id)
    return ap


def readiness_score(store: Store, policies: Policies, agent: str | None = None) -> dict[str, Any]:
    """Automation readiness: approved_unchanged / all decided, trailing window."""
    window = int(policies.get("readiness_score.window_days", 30))
    since = clock.now() - timedelta(days=window)
    runs = {r.id: r for r in store.all(m.AgentRun)}
    counts: dict[str, list[int]] = {}
    for ap in store.all(m.Approval):
        if ap.decision == "pending" or not ap.decided_at or ap.decided_at < since:
            continue
        a = str(runs[ap.agent_run_id].agent) if ap.agent_run_id in runs else "unknown"
        c = counts.setdefault(a, [0, 0])
        c[1] += 1
        if ap.decision == "approved":
            c[0] += 1
    def score(c: list[int]) -> float | None:
        return round(c[0] / c[1], 2) if c[1] else None
    per = {a: score(c) for a, c in counts.items()}
    tot = [sum(c[0] for c in counts.values()), sum(c[1] for c in counts.values())]
    if agent:
        return {"agent": agent, "score": per.get(agent), "window_days": window}
    return {"overall": score(tot), "per_agent": per, "window_days": window, "decided": tot[1]}


def audit(store: Store, entity: str | None = None, agent: str | None = None, user: str | None = None,
          since_days: int | None = 7, limit: int = 200) -> dict[str, Any]:
    """Events and agent runs ( audit)."""
    since = clock.now() - timedelta(days=since_days) if since_days else None
    actor = f"agent:{agent}" if agent else (f"user:{user}" if user else None)
    events = store.events(entity_id=entity, actor=actor, since=since, limit=limit)
    runs = [r for r in store.find(m.AgentRun, order_by="-started_at", limit=limit)
            if (agent is None or r.agent == agent) and (since is None or r.started_at >= since)]
    if entity and not agent:
        run_ids = {e["agent_run_id"] for e in events if e.get("agent_run_id")}
        runs = [r for r in runs if r.id in run_ids]
    return {"events": events, "runs": [r.model_dump(mode="json") for r in runs]}
