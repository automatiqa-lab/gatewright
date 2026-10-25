"""File, IMAP/SMTP and MCP-generic adapters.

All thin: they read files or servers and normalise into contract types.
"""

from __future__ import annotations

import csv
import email
import hashlib
import imaplib
import smtplib
from datetime import UTC, date, datetime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from gw.errors import Forbidden
from gw.harness.contracts import (
    ApprovalCard,
    CostRowIn,
    DocChange,
    DocMeta,
    ExternalRef,
    ExtPlan,
    Health,
    Hit,
    InboundMessage,
    OutboundMessage,
    Principal,
    SyncResult,
    Task,
)
from gw.harness.registry import register, resolve_config, secret
from gw.record import models as m


def _mtime(p: Path) -> datetime:
    return datetime.fromtimestamp(p.stat().st_mtime, UTC)


@register("local_folder")
class LocalFolder:
    """DocumentSource over a folder tree; cursor = ISO time of the newest change seen."""

    def __init__(self, conn: m.Connection) -> None:
        self.conn = conn
        self.root = Path(conn.config.get("folder", "./drop/docs"))

    def _path(self, ref: ExternalRef) -> Path:
        p = (self.root / ref.external_id).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise Forbidden(f"{ref.external_id} is outside the connection folder", limit="scope")
        return p

    def list_changes(self, cursor: str | None) -> tuple[list[DocChange], str]:
        since = datetime.fromisoformat(cursor) if cursor else None
        out = []
        newest = since
        for p in sorted(self.root.rglob("*")) if self.root.exists() else []:
            if not p.is_file():
                continue
            mt = _mtime(p)
            if since and mt <= since:
                continue
            rel = p.relative_to(self.root).as_posix()
            out.append(DocChange(ref=ExternalRef(conn_id=self.conn.id, external_id=rel, url=p.resolve().as_uri()),
                                 name=p.name, modified=mt))
            newest = max(newest, mt) if newest else mt
        return out, (newest or datetime.now(UTC)).isoformat()

    def stat(self, ref: ExternalRef) -> DocMeta:
        p = self._path(ref)
        return DocMeta(name=p.name, size=p.stat().st_size, modified=_mtime(p))

    def read_text(self, ref: ExternalRef, max_bytes: int) -> str:
        with self._path(ref).open("rb") as f:
            return f.read(max_bytes).decode("utf-8", errors="replace")

    def hash(self, ref: ExternalRef) -> str:
        h = hashlib.sha256()
        with self._path(ref).open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()

    def link(self, ref: ExternalRef) -> str:
        return self._path(ref).as_uri()

    def sync(self, since: str | None) -> SyncResult:
        ch, cur = self.list_changes(since)
        return SyncResult(pulled=len(ch), changed=len(ch), cursor=cur)

    def health(self) -> Health:
        return Health(status="ok" if self.root.exists() else "down", detail=str(self.root))


@register("imap_smtp")
class ImapSmtp:
    """MessageChannel over IMAP (pull) and SMTP (send). Password via secret_ref."""

    def __init__(self, conn: m.Connection) -> None:
        self.cfg = resolve_config(conn.config)

    def pull(self, since: str | None) -> list[InboundMessage]:
        box = imaplib.IMAP4_SSL(self.cfg["imap_host"])
        box.login(self.cfg["user"], secret(self.cfg.get("secret_ref")) or "")
        box.select("INBOX", readonly=True)
        crit = f'(SINCE "{datetime.fromisoformat(since):%d-%b-%Y}")' if since else "ALL"
        _, ids = box.search(None, crit)
        out = []
        for num in ids[0].split():
            _, data = box.fetch(num, "(RFC822)")
            raw = data[0][1] if data and isinstance(data[0], tuple) else b""
            msg = email.message_from_bytes(raw)
            body = msg.get_payload(decode=True) if not msg.is_multipart() else next(
                (p.get_payload(decode=True) for p in msg.walk() if p.get_content_type() == "text/plain"), b"")
            out.append(InboundMessage(
                id=msg.get("Message-ID", num.decode()), sender=email.utils.parseaddr(msg.get("From", ""))[1],
                subject=msg.get("Subject", ""),
                text=body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body or ""),
                received_at=parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else datetime.now(UTC),
                thread_id=msg.get("In-Reply-To"), headers={k.lower(): v for k, v in msg.items()}))
        box.logout()
        return out

    def send(self, msg: OutboundMessage) -> str:
        em = EmailMessage()
        em["From"], em["To"], em["Subject"] = self.cfg["user"], ", ".join(msg.to), msg.subject
        if msg.cc:
            em["Cc"] = ", ".join(msg.cc)
        em["X-GW-Ref"] = msg.gw_ref
        em.set_content(msg.text)
        with smtplib.SMTP_SSL(self.cfg["smtp_host"]) as s:
            s.login(self.cfg["user"], secret(self.cfg.get("secret_ref")) or "")
            s.send_message(em)
        return msg.gw_ref

    def post_card(self, card: ApprovalCard) -> str:
        raise Forbidden("mail connections do not post cards", limit="scope")

    def sync(self, since: str | None) -> SyncResult:
        msgs = self.pull(since)
        return SyncResult(pulled=len(msgs), cursor=max((x.received_at.isoformat() for x in msgs), default=since))

    def health(self) -> Health:
        try:
            box = imaplib.IMAP4_SSL(self.cfg["imap_host"])
            box.logout()
            return Health(status="ok")
        except Exception as e:
            return Health(status="down", detail=str(e))


