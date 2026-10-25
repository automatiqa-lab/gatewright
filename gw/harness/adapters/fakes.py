"""Fakes for every contract.M1 (fakes satisfy the Protocols).

Fakes hold data in memory and can be seeded from a JSON fixture (`config.fixture`)
so a demo database survives a restart. `fail(n)` makes the next n health checks
and syncs fail, to exercise degraded/down.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gw.harness.contracts import (
    Activity,
    ApprovalCard,
    Baseline,
    CalEvent,
    CostRowIn,
    DCMAReport,
    DocChange,
    DocMeta,
    ExternalRef,
    ExtPlan,
    ExtProject,
    Health,
    Hit,
    InboundMessage,
    OutboundMessage,
    Prediction,
    Principal,
    ScheduleSourceBase,
    SyncResult,
    Task,
)
from gw.harness.registry import register
from gw.record import models as m


class _Failing:
    def __init__(self) -> None:
        self._fail = 0

    def fail(self, n: int = 1) -> None:
        self._fail = n

    def _check(self) -> None:
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError("fake failure")

    def health(self) -> Health:
        try:
            self._check()
            return Health(status="ok", detail="fake")
        except RuntimeError as e:
            return Health(status="down", detail=str(e))


def _fixture(conn: m.Connection | None) -> dict[str, Any]:
    if conn is None or not conn.config.get("fixture"):
        return {}
    p = Path(conn.config["fixture"])
    return dict(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else {}


@register("fake_schedule")
class FakeSchedule(ScheduleSourceBase, _Failing):
    def __init__(self, conn: m.Connection | None = None,
                 projects: dict[str, list[Activity]] | None = None, dcma: dict[str, int] | None = None) -> None:
        super().__init__()
        fx = _fixture(conn)
        self.projects = projects or {k: [Activity.model_validate(a) for a in v] for k, v in fx.get("projects", {}).items()}
        self.dcma = dcma or fx.get("dcma", {})

    def list_projects(self) -> list[ExtProject]:
        return [ExtProject(id=k, name=k, data_date=datetime.now(UTC)) for k in self.projects]

    def get_activities(self, project: str) -> list[Activity]:
        return list(self.projects.get(project, []))

    def get_milestones(self, project: str) -> list[Activity]:
        return [a for a in self.get_activities(project) if a.is_milestone]

    def get_baseline(self, project: str) -> Baseline | None:
        return None

    def dcma14(self, project: str) -> DCMAReport | None:
        return DCMAReport(passed=int(self.dcma[project])) if project in self.dcma else None

    def sync(self, since: str | None) -> SyncResult:
        self._check()
        return SyncResult(pulled=sum(len(v) for v in self.projects.values()), cursor=datetime.now(UTC).isoformat())


@register("fake_plan")
class FakePlan(_Failing):
    def __init__(self, conn: m.Connection | None = None, plans: dict[str, list[Task]] | None = None) -> None:
        super().__init__()
        fx = _fixture(conn)
        self.plans = plans or {k: [Task.model_validate(t) for t in v] for k, v in fx.get("plans", {}).items()}
        self.comments: list[tuple[str, str]] = []

    def list_plans(self) -> list[ExtPlan]:
        return [ExtPlan(id=k, name=k) for k in self.plans]

    def get_tasks(self, plan: str) -> list[Task]:
        return list(self.plans.get(plan, []))

    def subscribe(self, plan: str) -> str | None:
        return None

    def sync(self, since: str | None) -> SyncResult:
        self._check()
        return SyncResult(pulled=sum(len(v) for v in self.plans.values()))

    def add_comment(self, task: str, text: str) -> None:
        self.comments.append((task, text))


@register("fake_documents")
class FakeDocuments(_Failing):
    def __init__(self, conn: m.Connection | None = None, docs: dict[str, tuple[str, bytes]] | None = None) -> None:
        super().__init__()
        self.conn_id = conn.id if conn else "fake-docs"
        self.docs: dict[str, tuple[str, bytes, datetime]] = {}
        for k, (name, body) in (docs or {}).items():
            self.put(k, name, body)
        for k, v in _fixture(conn).get("docs", {}).items():
            self.put(k, v["name"], v["text"].encode(), datetime.fromisoformat(v["modified"]))

    def put(self, key: str, name: str, body: bytes, modified: datetime | None = None) -> None:
        self.docs[key] = (name, body, modified or datetime.now(UTC))

    def upload(self, name: str, data: bytes) -> dict[str, Any]:
        self.put(name, name, data)
        return {"name": name, "size": len(data)}

    def list_changes(self, cursor: str | None) -> tuple[list[DocChange], str]:
        since = datetime.fromisoformat(cursor) if cursor else None
        out = [DocChange(ref=ExternalRef(conn_id=self.conn_id, external_id=k, url=f"fake://{k}"), name=n, modified=t)
               for k, (n, _, t) in sorted(self.docs.items()) if since is None or t > since]
        newest = max((t for _, _, t in self.docs.values()), default=datetime.now(UTC))
        return out, newest.isoformat()

    def stat(self, ref: ExternalRef) -> DocMeta:
        n, b, t = self.docs[ref.external_id]
        return DocMeta(name=n, size=len(b), modified=t, version="1")

    def read_text(self, ref: ExternalRef, max_bytes: int) -> str:
        return self.docs[ref.external_id][1][:max_bytes].decode("utf-8", errors="replace")

    def hash(self, ref: ExternalRef) -> str:
        return hashlib.sha256(self.docs[ref.external_id][1]).hexdigest()

    def link(self, ref: ExternalRef) -> str:
        return f"fake://{ref.external_id}"

    def sync(self, since: str | None) -> SyncResult:
        self._check()
        ch, cur = self.list_changes(since)
        return SyncResult(pulled=len(ch), changed=len(ch), cursor=cur)


@register("fake_mail")
class FakeMessages(_Failing):
    """Inbound queue plus a recorded outbox and card list."""

    def __init__(self, conn: m.Connection | None = None, inbound: list[InboundMessage] | None = None) -> None:
        super().__init__()
        self.inbound = inbound or [InboundMessage.model_validate(x) for x in _fixture(conn).get("inbound", [])]
        self.outbox: list[OutboundMessage] = []
        self.cards: list[ApprovalCard] = []
        self.outbox_dir = Path(conn.config["outbox_dir"]) if conn and conn.config.get("outbox_dir") else None

    def pull(self, since: str | None) -> list[InboundMessage]:
        s = datetime.fromisoformat(since) if since else None
        return [x for x in self.inbound if s is None or x.received_at > s]

    def send(self, msg: OutboundMessage) -> str:
        self.outbox.append(msg)
        if self.outbox_dir:
            self.outbox_dir.mkdir(parents=True, exist_ok=True)
            name = msg.gw_ref.replace("/", "_") + ".txt"
            (self.outbox_dir / name).write_text(
                f"To: {', '.join(msg.to)}\nCc: {', '.join(msg.cc)}\nSubject: {msg.subject}\nX-GW-Ref: {msg.gw_ref}\n\n{msg.text}",
                encoding="utf-8")
        return msg.gw_ref

    def post_card(self, card: ApprovalCard) -> str:
        self.cards.append(card)
        return card.approval_id

    def deliver_to_self(self, subject: str, text: str, headers: dict[str, str]) -> None:
        n = len(self.inbound)
        self.inbound.append(InboundMessage(id=f"self-{n}", sender="demo-mask@example.com", subject=subject, text=text,
                                           received_at=datetime.now(UTC), headers={k.lower(): v for k, v in headers.items()}))

    def sync(self, since: str | None) -> SyncResult:
        self._check()
        msgs = self.pull(since)
        return SyncResult(pulled=len(msgs), cursor=max((x.received_at.isoformat() for x in msgs), default=since))


@register("fake_cost")
class FakeCost(_Failing):
    def __init__(self, conn: m.Connection | None = None, rows: list[CostRowIn] | None = None) -> None:
        super().__init__()
        self.rows = rows or []

    def get_rows(self, since: str | None) -> list[CostRowIn]:
        return [r for r in self.rows if since is None or r.posted_at.isoformat() > since]

    def sync(self, since: str | None) -> SyncResult:
        self._check()
        return SyncResult(pulled=len(self.get_rows(since)))


@register("fake_calendar")
class FakeCalendar(_Failing):
    def __init__(self, conn: m.Connection | None = None, events: list[CalEvent] | None = None) -> None:
        super().__init__()
        self.events = events or [CalEvent.model_validate(e) for e in _fixture(conn).get("events", [])]

    def list_events(self, window: tuple[datetime, datetime]) -> list[CalEvent]:
        return [e for e in self.events if e.end >= window[0] and e.start <= window[1]]

    def create_event(self, ev: CalEvent) -> str:
        ev.id = ev.id or f"ev{len(self.events) + 1}"
        self.events.append(ev)
        return ev.id


@register("fake_identity")
class FakeIdentity:
    def __init__(self, conn: m.Connection | None = None, users: dict[str, list[str]] | None = None) -> None:
        self.users = users or {}

    def authenticate(self, token: str) -> Principal:
        return Principal(subject=token, email=token, name=token)

    def groups(self, principal: Principal) -> list[str]:
        return self.users.get(principal.email, [])


@register("fake_retrieval")
class FakeRetrieval(_Failing):
    def __init__(self, conn: m.Connection | None = None, hits: list[Hit] | None = None) -> None:
        super().__init__()
        self.conn_id = conn.id if conn else "fake-rag"
        self.hits = hits or [Hit.model_validate(h) for h in _fixture(conn).get("hits", [])]

    def search(self, query: str, k: int, filters: dict[str, Any]) -> list[Hit]:
        words = set(query.lower().split())
        scored = [(len(words & set((h.title + " " + h.snippet).lower().split())), h) for h in self.hits]
        out = [h.model_copy(update={"score": float(s), "source": self.conn_id}) for s, h in sorted(scored, key=lambda x: -x[0]) if s]
        return out[:k]

    def read(self, id: str, max_bytes: int) -> str:
        return next((h.snippet for h in self.hits if h.id == id), "")[:max_bytes]


@register("fake_model")
class FakeModel(_Failing):
    """Deterministic ModelProvider. Records every prompt so redaction can be tested."""

    def __init__(self, conn: m.Connection | None = None, external: bool = False, kind: str = "llm",
                 responses: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.kind = kind
        self.external = external if conn is None else bool(conn.config.get("external", external))
        self.responses = responses or {}
        self.prompts: list[str] = []

    def complete(self, prompt: str, schema: dict[str, Any] | None, max_tokens: int) -> str | dict[str, Any]:
        self.prompts.append(prompt)
        for key, val in self.responses.items():
            if key in prompt:
                return val  # type: ignore[no-any-return]
        return {} if schema else "ok"

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.prompts.extend(texts)
        out = []
        for t in texts:
            h = hashlib.sha256(t.lower().encode()).digest()
            out.append([b / 255 for b in h[:16]])
        return out

    def tabular_fit_predict(self, X_ctx: Any, y_ctx: Any, X_query: Any, task: str) -> Prediction:
        mean = float(sum(y_ctx) / max(len(y_ctx), 1))
        return Prediction(p=[mean] * len(X_query), model_id="fake")

    def chat(self, request: Any) -> Any:
        """Scripted chat: `script` responses in order, else echo the <draft> block it was asked to rewrite."""
        import re as _re

        from gw.harness.contracts import ChatResponse, Usage

        self._check()
        self.requests = getattr(self, "requests", []) + [request]
        self.prompts.append("\n".join(x.content for x in request.messages))
        script = getattr(self, "script", None) or []
        if script:
            nxt = script.pop(0)
            return nxt if isinstance(nxt, ChatResponse) else ChatResponse.model_validate(nxt)
        last = request.messages[-1].content if request.messages else ""
        found = _re.search(r"<draft>\n?(.*?)\n?</draft>", last, _re.S)
        return ChatResponse(text=found.group(1) if found else "ok", usage=Usage(input_tokens=len(last) // 4,
                                                                                    output_tokens=20))

    def is_external(self) -> bool:
        return self.external
