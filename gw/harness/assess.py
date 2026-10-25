"""Should a dependency-carrying plan be treated as a schedule?

Planner premium plans (Project for the web, read by `msproject_graph` through Dataverse) carry
dependencies but no float of their own. Promotion from PlanSource to ScheduleSource would feed
float and DCMA signals, so it must rest on measured data quality, never on the tool's label.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from gw.harness.contracts import Activity

THRESHOLDS = {"logic_coverage": 0.9, "dangling_share": 0.02, "float_coverage": 0.8, "dated_share": 0.95}


@dataclass
class Assessment:
    project: str
    activities: int
    logic_coverage: float  # share of non-milestone activities with a predecessor or successor
    dangling_share: float  # share of links pointing at unknown activities
    cycles: int
    float_coverage: float  # share of activities with a total float value reported by the source
    dated_share: float  # share with start and finish
    promote: bool
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cycles(acts: list[Activity]) -> int:
    graph = {a.id: [p for p in a.preds] for a in acts}
    colour: dict[str, int] = {}
    found = 0

    def visit(n: str) -> None:
        nonlocal found
        colour[n] = 1
        for p in graph.get(n, []):
            if p not in graph:
                continue
            if colour.get(p) == 1:
                found += 1
            elif colour.get(p) is None:
                visit(p)
        colour[n] = 2

    for n in graph:
        if colour.get(n) is None:
            visit(n)
    return found


def assess(project: str, acts: list[Activity], float_reported: bool) -> Assessment:
    """Measure logic, links, cycles, float and dates; recommend promotion only when every bar is met."""
    ids = {a.id for a in acts}
    work = [a for a in acts if not a.is_milestone] or acts
    succ: dict[str, int] = {}
    links = dangling = 0
    for a in acts:
        for p in a.preds:
            links += 1
            if p not in ids:
                dangling += 1
            succ[p] = succ.get(p, 0) + 1
    n = max(len(work), 1)
    logic = sum(1 for a in work if a.preds or succ.get(a.id)) / n
    dated = sum(1 for a in acts if a.start and a.finish) / max(len(acts), 1)
    fl = (sum(1 for a in acts if a.total_float or a.is_critical) / max(len(acts), 1)) if float_reported else 0.0
    cyc = _cycles(acts)
    reasons = []
    if logic < THRESHOLDS["logic_coverage"]:
        reasons.append(f"logic coverage {logic:.0%} < {THRESHOLDS['logic_coverage']:.0%}")
    if links and dangling / links > THRESHOLDS["dangling_share"]:
        reasons.append(f"{dangling} of {links} links point at unknown tasks")
    if cyc:
        reasons.append(f"{cyc} dependency cycles")
    if fl < THRESHOLDS["float_coverage"]:
        reasons.append("no float reported by the source (Gatewright never computes CPM; longpath would)")
    if dated < THRESHOLDS["dated_share"]:
        reasons.append(f"only {dated:.0%} of tasks dated")
    return Assessment(project, len(acts), round(logic, 3), round(dangling / links, 3) if links else 0.0, cyc,
                      round(fl, 3), round(dated, 3), promote=not reasons, reasons=reasons)


def assess_connection(registry: Any, conn_id: str) -> list[dict[str, Any]]:
    """Assess every project of a ScheduleSource-shaped connection (e.g. msproject_graph)."""
    src = registry.get(conn_id)
    float_reported = bool(getattr(src.adapter, "reports_float", False))
    return [assess(p.id, src.get_activities(p.id), float_reported).as_dict() for p in src.list_projects()]
