"""Agent shape and runner.intro and

Agents are pure: `run(snapshot) -> outputs`. The runner creates the AgentRun,
validates citations, passes each output through `governance.decide`, applies or
queues it, and closes the run with cost and outcome. Agents never send, write or
approve themselves.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from gw.governance import core as gov
from gw.policies.store import Policies
from gw.policies.store import current as current_policies
from gw.record import clock
from gw.record import models as m
from gw.record.ids import new_id
from gw.record.store import Store


@dataclass
class AgentOutput:
    """One output of one run. `citations` are entity ids; an output without them fails validation."""

    output_type: str
    payload: dict[str, Any]
    citations: list[str]
    title: str = ""
    hard_limited: bool = False  # e.g. L3 to an authority: never above draft
    approval_kind: str | None = None
    apply: Callable[[Store, m.AgentRun, m.AgentOutput], Any] | None = None
    on_draft: Callable[[Store, m.AgentRun, m.AgentOutput, str | None], Any] | None = None
    generated: list[str] = field(default_factory=list)  # payload keys a language model wrote (Art. 50)


@dataclass
class Snapshot:
    seq: int
    policies: Policies
    data: dict[str, Any] = field(default_factory=dict)


class Agent(Protocol):
    name: str
    output_type: str

    def snapshot(self, store: Store, policies: Policies, **kw: Any) -> Snapshot: ...
    def run(self, snap: Snapshot) -> list[AgentOutput]: ...


@dataclass
class RunResult:
    run: m.AgentRun
    outputs: list[m.AgentOutput]
    approvals: list[m.Approval]


def run_agent(store: Store, agent: Agent, trigger: str = "manual", **kw: Any) -> RunResult:
    """Run one agent under governance and record everything as an AgentRun."""
    policies = current_policies(store)
    started = clock.now()
    t0 = time.perf_counter()
    run = m.AgentRun(id=new_id("AgentRun"), agent=m.AgentName(agent.name), trigger=trigger, started_at=started,
                     input_snapshot_ref=store.last_seq(), output_type=agent.output_type,
                     autonomy_level=m.AutonomyLevel(policies.autonomy(agent.name)
                                                    if agent.name in ("chaser", "gatekeeper", "scout", "tender", "brief", "reporter")
                                                    else "act"))
    store.insert(run, actor=f"agent:{agent.name}", kind="started", agent_run_id=run.id)
    stored: list[m.AgentOutput] = []
    approvals: list[m.Approval] = []
    try:
        snap = agent.snapshot(store, policies, **kw)
        run.input_snapshot_ref = snap.seq
        outputs = agent.run(snap)
        budget_s = float(getattr(agent, "seconds_budget", 0) or policies.get("agents.budgets.seconds", 120))
        for out in outputs:
            if not out.citations:
                raise ValueError(f"{agent.name}: output {out.output_type} has no citations")
            disposition = gov.decide(policies, agent.name, out.output_type, out.hard_limited)
            ao = m.AgentOutput(id=new_id("AgentOutput"), agent_run_id=run.id, agent=m.AgentName(agent.name),
                               output_type=out.output_type, disposition=disposition,
                               payload={**out.payload, "title": out.title,
                                        **({"_generated": out.generated} if out.generated else {})},
                               citations=out.citations,
                               created_at=clock.now())
            store.insert(ao, actor=f"agent:{agent.name}", agent_run_id=run.id)
            stored.append(ao)
            if disposition == "apply" and out.apply:
                out.apply(store, run, ao)
            elif disposition in ("draft", "propose"):
                ap = None
                if disposition == "draft" and out.approval_kind:
                    ap = gov.request_approval(store, run, out.approval_kind, out.title, ao.id)
                    approvals.append(ap)
                if out.on_draft:
                    out.on_draft(store, run, ao, ap.id if ap else None)
            if time.perf_counter() - t0 > budget_s:
                raise TimeoutError(f"{agent.name}: run exceeded {budget_s:.0f} s budget")
        dispositions = {o.disposition for o in stored}
        run.outcome = m.RunOutcome(
            "applied" if "apply" in dispositions else "drafted" if "draft" in dispositions
            else "proposed" if "propose" in dispositions else "applied")
        run.needs_approval = bool(approvals)
        run.approval_id = approvals[0].id if approvals else None
        run.output_ref = stored[0].id if stored else None
        run.summary = getattr(agent, "summary", "") or f"{len(stored)} outputs"
    except Exception as e:
        run.outcome = m.RunOutcome.failed
        run.error = f"{type(e).__name__}: {e}"[:500]
        run.summary = "failed"
    run.finished_at = clock.now()
    run.cost_seconds = round(time.perf_counter() - t0, 3)
    run.cost_tokens = int(getattr(agent, "tokens_used", 0))
    store.update(run, actor=f"agent:{agent.name}", kind="finished", agent_run_id=run.id,
                 payload={"outcome": run.outcome, "outputs": len(stored)})
    return RunResult(run, stored, approvals)
