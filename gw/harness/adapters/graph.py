"""Microsoft Graph adapters: Planner, OneDrive/SharePoint, mail, Teams, calendar.

for M365. Thin: fetch and normalise only.
Application permissions are scoped Tests use
recorded fixtures or fakes; no live calls in CI.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from gw.errors import Forbidden
from gw.harness.contracts import (
    ApprovalCard,
    CalEvent,
    DocChange,
    DocMeta,
    ExternalRef,
    ExtPlan,
    Health,
    InboundMessage,
    OutboundMessage,
    SyncResult,
    Task,
)
from gw.harness.registry import register, resolve_config, secret
from gw.record import models as m

GRAPH = "https://graph.microsoft.com/v1.0"


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


DELEGATED_SCOPES = ["User.Read", "Mail.ReadWrite", "Mail.Send", "Files.ReadWrite", "Tasks.ReadWrite"]


def token_cache_path() -> Path:
    """The delegated token cache lives outside any repository, encrypted per user (DPAPI on Windows)."""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    return base / "gatewright" / "msal_token_cache.bin"


class GraphClient:
    """Graph over httpx with two auth modes; `transport` is injectable for recorded fixtures.

    - `auth: device_code` (delegated, solo): a person signs in once with `gw harness login <conn>`;
      the token cache is encrypted on disk outside the repo. No secret exists anywhere.
    - default (application): client credentials with `secret_ref` read from the environment.
    """

    def __init__(self, conn: m.Connection, transport: httpx.BaseTransport | None = None) -> None:
        self.conn = conn
        self.cfg = resolve_config(conn.config)
        self.client = httpx.Client(base_url=GRAPH, timeout=30, transport=transport)
        self._token: str | None = None
        self.delegated = self.cfg.get("auth") == "device_code"
        self.user_path = "/me" if self.delegated else f"/users/{self.cfg.get('mailbox', '')}"

    def _public_app(self) -> Any:
        import msal
        from msal_extensions import PersistedTokenCache, build_encrypted_persistence

        path = token_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        cache = PersistedTokenCache(build_encrypted_persistence(str(path)))
        return msal.PublicClientApplication(
            self.cfg.get("client_id"), authority=f"https://login.microsoftonline.com/{self.cfg.get('tenant', 'organizations')}",
            token_cache=cache)

    def start_device_flow(self) -> dict[str, Any]:
        """Begin a device-code sign-in; the returned flow carries user_code, verification_uri, message."""
        flow = dict(self._public_app().initiate_device_flow(scopes=DELEGATED_SCOPES))
        if "user_code" not in flow:
            raise RuntimeError(f"{self.conn.id}: device flow failed: {flow.get('error_description', flow)}")
        return flow

    def finish_device_flow(self, flow: dict[str, Any]) -> str:
        """Block until the person has signed in (or the code expires); returns the account name."""
        res = self._public_app().acquire_token_by_device_flow(flow)
        if "access_token" not in res:
            raise RuntimeError(f"{self.conn.id}: sign-in failed: {res.get('error_description', res.get('error'))}")
        self._token = None
        return str((res.get("id_token_claims") or {}).get("preferred_username", "signed in"))

    def login(self, show: Any = print) -> str:
        """Device-code sign-in (delegated) for the CLI: shows the code and URL, waits, returns the account."""
        flow = self.start_device_flow()
        show(flow["message"])
        return self.finish_device_flow(flow)

    def token(self) -> str:
        if self._token:
            return self._token
        if self.delegated:
            app = self._public_app()
            accounts = app.get_accounts()
            res = app.acquire_token_silent(DELEGATED_SCOPES, account=accounts[0]) if accounts else None
            if not res or "access_token" not in res:
                raise RuntimeError(f"{self.conn.id}: not signed in; run `gw harness login {self.conn.id}`")
            self._token = str(res["access_token"])
            return self._token
        import msal

        app = msal.ConfidentialClientApplication(
            self.cfg.get("client_id"), authority=f"https://login.microsoftonline.com/{self.cfg.get('tenant')}",
            client_credential=secret(self.cfg.get("secret_ref")))
        res = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in res:
            raise RuntimeError(f"{self.conn.id}: token failed: {res.get('error_description', res.get('error'))}")
        self._token = str(res["access_token"])
        return self._token

    def get(self, path: str, _headers: dict[str, str] | None = None, **params: Any) -> dict[str, Any]:
        r = self.client.get(path, params=params or None,
                            headers={"Authorization": f"Bearer {self.token()}", **(_headers or {})})
        r.raise_for_status()
        return dict(r.json())

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self.client.post(path, json=body, headers={"Authorization": f"Bearer {self.token()}"})
        r.raise_for_status()
        return dict(r.json()) if r.content else {}

    def put_bytes(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> dict[str, Any]:
        r = self.client.put(path, content=data, headers={"Authorization": f"Bearer {self.token()}",
                                                         "Content-Type": content_type})
        r.raise_for_status()
        return dict(r.json()) if r.content else {}

    def health(self) -> Health:
        try:
            self.token()
            return Health(status="ok", detail="token acquired")
        except Exception as e:
            return Health(status="down", detail=str(e))


@register("planner_graph")
class PlannerGraph:
    """Planner basic and premium plans as a PlanSource ( 0007)."""

    def __init__(self, conn: m.Connection) -> None:
        self.g = GraphClient(conn)
        self.plan_ids = list(self.g.cfg.get("plan_ids") or [])

    def list_plans(self) -> list[ExtPlan]:
        return [ExtPlan(id=p, name=self.g.get(f"/planner/plans/{p}").get("title", p)) for p in self.plan_ids]

    def get_tasks(self, plan: str) -> list[Task]:
        buckets = {b["id"]: b["name"] for b in self.g.get(f"/planner/plans/{plan}/buckets").get("value", [])}
        cats = self.g.get(f"/planner/plans/{plan}/details").get("categoryDescriptions", {})
        out = []
        for t in self.g.get(f"/planner/plans/{plan}/tasks").get("value", []):
            labels = [cats.get(k) or k for k, on in (t.get("appliedCategories") or {}).items() if on]
            out.append(Task(id=t["id"], title=t.get("title", ""), bucket=buckets.get(t.get("bucketId"), ""),
                            due=_ts(t.get("dueDateTime")), done=t.get("percentComplete") == 100,
                            pct=float(t.get("percentComplete") or 0), labels=[str(x) for x in labels],
                            url=f"https://tasks.office.com/Home/Task/{t['id']}"))
        return out

    def subscribe(self, plan: str) -> str | None:
        return None  # polling; change notifications depend on tenant settings

    def sync(self, since: str | None) -> SyncResult:
        n = sum(len(self.get_tasks(p)) for p in self.plan_ids)
        return SyncResult(pulled=n, cursor=datetime.now(UTC).isoformat())

    def health(self) -> Health:
        return self.g.health()

    def add_comment(self, task: str, text: str) -> None:
        raise Forbidden("this adapter does not read planner comments", limit="scope")


@register("onedrive_graph")
class OneDriveGraph:
    """OneDrive and SharePoint document libraries via Graph delta."""

    def __init__(self, conn: m.Connection) -> None:
        self.conn = conn
        self.g = GraphClient(conn)
        self.drives = list(self.g.cfg.get("drive_ids") or ([] if not self.g.delegated else ["me"]))
        self.root_path = str(self.g.cfg.get("root_path", "")).strip("/")

    def _drive(self, d: str) -> str:
        return "/me/drive" if d == "me" else f"/drives/{d}"

    def _in_root(self, it: dict[str, Any]) -> bool:
        if not self.root_path:
            return True
        parent = str((it.get("parentReference") or {}).get("path", ""))
        return f"/root:/{self.root_path}" in parent

    def list_changes(self, cursor: str | None) -> tuple[list[DocChange], str]:
        changes: list[DocChange] = []
        new_cursor = cursor or ""
        for d in self.drives:
            url = cursor or f"{self._drive(d)}/root/delta"
            while url:
                page = self.g.get(url.replace(GRAPH, ""))
                for it in page.get("value", []):
                    if ("file" not in it and "deleted" not in it) or not self._in_root(it):
                        continue
                    changes.append(DocChange(
                        ref=ExternalRef(conn_id=self.conn.id, external_id=f"{d}:{it['id']}", url=it.get("webUrl")),
                        name=it.get("name", ""), modified=_ts(it.get("lastModifiedDateTime")) or datetime.now(UTC),
                        deleted="deleted" in it))
                url = page.get("@odata.nextLink", "")
                new_cursor = page.get("@odata.deltaLink", new_cursor)
        return changes, new_cursor

    def _item(self, ref: ExternalRef) -> tuple[str, str]:
        d, i = ref.external_id.split(":", 1)
        return d, i

    def stat(self, ref: ExternalRef) -> DocMeta:
        d, i = self._item(ref)
        it = self.g.get(f"{self._drive(d)}/items/{i}")
        return DocMeta(name=it.get("name", ""), size=int(it.get("size", 0)),
                       modified=_ts(it.get("lastModifiedDateTime")) or datetime.now(UTC),
                       version=it.get("eTag"), hash=((it.get("file") or {}).get("hashes") or {}).get("sha256Hash"))

    def _stream(self, ref: ExternalRef, max_bytes: int | None = None) -> bytes:
        d, i = self._item(ref)
        r = self.g.client.get(f"{self._drive(d)}/items/{i}/content", follow_redirects=True,
                              headers={"Authorization": f"Bearer {self.g.token()}",
                                       **({"Range": f"bytes=0-{max_bytes - 1}"} if max_bytes else {})})
        r.raise_for_status()
        return r.content

    def read_text(self, ref: ExternalRef, max_bytes: int) -> str:
        return self._stream(ref, max_bytes)[:max_bytes].decode("utf-8", errors="replace")

    def hash(self, ref: ExternalRef) -> str:
        return hashlib.sha256(self._stream(ref)).hexdigest()

    def link(self, ref: ExternalRef) -> str:
        d, i = self._item(ref)
        return str(self.g.get(f"{self._drive(d)}/items/{i}").get("webUrl", ""))

    def upload(self, name: str, data: bytes) -> dict[str, Any]:
        """Upload a small file into the connection's folder (demo seeding only; needs scope `seed_demo`)."""
        d = self.drives[0]
        path = f"{self.root_path}/{name}" if self.root_path else name
        return self.g.put_bytes(f"{self._drive(d)}/root:/{path}:/content", data)

    def sync(self, since: str | None) -> SyncResult:
        ch, cur = self.list_changes(since)
        return SyncResult(pulled=len(ch), changed=len(ch), cursor=cur)

    def health(self) -> Health:
        return self.g.health()


