"""Cloud ScheduleSource adapters: Smartsheet and MS Project (Project for the web).

- `smartsheet_api`: dependency-enabled sheets. Config: sheet_ids, token_ref, columns (title map for
  name/start/finish/pct/preds/wbs/float). Smartsheet's API does not return float or the critical path,
  so float comes from an optional column (e.g. a formula column) and is otherwise absent (0, not
  critical): Gatewright never computes CPM (ADR 0007).
- `msproject_graph`: Project for the web / Planner premium tasks and dependencies through the
  Dataverse Web API (msdyn_projecttasks, msdyn_projecttaskdependencies). Config: org_url, tenant,
  client_id, auth (device_code | app), secret_ref (app only), project_ids. Float is not exposed by
  Dataverse either; milestones are tasks with zero duration.
Both are read-only: they subclass ScheduleSourceBase, so writes raise Forbidden (hard limit 1).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from gw.harness.contracts import (
    Activity,
    Baseline,
    DCMAReport,
    ExtProject,
    Health,
    ScheduleSourceBase,
    SyncResult,
)
from gw.harness.registry import register, resolve_config, secret
from gw.record import models as m


def _dt(s: Any) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


@register("smartsheet_api")
class SmartsheetApi(ScheduleSourceBase):
    """Smartsheet API 2.0, one sheet = one external project."""

    DEFAULT_COLUMNS = {"name": "Task Name", "start": "Start", "finish": "Finish", "pct": "% Complete",
                       "preds": "Predecessors", "wbs": "WBS", "float": "Total Float"}

    def __init__(self, conn: m.Connection, transport: httpx.BaseTransport | None = None) -> None:
        cfg = resolve_config(conn.config)
        self.sheets = [str(s) for s in cfg.get("sheet_ids") or []]
        self.columns = {**self.DEFAULT_COLUMNS, **(cfg.get("columns") or {})}
        # float is only real when the sheet carries a float column the person maintains or a tool computes
        self.reports_float = bool(cfg.get("float_column_maintained", False))
        self.token_ref = cfg.get("token_ref")
        self.client = httpx.Client(base_url="https://api.smartsheet.com/2.0", timeout=30, transport=transport)
        self._cache: dict[str, dict[str, Any]] = {}

    def _sheet(self, sheet_id: str) -> dict[str, Any]:
        if sheet_id not in self._cache:
            r = self.client.get(f"/sheets/{sheet_id}", headers={"Authorization": f"Bearer {secret(self.token_ref) or ''}"})
            r.raise_for_status()
            self._cache[sheet_id] = dict(r.json())
        return self._cache[sheet_id]

    def list_projects(self) -> list[ExtProject]:
        out = []
        for sid in self.sheets:
            sh = self._sheet(sid)
            out.append(ExtProject(id=sid, name=str(sh.get("name", sid)), data_date=_dt(sh.get("modifiedAt"))))
        return out

    def get_activities(self, project: str) -> list[Activity]:
        sh = self._sheet(project)
        title_by_id = {c["id"]: c.get("title", "") for c in sh.get("columns", [])}
        want = {v: k for k, v in self.columns.items()}
        rows = sh.get("rows", [])
        num_to_id = {r.get("rowNumber"): str(r["id"]) for r in rows}
        acts = []
        for r in rows:
            vals: dict[str, Any] = {}
            for cell in r.get("cells", []):
                key = want.get(title_by_id.get(cell.get("columnId"), ""))
                if key:
                    vals[key] = cell.get("value")
            start, finish = _dt(vals.get("start")), _dt(vals.get("finish"))
            preds = [num_to_id[int(n)] for n in re.findall(r"(\d+)", str(vals.get("preds") or ""))
                     if int(n) in num_to_id]
            pct = float(vals.get("pct") or 0)
            acts.append(Activity(
                id=str(r["id"]), name=str(vals.get("name") or ""), wbs=str(vals.get("wbs") or ""), start=start,
                finish=finish, total_float=float(vals.get("float") or 0), pct=pct * 100 if pct <= 1 else pct,
                preds=preds, is_milestone=start is not None and start == finish, is_critical=False))
        return acts

    def get_milestones(self, project: str) -> list[Activity]:
        return [a for a in self.get_activities(project) if a.is_milestone]

    def get_baseline(self, project: str) -> Baseline | None:
        return None

    def dcma14(self, project: str) -> DCMAReport | None:
        return None  # longpath provides DCMA; Smartsheet has no CPM data

    def sync(self, since: str | None) -> SyncResult:
        self._cache.clear()
        return SyncResult(pulled=len(self.sheets), changed=len(self.sheets), cursor=datetime.now(UTC).isoformat())

    def health(self) -> Health:
        try:
            r = self.client.get("/users/me", headers={"Authorization": f"Bearer {secret(self.token_ref) or ''}"})
            r.raise_for_status()
            return Health(status="ok", detail="smartsheet")
        except Exception as e:
            return Health(status="down", detail=str(e)[:200])


@register("msproject_graph")
class MsProjectDataverse(ScheduleSourceBase):
    reports_float = False  # Project for the web exposes dependencies but no total float
    """Project for the web through Dataverse; one msdyn_project = one external project."""

    def __init__(self, conn: m.Connection, transport: httpx.BaseTransport | None = None) -> None:
        self.conn = conn
        self.cfg = resolve_config(conn.config)
        self.org = str(self.cfg.get("org_url", "")).rstrip("/")
        self.projects = [str(p) for p in self.cfg.get("project_ids") or []]
        self.client = httpx.Client(base_url=f"{self.org}/api/data/v9.2", timeout=30, transport=transport)
        self._token: str | None = None

    def token(self) -> str:
        """Dataverse token (scope <org>/.default), delegated via the shared device-code cache or app-only."""
        if self._token:
            return self._token
        import msal

        scope = [f"{self.org}/.default"]
        authority = f"https://login.microsoftonline.com/{self.cfg.get('tenant', 'organizations')}"
        if self.cfg.get("auth") == "device_code":
            from msal_extensions import PersistedTokenCache, build_encrypted_persistence

            from gw.harness.adapters.graph import token_cache_path

            cache = PersistedTokenCache(build_encrypted_persistence(str(token_cache_path())))
            app: Any = msal.PublicClientApplication(self.cfg.get("client_id"), authority=authority, token_cache=cache)
            accounts = app.get_accounts()
            res = app.acquire_token_silent(scope, account=accounts[0]) if accounts else None
        else:
            app = msal.ConfidentialClientApplication(self.cfg.get("client_id"), authority=authority,
                                                     client_credential=secret(self.cfg.get("secret_ref")))
            res = app.acquire_token_for_client(scopes=scope)
        if not res or "access_token" not in res:
            raise RuntimeError(f"{self.conn.id}: no Dataverse token; sign in or check the app registration")
        self._token = str(res["access_token"])
        return self._token

    def _get_all(self, path: str, **params: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        url: str | None = path
        first = True
        while url:
            r = self.client.get(url, params=params if first else None,
                                headers={"Authorization": f"Bearer {self.token()}", "Prefer": "odata.maxpagesize=500"})
            r.raise_for_status()
            page = r.json()
            out += page.get("value", [])
            url = page.get("@odata.nextLink")
            first = False
        return out

    def list_projects(self) -> list[ExtProject]:
        out = []
        for row in self._get_all("/msdyn_projects", **{"$select": "msdyn_projectid,msdyn_subject,modifiedon"}):
            if self.projects and row["msdyn_projectid"] not in self.projects:
                continue
            out.append(ExtProject(id=row["msdyn_projectid"], name=row.get("msdyn_subject", ""),
                                  data_date=_dt(row.get("modifiedon"))))
        return out

    def get_activities(self, project: str) -> list[Activity]:
        flt = f"_msdyn_project_value eq {project}"
        tasks = self._get_all("/msdyn_projecttasks", **{
            "$select": "msdyn_projecttaskid,msdyn_subject,msdyn_scheduledstart,msdyn_scheduledend,msdyn_progress,"
                       "msdyn_wbsid,msdyn_summary", "$filter": flt})
        deps = self._get_all("/msdyn_projecttaskdependencies", **{
            "$select": "_msdyn_predecessortask_value,_msdyn_successortask_value", "$filter": flt})
        preds: dict[str, list[str]] = {}
        for d in deps:
            preds.setdefault(d["_msdyn_successortask_value"], []).append(d["_msdyn_predecessortask_value"])
        acts = []
        for t in tasks:
            if t.get("msdyn_summary"):
                continue
            start, finish = _dt(t.get("msdyn_scheduledstart")), _dt(t.get("msdyn_scheduledend"))
            acts.append(Activity(id=t["msdyn_projecttaskid"], name=t.get("msdyn_subject", ""), wbs=str(t.get("msdyn_wbsid") or ""),
                                 start=start, finish=finish, pct=float(t.get("msdyn_progress") or 0) * 100,
                                 preds=preds.get(t["msdyn_projecttaskid"], []),
                                 is_milestone=bool(start and finish and start == finish), is_critical=False))
        return acts

    def get_milestones(self, project: str) -> list[Activity]:
        return [a for a in self.get_activities(project) if a.is_milestone]

    def get_baseline(self, project: str) -> Baseline | None:
        return None

    def dcma14(self, project: str) -> DCMAReport | None:
        return None

    def sync(self, since: str | None) -> SyncResult:
        return SyncResult(pulled=len(self.list_projects()), cursor=datetime.now(UTC).isoformat())

    def health(self) -> Health:
        try:
            self.token()
            return Health(status="ok", detail=self.org)
        except Exception as e:
            return Health(status="down", detail=str(e)[:200])