@register("csv_drop")
class CsvDrop:
    """CostSource over a watched folder of `cost_<YYYYMMDD>.csv` (schemas/cost-csv.md)."""

    def __init__(self, conn: m.Connection) -> None:
        self.folder = Path(conn.config.get("folder", "./drop/cost"))

    def get_rows(self, since: str | None) -> list[CostRowIn]:
        seen: set[tuple[str, str, str, str, str]] = set()
        out = []
        for f in sorted(self.folder.glob("cost_*.csv")) if self.folder.exists() else []:
            if since and f.stem.split("_", 1)[1] <= since:
                continue
            with f.open(encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh):
                    key = (r["project_code"], r["wbs"], r["kind"], r["posted_at"], r["amount"])
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(CostRowIn(project_code=r["project_code"], wbs=r["wbs"], kind=r["kind"],
                                         amount=float(r["amount"]), currency=r["currency"],
                                         posted_at=date.fromisoformat(r["posted_at"]),
                                         ref=r.get("ref") or None, note=r.get("note") or None))
        return out

    def sync(self, since: str | None) -> SyncResult:
        rows = self.get_rows(since)
        files = sorted(self.folder.glob("cost_*.csv")) if self.folder.exists() else []
        return SyncResult(pulled=len(rows), cursor=files[-1].stem.split("_", 1)[1] if files else since)

    def health(self) -> Health:
        return Health(status="ok" if self.folder.exists() else "down", detail=str(self.folder))


@register("excel_list")
class ExcelList:
    """PlanSource over an xlsx with columns title, owner, due, done, package (one sheet = one plan)."""

    def __init__(self, conn: m.Connection) -> None:
        self.path = Path(conn.config.get("path", "./drop/plan.xlsx"))

    def _sheets(self) -> dict[str, Any]:
        import pandas as pd

        return dict(pd.read_excel(self.path, sheet_name=None))

    def list_plans(self) -> list[ExtPlan]:
        return [ExtPlan(id=k, name=k) for k in self._sheets()]

    def get_tasks(self, plan: str) -> list[Task]:
        df = self._sheets()[plan]
        out = []
        for i, r in df.iterrows():
            due = r.get("due")
            out.append(Task(id=f"{plan}:{i}", title=str(r.get("title", "")), bucket=str(r.get("package", "") or ""),
                            owner_email=str(r.get("owner", "") or "") or None,
                            due=datetime.combine(due.date(), datetime.min.time(), UTC) if hasattr(due, "date") else None,
                            done=str(r.get("done", "")).strip().lower() in ("1", "true", "yes", "x"),
                            labels=[s.strip() for s in str(r.get("labels", "") or "").split(",") if s.strip()]))
        return out

    def subscribe(self, plan: str) -> str | None:
        return None

    def sync(self, since: str | None) -> SyncResult:
        return SyncResult(pulled=sum(len(self.get_tasks(p.id)) for p in self.list_plans()))

    def health(self) -> Health:
        return Health(status="ok" if self.path.exists() else "down", detail=str(self.path))

    def add_comment(self, task: str, text: str) -> None:
        raise Forbidden("excel lists take no comments", limit="scope")


@register("local")
class LocalIdentity:
    """Local users table with argon2 hashes. Token = 'email:password' for solo mode."""

    def __init__(self, conn: m.Connection, store: Any = None) -> None:
        self.store = store

    def authenticate(self, token: str) -> Principal:
        from argon2 import PasswordHasher

        mail, _, pw = token.partition(":")
        if self.store is None:
            raise Forbidden("local identity needs the record", limit="login")
        user = self.store.find_one(m.User, email=mail)
        if user is None or not user.password_hash:
            raise Forbidden("unknown user", limit="login")
        PasswordHasher().verify(user.password_hash, pw)
        return Principal(subject=user.id, email=user.email, name=user.name)

    def groups(self, principal: Principal) -> list[str]:
        user = self.store.find_one(m.User, email=principal.email) if self.store else None
        return list(user.roles) if user else []


@register("mcp_generic")
class McpRetrieval:
    """RetrievalSource over any MCP server exposing search and read tools (names configurable)."""

    def __init__(self, conn: m.Connection, call: Any = None) -> None:
        self.conn = conn
        self.search_tool = conn.config.get("search_tool", "search")
        self.read_tool = conn.config.get("read_tool", "read")
        self._call = call

    def _invoke(self, tool: str, args: dict[str, Any]) -> Any:
        if self._call is not None:
            return self._call(tool, args)
        from gw.harness.adapters.mcp_client import call_tool

        return call_tool(self.conn.config, tool, args)

    def search(self, query: str, k: int, filters: dict[str, Any]) -> list[Hit]:
        res = self._invoke(self.search_tool, {"query": query, "k": k, **filters})
        return [Hit.model_validate({**h, "source": self.conn.id}) for h in (res or [])]

    def read(self, id: str, max_bytes: int) -> str:
        return str(self._invoke(self.read_tool, {"id": id}))[:max_bytes]

    def health(self) -> Health:
        try:
            self.search("health", 1, {})
            return Health(status="ok")
        except Exception as e:
            return Health(status="down", detail=str(e))