@register("m365_mail_graph")
class M365Mail:
    """Shared mailbox: pull messages, send-as with the gw-ref footer."""

    def __init__(self, conn: m.Connection) -> None:
        self.g = GraphClient(conn)
        self.mailbox = self.g.cfg.get("mailbox", "")

    def pull(self, since: str | None) -> list[InboundMessage]:
        params: dict[str, Any] = {"$top": 50, "$orderby": "receivedDateTime asc",
                                  "$select": "id,subject,body,from,receivedDateTime,conversationId,internetMessageHeaders"}
        if since:
            stamp = datetime.fromisoformat(since).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            params["$filter"] = f"receivedDateTime gt {stamp}"
        out = []
        url: str | None = f"{self.g.user_path}/mailFolders/inbox/messages"
        while url:
            page = self.g.get(url.replace(GRAPH, ""), **(params if not url.startswith(GRAPH) else {}),
                              _headers={"Prefer": 'outlook.body-content-type="text"'})
            for it in page.get("value", []):
                hdrs = {h["name"].lower(): h["value"] for h in it.get("internetMessageHeaders") or []}
                out.append(InboundMessage(
                    id=it["id"], sender=(it.get("from") or {}).get("emailAddress", {}).get("address", ""),
                    subject=it.get("subject", ""), text=(it.get("body") or {}).get("content", ""),
                    received_at=_ts(it.get("receivedDateTime")) or datetime.now(UTC),
                    thread_id=it.get("conversationId"), headers=hdrs))
            url = page.get("@odata.nextLink")
        return out

    def send(self, msg: OutboundMessage) -> str:
        body = {"message": {
            "subject": msg.subject, "body": {"contentType": "Text", "content": msg.text},
            "toRecipients": [{"emailAddress": {"address": a}} for a in msg.to],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in msg.cc],
            "internetMessageHeaders": [{"name": "x-gw-ref", "value": msg.gw_ref}]}, "saveToSentItems": True}
        self.g.post(f"{self.g.user_path}/sendMail", body)
        return msg.gw_ref

    def post_card(self, card: ApprovalCard) -> str:
        raise Forbidden("mail connections do not post cards", limit="scope")

    def deliver_to_self(self, subject: str, text: str, headers: dict[str, str]) -> None:
        """Demo seeding: send a synthetic inbound message to this mailbox itself (never to a party)."""
        me = self.g.get(f"{self.g.user_path}").get("mail") or self.mailbox
        self.g.post(f"{self.g.user_path}/sendMail", {"message": {
            "subject": subject, "body": {"contentType": "Text", "content": text},
            "toRecipients": [{"emailAddress": {"address": me}}],
            "internetMessageHeaders": [{"name": k, "value": v} for k, v in headers.items()]},
            "saveToSentItems": False})

    def sync(self, since: str | None) -> SyncResult:
        msgs = self.pull(since)
        return SyncResult(pulled=len(msgs), cursor=max((x.received_at.isoformat() for x in msgs), default=since))

    def health(self) -> Health:
        return self.g.health()


