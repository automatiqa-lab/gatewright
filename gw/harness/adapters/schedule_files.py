"""ScheduleSource adapters over files and MCP schedule servers.- `xer_file`: direct parse of XER and MSPDI files in a folder (fallback when longpath
  is unavailable).
- `longpath_mcp` / `p6_mcp`: MCP clients, in schedule_mcp.py.

Gatewright never computes CPM: float and criticality come from the file or server.
DCMA 14 is computed here only for files (longpath provides it otherwise).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from gw.harness.contracts import (
    Activity,
    Baseline,
    DCMAReport,
    ExtProject,
    Health,
    ScheduleSourceBase,
    SyncResult,
)
from gw.harness.registry import register
from gw.record import models as m

NS = {"p": "http://schemas.microsoft.com/project"}
MIN_PER_DAY = 480  # 8 h working day


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", ""))
    except ValueError:
        return None
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d


class ParsedSchedule:
    """One schedule file, normalised."""

    def __init__(self, project: ExtProject, activities: list[Activity], constraints: dict[str, int],
                 lags: dict[tuple[str, str], float], rel_types: dict[tuple[str, str], int],
                 baseline_finish: dict[str, datetime | None], durations: dict[str, float]) -> None:
        self.project = project
        self.activities = activities
        self.constraints = constraints
        self.lags = lags
        self.rel_types = rel_types
        self.baseline_finish = baseline_finish
        self.durations = durations


def parse_mspdi(path: Path) -> ParsedSchedule:
    """Parse an MSPDI (MS Project XML) file."""
    root = ET.parse(path).getroot()

    def t(el: ET.Element, tag: str) -> str | None:
        x = el.find(f"p:{tag}", NS)
        return x.text if x is not None else None

    name = t(root, "Name") or path.stem
    status = _dt(t(root, "StatusDate"))
    acts: list[Activity] = []
    constraints: dict[str, int] = {}
    lags: dict[tuple[str, str], float] = {}
    rels: dict[tuple[str, str], int] = {}
    bfin: dict[str, datetime | None] = {}
    durs: dict[str, float] = {}
    tasks = root.find("p:Tasks", NS)
    for task in tasks if tasks is not None else []:
        uid = t(task, "UID") or ""
        if uid == "0":
            continue
        preds = []
        for link in task.findall("p:PredecessorLink", NS):
            pu = t(link, "PredecessorUID") or ""
            preds.append(pu)
            lags[(pu, uid)] = float(t(link, "LinkLag") or 0) / 10 / MIN_PER_DAY
            rels[(pu, uid)] = int(t(link, "Type") or 1)
        start, finish = _dt(t(task, "Start")), _dt(t(task, "Finish"))
        acts.append(Activity(
            id=uid, name=t(task, "Name") or "", wbs=t(task, "WBS") or "", start=start, finish=finish,
            total_float=float(t(task, "TotalSlack") or 0) / 10 / MIN_PER_DAY,
            free_float=float(t(task, "FreeSlack") or 0) / 10 / MIN_PER_DAY,
            pct=float(t(task, "PercentComplete") or 0), preds=preds,
            is_milestone=(t(task, "Milestone") == "1"), is_critical=(t(task, "Critical") == "1"),
        ))
        constraints[uid] = int(t(task, "ConstraintType") or 0)
        bl = task.find("p:Baseline", NS)
        bfin[uid] = _dt(t(bl, "Finish")) if bl is not None else None
        durs[uid] = (finish - start).days if start and finish else 0
    proj = ExtProject(id=path.stem, name=name, data_date=status, baseline=any(bfin.values()))
    return ParsedSchedule(proj, acts, constraints, lags, rels, bfin, durs)


def parse_xer(path: Path) -> ParsedSchedule:
    """Parse the TASK and TASKPRED tables of a Primavera XER file (tab-separated)."""
    tables: dict[str, list[dict[str, str]]] = {}
    cur: str | None = None
    fields: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if parts[0] == "%T":
            cur = parts[1]
            tables[cur] = []
        elif parts[0] == "%F":
            fields = parts[1:]
        elif parts[0] == "%R" and cur:
            tables[cur].append(dict(zip(fields, parts[1:], strict=False)))
    proj_rows = tables.get("PROJECT", [{}])
    name = proj_rows[0].get("proj_short_name", path.stem) if proj_rows else path.stem
    status = _dt(proj_rows[0].get("last_recalc_date")) if proj_rows else None
    preds: dict[str, list[str]] = {}
    lags: dict[tuple[str, str], float] = {}
    rels: dict[tuple[str, str], int] = {}
    for r in tables.get("TASKPRED", []):
        preds.setdefault(r["task_id"], []).append(r["pred_task_id"])
        lags[(r["pred_task_id"], r["task_id"])] = float(r.get("lag_hr_cnt") or 0) / 8
        rels[(r["pred_task_id"], r["task_id"])] = 1 if r.get("pred_type", "PR_FS") == "PR_FS" else 2
    acts: list[Activity] = []
    cons: dict[str, int] = {}
    bfin: dict[str, datetime | None] = {}
    durs: dict[str, float] = {}
    for r in tables.get("TASK", []):
        tid = r["task_id"]
        start = _dt(r.get("target_start_date"))
        finish = _dt(r.get("target_end_date"))
        tf = float(r.get("total_float_hr_cnt") or 0) / 8
        acts.append(Activity(id=tid, name=r.get("task_name", ""), wbs=r.get("wbs_id", ""), start=start,
                             finish=finish, total_float=tf, free_float=float(r.get("free_float_hr_cnt") or 0) / 8,
                             pct=float(r.get("phys_complete_pct") or 0), preds=preds.get(tid, []),
                             is_milestone=r.get("task_type") in ("TT_Mile", "TT_FinMile"), is_critical=tf <= 0))
        cons[tid] = 0 if not r.get("cstr_type") else 2
        bfin[tid] = None
        durs[tid] = float((finish - start).days) if start and finish else 0.0
    return ParsedSchedule(ExtProject(id=path.stem, name=name, data_date=status), acts, cons, lags, rels, bfin, durs)


def dcma14(s: ParsedSchedule) -> DCMAReport:
    """The fourteen DCMA checks over a parsed schedule. Checks needing CPM pass as 'not tested'."""
    work = [a for a in s.activities if not a.is_milestone] or s.activities
    n = max(len(work), 1)
    succs: dict[str, int] = {}
    for a in s.activities:
        for p in a.preds:
            succs[p] = succs.get(p, 0) + 1
    status = s.project.data_date or datetime.now(UTC)
    failing = []
    checks = {
        "logic": sum(1 for a in work if not a.preds or not succs.get(a.id)) / n <= 0.05 or len(work) < 3,
        "leads": not any(v < 0 for v in s.lags.values()),
        "lags": sum(1 for v in s.lags.values() if v > 0) / max(len(s.lags), 1) <= 0.05,
        "relationship_types": sum(1 for v in s.rel_types.values() if v == 1) / max(len(s.rel_types), 1) >= 0.9,
        "hard_constraints": sum(1 for v in s.constraints.values() if v in (2, 3, 6, 7)) / n <= 0.05,
        "high_float": sum(1 for a in work if a.total_float > 44) / n <= 0.05,
        "negative_float": not any(a.total_float < 0 for a in s.activities),
        "high_duration": sum(1 for a in work if s.durations.get(a.id, 0) > 44 * 7 / 5) / n <= 0.05,
        "invalid_dates": not any(a.pct > 0 and a.start is not None and a.start > status for a in work),
        "resources": True,
        "missed_tasks": sum(1 for a in work if (s.baseline_finish.get(a.id) or status) < status and a.pct < 100) / n <= 0.05,
        "critical_path_test": True,
        "cpli": True,
        "bei": True,
    }
    for k, ok in checks.items():
        if not ok:
            failing.append(k)
    return DCMAReport(passed=14 - len(failing), total=14, failing=failing)


def read_schedule(path: Path) -> ParsedSchedule:
    return parse_xer(path) if path.suffix.lower() == ".xer" else parse_mspdi(path)


@register("xer_file")
class XerFileAdapter(ScheduleSourceBase):
    """Reads every .xer and .xml file in `config.folder`; one file = one external project."""

    def __init__(self, conn: m.Connection) -> None:
        self.folder = Path(conn.config.get("folder", "./drop/schedules"))
        self._cache: dict[str, ParsedSchedule] = {}

    def _files(self) -> dict[str, Path]:
        if not self.folder.exists():
            return {}
        return {p.stem: p for p in sorted(self.folder.iterdir()) if p.suffix.lower() in (".xer", ".xml")}

    def _get(self, project: str) -> ParsedSchedule:
        if project not in self._cache:
            self._cache[project] = read_schedule(self._files()[project])
        return self._cache[project]

    def list_projects(self) -> list[ExtProject]:
        return [self._get(k).project for k in self._files()]

    def get_activities(self, project: str) -> list[Activity]:
        return self._get(project).activities

    def get_milestones(self, project: str) -> list[Activity]:
        return [a for a in self.get_activities(project) if a.is_milestone]

    def get_baseline(self, project: str) -> Baseline | None:
        s = self._get(project)
        fins = [f for f in s.baseline_finish.values() if f]
        return Baseline(project=project, finish=max(fins)) if fins else None

    def dcma14(self, project: str) -> DCMAReport | None:
        return dcma14(self._get(project))

    def sync(self, since: str | None) -> SyncResult:
        self._cache.clear()
        files = self._files()
        return SyncResult(pulled=len(files), changed=len(files), cursor=datetime.now(UTC).isoformat())

    def health(self) -> Health:
        if not self.folder.exists():
            return Health(status="down", detail=f"folder {self.folder} missing")
        return Health(status="ok", detail=f"{len(self._files())} schedule files")
