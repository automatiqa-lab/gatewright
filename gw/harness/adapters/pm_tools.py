"""PlanSource adapters for task tools: Jira, Asana, Trello.

Thin: fetch and normalise into `Task`. Mapping hints go through the fields the ingest step
already reads ( mapping): `labels` carry deliverable ids, `bucket` carries a work
package code.
- Jira: labels -> labels; components (or the epic key) -> bucket.
- Asana: tags -> labels; section -> bucket.
- Trello: label names -> labels; list name -> bucket.
Tokens are read by key name through `secret()` at call time and never stored.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import httpx

from gw.harness.contracts import ExtPlan, Health, SyncResult, Task
from gw.harness.registry import register, resolve_config, secret
from gw.record import models as m


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if len(s) == 10:
        s += "T17:00:00+00:00"
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


class _Http:
    def __init__(self, base_url: str, headers: dict[str, str], transport: httpx.BaseTransport | None) -> None:
        self.client = httpx.Client(base_url=base_url, headers=headers, timeout=30, transport=transport)

    def get(self, path: str, **params: Any) -> Any:
        r = self.client.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict[str, Any]) -> Any:
        r = self.client.post(path, json=body)
        r.raise_for_status()
        return r.json() if r.content else {}


@register("jira_rest")
class JiraRest:
    """Jira Cloud REST v3. Config: base_url, project_key, email, token_ref, [jql]. One project = one plan."""

    def __init__(self, conn: m.Connection, transport: httpx.BaseTransport | None = None) -> None:
        self.conn = conn
        cfg = resolve_config(conn.config)
        self.project_key = str(cfg.get("project_key", ""))
        self.jql = str(cfg.get("jql") or f"project = {self.project_key} ORDER BY duedate")
        self.base_url = str(cfg.get("base_url", "")).rstrip("/")
        user = str(cfg.get("email", ""))
        auth = base64.b64encode(f"{user}:{secret(cfg.get('token_ref')) or ''}".encode()).decode()
        self.http = _Http(self.base_url, {"Authorization": f"Basic {auth}", "Accept": "application/json"}, transport)

    def list_plans(self) -> list[ExtPlan]:
        p = self.http.get(f"/rest/api/3/project/{self.project_key}")
        return [ExtPlan(id=self.project_key, name=f"{self.project_key} {p.get('name', '')}".strip())]

    def get_tasks(self, plan: str) -> list[Task]:
        out: list[Task] = []
        start = 0
        while True:
            page = self.http.get("/rest/api/3/search", jql=self.jql, startAt=start, maxResults=100,
                                 fields="summary,labels,components,duedate,status,assignee,parent")
            for it in page.get("issues", []):
                f = it.get("fields") or {}
                comps = [c.get("name", "") for c in f.get("components") or []]
                epic = ((f.get("parent") or {}).get("key")) or ""
                done = ((f.get("status") or {}).get("statusCategory") or {}).get("key") == "done"
                out.append(Task(id=it["key"], title=f.get("summary", ""), bucket=comps[0] if comps else epic,
                                owner_email=(f.get("assignee") or {}).get("emailAddress"), due=_dt(f.get("duedate")),
                                done=done, pct=100.0 if done else 0.0, labels=list(f.get("labels") or []),
                                url=f"{self.base_url}/browse/{it['key']}"))
            start += len(page.get("issues", []))
            if not page.get("issues") or start >= int(page.get("total", 0)):
                return out

    def subscribe(self, plan: str) -> str | None:
        return None  # webhooks need an admin; polling

    def sync(self, since: str | None) -> SyncResult:
        n = sum(len(self.get_tasks(p.id)) for p in self.list_plans())
        return SyncResult(pulled=n, cursor=datetime.now(UTC).isoformat())

    def health(self) -> Health:
        try:
            self.http.get("/rest/api/3/myself")
            return Health(status="ok", detail=self.base_url)
        except Exception as e:
            return Health(status="down", detail=str(e)[:200])

    def add_comment(self, task: str, text: str) -> None:
        self.http.post(f"/rest/api/3/issue/{task}/comment", {"body": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}})


@register("asana_api")
class AsanaApi:
    """Asana REST 1.0. Config: project_gids (list), token_ref. One project = one plan."""

    def __init__(self, conn: m.Connection, transport: httpx.BaseTransport | None = None) -> None:
        cfg = resolve_config(conn.config)
        self.projects = [str(p) for p in cfg.get("project_gids") or []]
        self.http = _Http("https://app.asana.com/api/1.0",
                          {"Authorization": f"Bearer {secret(cfg.get('token_ref')) or ''}"}, transport)

    def list_plans(self) -> list[ExtPlan]:
        return [ExtPlan(id=g, name=str(self.http.get(f"/projects/{g}")["data"].get("name", g))) for g in self.projects]

    def get_tasks(self, plan: str) -> list[Task]:
        out: list[Task] = []
        params: dict[str, Any] = {"project": plan, "limit": 100,
                                  "opt_fields": "name,completed,due_on,assignee.email,tags.name,memberships.section.name,"
                                                "memberships.project.gid,permalink_url"}
        while True:
            page = self.http.get("/tasks", **params)
            for t in page.get("data", []):
                section = next((ms.get("section", {}).get("name", "") for ms in t.get("memberships") or []
                                if (ms.get("project") or {}).get("gid") == plan), "")
                out.append(Task(id=t["gid"], title=t.get("name", ""), bucket=section,
                                owner_email=(t.get("assignee") or {}).get("email"), due=_dt(t.get("due_on")),
                                done=bool(t.get("completed")), pct=100.0 if t.get("completed") else 0.0,
                                labels=[tg.get("name", "") for tg in t.get("tags") or []], url=t.get("permalink_url")))
            nxt = (page.get("next_page") or {}).get("offset")
            if not nxt:
                return out
            params["offset"] = nxt

    def subscribe(self, plan: str) -> str | None:
        return None

    def sync(self, since: str | None) -> SyncResult:
        return SyncResult(pulled=sum(len(self.get_tasks(p)) for p in self.projects),
                          cursor=datetime.now(UTC).isoformat())

    def health(self) -> Health:
        try:
            self.http.get("/users/me")
            return Health(status="ok", detail="asana")
        except Exception as e:
            return Health(status="down", detail=str(e)[:200])

    def add_comment(self, task: str, text: str) -> None:
        self.http.post(f"/tasks/{task}/stories", {"data": {"text": text}})


@register("trello_api")
class TrelloApi:
    """Trello REST 1. Config: board_ids (list), key_ref, token_ref. One board = one plan."""

    def __init__(self, conn: m.Connection, transport: httpx.BaseTransport | None = None) -> None:
        cfg = resolve_config(conn.config)
        self.boards = [str(b) for b in cfg.get("board_ids") or []]
        self.key_ref, self.token_ref = cfg.get("key_ref"), cfg.get("token_ref")
        self.http = _Http("https://api.trello.com/1", {"Accept": "application/json"}, transport)

    def _auth(self) -> dict[str, str]:
        return {"key": secret(self.key_ref) or "", "token": secret(self.token_ref) or ""}

    def list_plans(self) -> list[ExtPlan]:
        return [ExtPlan(id=b, name=str(self.http.get(f"/boards/{b}", **self._auth()).get("name", b))) for b in self.boards]

    def get_tasks(self, plan: str) -> list[Task]:
        lists = {li["id"]: li.get("name", "") for li in self.http.get(f"/boards/{plan}/lists", **self._auth())}
        out: list[Task] = []
        for c in self.http.get(f"/boards/{plan}/cards", **self._auth()):
            out.append(Task(id=c["id"], title=c.get("name", ""), bucket=lists.get(c.get("idList", ""), ""),
                            due=_dt(c.get("due")), done=bool(c.get("dueComplete")),
                            pct=100.0 if c.get("dueComplete") else 0.0,
                            labels=[lb.get("name", "") for lb in c.get("labels") or [] if lb.get("name")],
                            url=c.get("shortUrl")))
        return out

    def subscribe(self, plan: str) -> str | None:
        return None

    def sync(self, since: str | None) -> SyncResult:
        return SyncResult(pulled=sum(len(self.get_tasks(b)) for b in self.boards), cursor=datetime.now(UTC).isoformat())

    def health(self) -> Health:
        try:
            self.http.get("/members/me", **self._auth())
            return Health(status="ok", detail="trello")
        except Exception as e:
            return Health(status="down", detail=str(e)[:200])

    def add_comment(self, task: str, text: str) -> None:
        r = self.http.client.post(f"/cards/{task}/actions/comments", params={**self._auth(), "text": text})
        r.raise_for_status()
