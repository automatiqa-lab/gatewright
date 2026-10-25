"""ScheduleSource over the schedule MCP servers longpath and p6-mcp.
Enforces hard limit 1: never write to a schedule tool.

Tool names and arguments follow the servers as measured on 24 Oct 2026:
- `longpath_mcp`: longpath 0.1.0 (S-CurveLabs, MIT) reads XER, MSPDI and P6 XML; JSON text results.
- `p6_mcp`: p6-mcp 1.0.2 (MIT) reads XER only; structured results, paged `items`.

Both servers take a schedule file per call, so a project id is `<file name>#<project>` and the files
come from `config.folder`. Hard limit 1 is enforced here, before anything is sent: only the
read-only tools in `READ_TOOLS` can be called, arguments that make a server write a file are refused,
and the tool names cannot be remapped by connection config. p6-mcp lists write and export tools even
under `--read-only`, so the allowlist, not the server flag, is the guard.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gw.errors import Forbidden
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

WRITE_ARGS = frozenset({"output_path", "csv_path", "confirm", "overwrite", "allow_writes", "dry_run", "output_dir"})
PAGE = 1000


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace(" ", "T"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _num(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


class _McpSchedule(ScheduleSourceBase):
    """Common part: file discovery, the read-only gate and the transport. `call` is injectable for tests."""

    READ_TOOLS: frozenset[str] = frozenset()
    SUFFIXES: tuple[str, ...] = ()

    def __init__(self, conn: m.Connection, call: Any = None) -> None:
        self.conn = conn
        if conn.config.get("tools"):
            raise Forbidden(f"{conn.id}: schedule MCP tool names are fixed; config.tools is not accepted",
                            limit="no_schedule_writes")
        self.folder = Path(str(conn.config.get("folder") or "."))
        self._call = call

    def call(self, tool: str, args: dict[str, Any]) -> Any:
        """Call one read-only tool. Anything else is refused before it reaches the server."""
        if tool not in self.READ_TOOLS:
            raise Forbidden(f"{self.conn.id}: {tool} is not a read-only schedule tool", limit="no_schedule_writes")
        if WRITE_ARGS & set(args):
            raise Forbidden(f"{self.conn.id}: {tool} called with a file-writing argument", limit="no_schedule_writes")
        if self._call is not None:
            out = self._call(tool, args)
        else:
            from gw.harness.adapters.mcp_client import call_tool

            out = call_tool(self.conn.config, tool, args)
        if isinstance(out, dict) and out.get("error"):
            raise RuntimeError(f"{self.conn.id}: {tool}: {out['error']}")
        return out

    def files(self) -> list[Path]:
        return sorted(p for p in self.folder.glob("*") if p.suffix.lower() in self.SUFFIXES)

    def _split(self, project: str) -> tuple[str, str]:
        name, _, short = project.partition("#")
        path = (self.folder / name).resolve()
        if path.parent != self.folder.resolve():
            raise Forbidden(f"{self.conn.id}: {name} is outside the schedule folder", limit="scope")
        return str(path), short

    def get_milestones(self, project: str) -> list[Activity]:
        return [a for a in self.get_activities(project) if a.is_milestone]

    def sync(self, since: str | None) -> SyncResult:
        return SyncResult(pulled=len(self.list_projects()), cursor=datetime.now(UTC).isoformat())

    def health(self) -> Health:
        if not self.folder.exists():
            return Health(status="down", detail=f"folder {self.folder} missing")
        try:
            n = len(self.list_projects())
            return Health(status="ok", detail=f"{n} projects in {len(self.files())} files")
        except Exception as e:
            return Health(status="down", detail=f"{type(e).__name__}: {str(e)[:120]}")

    # the contract's read methods, per server
    def list_projects(self) -> list[ExtProject]:
        raise NotImplementedError

    def get_activities(self, project: str) -> list[Activity]:
        raise NotImplementedError

    def get_baseline(self, project: str) -> Baseline | None:
        raise NotImplementedError

    def dcma14(self, project: str) -> DCMAReport | None:
        raise NotImplementedError


@register("longpath_mcp")
class LongpathSchedule(_McpSchedule):
    """longpath: `longpath` over stdio, config {command: longpath, folder, [cwd]}."""

    READ_TOOLS = frozenset({"check_setup", "schedule_summary", "list_activities", "list_relationships",
                            "activity_detail", "baseline_variance", "dcma_assessment", "critical_path"})
    SUFFIXES = (".xer", ".xml", ".mpp")

    def list_projects(self) -> list[ExtProject]:
        out: list[ExtProject] = []
        for f in self.files():
            s = self.call("schedule_summary", {"path": str(f)})
            inner = s.get("projects_in_file") or []
            for p in inner:
                if p.get("baseline_copy"):
                    continue
                out.append(ExtProject(id=f"{f.name}#{p.get('project')}", name=str(p.get("name") or p.get("project")),
                                      data_date=_dt(p.get("data_date"))))
            if not inner:
                out.append(ExtProject(id=f"{f.name}#{s.get('project', '')}", name=str(s.get("name") or s.get("project")),
                                      data_date=_dt(s.get("data_date"))))
        return out

    def _rows(self, tool: str, project: str, **extra: Any) -> list[dict[str, Any]]:
        path, short = self._split(project)
        args: dict[str, Any] = {"path": path, "limit": 100000, **extra}
        if short:
            args["project"] = short
        res = self.call(tool, args)
        return list(res.get("rows") or [])

    def get_activities(self, project: str) -> list[Activity]:
        rows = self._rows("list_activities", project, include_stored_float=True)
        critical = {r["id"] for r in self._rows("list_activities", project, critical_only=True)}
        preds: dict[str, list[str]] = {}
        for r in self._rows("list_relationships", project):
            preds.setdefault(str(r["successor"]), []).append(str(r["predecessor"]))
        return [Activity(id=str(r["id"]), name=str(r.get("name", "")), wbs=str(r.get("wbs", "")),
                         start=_dt(r.get("start")), finish=_dt(r.get("finish")),
                         total_float=_num(r.get("stored_float_d", r.get("total_float_d"))),
                         pct=100.0 if r.get("status") == "complete" else 0.0, preds=preds.get(str(r["id"]), []),
                         is_milestone=str(r.get("type", "")).endswith("milestone"), is_critical=r["id"] in critical)
                for r in rows]

    def get_baseline(self, project: str) -> Baseline | None:
        path, short = self._split(project)
        args: dict[str, Any] = {"path": path, "limit": 100000}
        if short:
            args["project"] = short
        base = self.conn.config.get("baseline_project")
        if base:  # an XER with a baseline copy inside: name it explicitly
            args.update(baseline_path=path, baseline_project=str(base))
        res = self.call("baseline_variance", args)
        fin = _dt(((res.get("summary") or {}).get("project_finish") or {}).get("baseline"))
        if fin is None:
            return None
        acts = [Activity(id=str(r["id"]), name=str(r.get("name", "")), finish=_dt(r.get("baseline_finish")))
                for r in res.get("rows") or [] if r.get("baseline_finish")]
        return Baseline(project=project, finish=fin, activities=acts)

    def dcma14(self, project: str) -> DCMAReport | None:
        path, short = self._split(project)
        args: dict[str, Any] = {"path": path, "float_source": str(self.conn.config.get("float_source", "stored"))}
        if short:
            args["project"] = short
        res = self.call("dcma_assessment", args)
        checks = res.get("checks") or []
        if not checks:
            return None
        found = re.match(r"\s*(\d+)\s+of\s+(\d+)", str(res.get("passed", "")))
        passed = int(found.group(1)) if found else sum(1 for c in checks if c.get("result") == "pass")
        total = int(found.group(2)) if found else len(checks)
        return DCMAReport(passed=passed, total=total,
                          failing=[str(c.get("title") or c.get("check")) for c in checks if c.get("result") == "fail"])


@register("p6_mcp")
class P6Schedule(_McpSchedule):
    """p6-mcp: config {command: p6-mcp, args: [serve, --transport, stdio, --no-mutation, --read-only,
    --allowed-dir, <folder>, --output-dir, <scratch>], folder}. Reads XER files only."""

    READ_TOOLS = frozenset({"list_xer_files", "get_projects", "get_activities", "get_relationships",
                            "get_milestones", "get_baselines", "get_activity_variances", "run_dcma_assessment"})
    SUFFIXES = (".xer",)

    def _paged(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset: int | None = 0
        while offset is not None:
            res = self.call(tool, {**args, "limit": PAGE, "offset": offset})
            items += list(res.get("items") or [])
            nxt = res.get("next_offset")
            offset = int(nxt) if nxt is not None and nxt != offset else None
        return items

    def list_projects(self) -> list[ExtProject]:
        out: list[ExtProject] = []
        for f in self.files():
            res = self.call("get_projects", {"file_path": str(f), "include_baselines": False})
            for p in res.get("items") or []:
                if p.get("is_baseline"):
                    continue
                out.append(ExtProject(id=f"{f.name}#{p['proj_short_name']}", name=str(p["proj_short_name"]),
                                      data_date=_dt(p.get("data_date"))))
        return out

    def _args(self, project: str) -> dict[str, Any]:
        path, short = self._split(project)
        return {"file_path": path, "project_short_name": short} if short else {"file_path": path}

    def get_activities(self, project: str) -> list[Activity]:
        args = self._args(project)
        preds: dict[str, list[str]] = {}
        for r in self._paged("get_relationships", args):
            preds.setdefault(str(r["successor"]), []).append(str(r["predecessor"]))
        return [Activity(id=str(r["task_code"]), name=str(r.get("task_name", "")), wbs=str(r.get("wbs_path") or ""),
                         start=_dt(r.get("start")), finish=_dt(r.get("finish")),
                         total_float=_num(r.get("total_float_days")), free_float=_num(r.get("free_float_days")),
                         pct=_num(r.get("percent_complete")), preds=preds.get(str(r["task_code"]), []),
                         is_milestone=bool(r.get("is_milestone")) or "Milestone" in str(r.get("task_type", "")),
                         is_critical=bool(r.get("is_critical")))
                for r in self._paged("get_activities", args)]

    def get_baseline(self, project: str) -> Baseline | None:
        path, short = self._split(project)
        base = next((b for b in self.call("get_baselines", {"file_path": path}).get("items") or []
                     if not short or b.get("linked_to") == short), None)
        if base is None:
            return None
        acts = [Activity(id=str(r["task_code"]), name=str(r.get("task_name", "")), finish=_dt(r.get("baseline")),
                         is_milestone=True)
                for r in self._paged("get_milestones", self._args(project)) if r.get("baseline")]
        return Baseline(project=project, finish=_dt(base.get("plan_end")), activities=acts)

    def dcma14(self, project: str) -> DCMAReport | None:
        res = self.call("run_dcma_assessment", self._args(project))
        checks = res.get("checks") or []
        if not checks:
            return None
        passed = int((res.get("summary") or {}).get("passed") or sum(1 for c in checks if c.get("passed")))
        return DCMAReport(passed=passed, total=len(checks),
                          failing=[str(c.get("name")) for c in checks if c.get("passed") is False])
