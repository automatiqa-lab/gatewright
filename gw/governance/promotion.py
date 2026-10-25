"""Autonomy promotion proposals.

Promotion is never automatic: when an agent's automation readiness has stayed above
`agents.promotion.requires_readiness_above` for `for_weeks` consecutive weeks, a pending
Approval of kind `policy_change` proposes raising that agent's autonomy by at most `max_step`
levels, never above the level its outputs can use. A portfolio lead approving it applies the
change through the normal policy revision path.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from gw.governance.core import LEVELS
from gw.policies.store import current as current_policies
from gw.policies.store import current_revision
from gw.record import clock
from gw.record import models as m
from gw.record.ids import new_id
from gw.record.store import Store

# Highest autonomy an agent's outputs can use (output caps in governance.core /).
AGENT_CAP = {"chaser": "act", "gatekeeper": "draft", "scout": "propose", "tender": "draft", "brief": "act",
             "reporter": "act"}


def weeks_above(store: Store, agent: str, threshold: float, weeks: int) -> bool:
    """True when every snapshot in the last `weeks` weeks is above threshold and each week has one."""
    now = clock.now()
    rows = [r for r in store.signal_history("automation_readiness", "agent", agent, now - timedelta(weeks=weeks))]
    if not rows:
        return False
    covered = {(now - r.computed_at).days // 7 for r in rows}
    return all(r.value > threshold for r in rows) and covered >= set(range(weeks))


def raise_autonomy_yaml(text: str, agent: str, new_level: str) -> str:
    """Change one line under agents.autonomy, keeping the rest of the document (comments included)."""
    pattern = re.compile(rf"(?m)^(\s+{re.escape(agent)}:\s*)(observe|propose|draft|act)\b")
    block = text.split("autonomy:", 1)
    if len(block) != 2 or not pattern.search(block[1]):
        raise ValueError(f"agents.autonomy.{agent} not found in policies")
    return block[0] + "autonomy:" + pattern.sub(lambda mt: mt.group(1) + new_level, block[1], count=1)


def propose(store: Store) -> list[m.Approval]:
    """Create at most one pending policy_change proposal per eligible agent."""
    pol = current_policies(store)
    threshold = float(pol.get("agents.promotion.requires_readiness_above", 0.85))
    weeks = int(pol.get("agents.promotion.for_weeks", 4))
    step = int(pol.get("agents.promotion.max_step", 1))
    pending = {o.payload.get("agent") for o in store.find(m.AgentOutput, output_type="PolicyChangeProposal")
               if any(a.decision == "pending" for a in store.find(m.Approval, output_id=o.id))}
    out: list[m.Approval] = []
    for agent, cap in AGENT_CAP.items():
        cur = pol.autonomy(agent)
        if agent in pending or LEVELS.index(cur) >= LEVELS.index(cap):
            continue
        if not weeks_above(store, agent, threshold, weeks):
            continue
        new = LEVELS[min(LEVELS.index(cur) + step, LEVELS.index(cap))]
        text = raise_autonomy_yaml(current_revision(store).yaml_text, agent, new)
        now = clock.now()
        run = m.AgentRun(id=new_id("AgentRun"), agent=m.AgentName(agent), trigger="schedule", started_at=now,
                         finished_at=now, input_snapshot_ref=store.last_seq(), output_type="PolicyChangeProposal",
                         autonomy_level=m.AutonomyLevel.draft, needs_approval=True, outcome=m.RunOutcome.drafted,
                         summary=f"propose {agent} autonomy {cur} -> {new}")
        store.insert(run, actor="system:governance", agent_run_id=run.id)
        payload: dict[str, Any] = {"agent": agent, "from": cur, "to": new, "yaml": text, "threshold": threshold,
                                   "weeks": weeks, "title": f"Promote {agent}: {cur} -> {new}"}
        ao = m.AgentOutput(id=new_id("AgentOutput"), agent_run_id=run.id, agent=m.AgentName(agent),
                           output_type="PolicyChangeProposal", disposition="draft", payload=payload,
                           citations=[run.id], created_at=now)
        store.insert(ao, actor="system:governance", agent_run_id=run.id)
        ap = m.Approval(id=new_id("Approval"), agent_run_id=run.id, output_id=ao.id, kind=m.ApprovalKind.policy_change,
                        title=payload["title"], requested_at=now)
        store.insert(ap, actor="system:governance", agent_run_id=run.id)
        run.approval_id = ap.id
        store.update(run, actor="system:governance", kind="finished", agent_run_id=run.id)
        out.append(ap)
    return out