@register("teams_graph")
class TeamsGraph(M365Mail):
    """Teams channel: pull messages and post adaptive approval cards."""

    def __init__(self, conn: m.Connection) -> None:
        super().__init__(conn)
        self.team = self.g.cfg.get("team_id", "")
        self.channel = self.g.cfg.get("channel_id", "")

    def pull(self, since: str | None) -> list[InboundMessage]:
        out = []
        for it in self.g.get(f"/teams/{self.team}/channels/{self.channel}/messages").get("value", []):
            out.append(InboundMessage(id=it["id"], sender=((it.get("from") or {}).get("user") or {}).get("displayName", ""),
                                      text=(it.get("body") or {}).get("content", ""),
                                      received_at=_ts(it.get("createdDateTime")) or datetime.now(UTC)))
        return out

    def post_card(self, card: ApprovalCard) -> str:
        adaptive = {"type": "AdaptiveCard", "version": "1.5", "body": [
            {"type": "TextBlock", "text": card.title, "weight": "Bolder"},
            {"type": "TextBlock", "text": card.body, "wrap": True}],
            "actions": [{"type": "Action.Submit", "title": a, "data": {"approval": card.approval_id,
                                                                      "action": a.lower(), "token": card.token}}
                        for a in card.actions]}
        res = self.g.post(f"/teams/{self.team}/channels/{self.channel}/messages", {
            "body": {"contentType": "html", "content": '<attachment id="gw"></attachment>'},
            "attachments": [{"id": "gw", "contentType": "application/vnd.microsoft.card.adaptive",
                             "content": __import__("json").dumps(adaptive)}]})
        return str(res.get("id", ""))


@register("m365_calendar_graph")
class M365Calendar:
    """Gate review calendar."""

    def __init__(self, conn: m.Connection) -> None:
        self.g = GraphClient(conn)
        self.user = self.g.cfg.get("mailbox") or self.g.cfg.get("user", "")
        self.base = self.g.user_path if self.g.delegated else f"/users/{self.user}"

    def list_events(self, window: tuple[datetime, datetime]) -> list[CalEvent]:
        res = self.g.get(f"{self.base}/calendarView", startDateTime=window[0].isoformat(),
                         endDateTime=window[1].isoformat())
        return [CalEvent(id=e["id"], title=e.get("subject", ""), start=_ts(e["start"]["dateTime"]) or window[0],
                         end=_ts(e["end"]["dateTime"]) or window[1]) for e in res.get("value", [])]

    def create_event(self, ev: CalEvent) -> str:
        res = self.g.post(f"{self.base}/events", {
            "subject": ev.title, "start": {"dateTime": ev.start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": ev.end.isoformat(), "timeZone": "UTC"}})
        return str(res.get("id", ""))

    def health(self) -> Health:
        return self.g.health()
