"""Capability contracts.

Contracts are runtime-checkable Protocols; adapters are thin classes that fetch
and normalise. The harness (not the adapter) enforces the hard limits.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from gw.errors import Forbidden
from gw.record import clock

# --- common types ------------------------------------------------------------------


class ExternalRef(BaseModel):
    conn_id: str
    external_id: str
    url: str | None = None


class SyncResult(BaseModel):
    pulled: int = 0
    changed: int = 0
    errors: list[str] = Field(default_factory=list)
    cursor: str | None = None


class Health(BaseModel):
    status: Literal["ok", "degraded", "down"]
    detail: str = ""
    checked_at: datetime = Field(default_factory=clock.now)


class ExtProject(BaseModel):
    id: str
    name: str
    data_date: datetime | None = None
    baseline: bool = False


class Activity(BaseModel):
    id: str
    name: str
    wbs: str = ""
    start: datetime | None = None
    finish: datetime | None = None
    total_float: float = 0.0
    free_float: float = 0.0
    pct: float = 0.0
    preds: list[str] = Field(default_factory=list)
    is_milestone: bool = False
    is_critical: bool = False


class Baseline(BaseModel):
    project: str
    finish: datetime | None = None
    activities: list[Activity] = Field(default_factory=list)


class DCMAReport(BaseModel):
    passed: int
    total: int = 14
    failing: list[str] = Field(default_factory=list)


class ExtPlan(BaseModel):
    id: str
    name: str


class Task(BaseModel):
    id: str
    title: str
    bucket: str = ""
    owner_email: str | None = None
    due: datetime | None = None
    done: bool = False
    pct: float = 0.0
    checklist: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    url: str | None = None


class DocChange(BaseModel):
    ref: ExternalRef
    name: str
    modified: datetime
    deleted: bool = False


class DocMeta(BaseModel):
    name: str
    size: int
    modified: datetime
    version: str | None = None
    hash: str | None = None


class InboundMessage(BaseModel):
    id: str
    sender: str
    to: list[str] = Field(default_factory=list)
    subject: str = ""
    text: str = ""
    received_at: datetime
    thread_id: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    attachments: list[ExternalRef] = Field(default_factory=list)


class OutboundMessage(BaseModel):
    to: list[str]
    cc: list[str] = Field(default_factory=list)
    subject: str
    text: str
    gw_ref: str
    party_id: str
    approval_id: str | None = None
    ai_generated: dict[str, Any] | None = None  # Art. 50 envelope when a model wrote the text


class ApprovalCard(BaseModel):
    approval_id: str
    title: str
    body: str
    actions: list[str] = Field(default_factory=lambda: ["Approve", "Edit", "Hold"])
    token: str = ""


class CostRowIn(BaseModel):
    project_code: str
    wbs: str
    kind: str
    amount: float
    currency: str
    posted_at: date
    ref: str | None = None
    note: str | None = None


class CalEvent(BaseModel):
    id: str | None = None
    title: str
    start: datetime
    end: datetime
    kind: str = "other"  # gate_review | steerco | outage | other


class Principal(BaseModel):
    subject: str
    email: str
    name: str = ""


class Hit(BaseModel):
    id: str
    title: str
    snippet: str = ""
    url: str | None = None
    score: float = 0.0
    source: str = "local"


class Prediction(BaseModel):
    p: list[float] = Field(default_factory=list)
    model_id: str = ""


# --- contracts ---------------------------------------------------------------------


@runtime_checkable
class ScheduleSource(Protocol):
    """ - networked schedules with logic and float."""

    def list_projects(self) -> list[ExtProject]: ...
    def get_activities(self, project: str) -> list[Activity]: ...
    def get_milestones(self, project: str) -> list[Activity]: ...
    def get_baseline(self, project: str) -> Baseline | None: ...
    def dcma14(self, project: str) -> DCMAReport | None: ...
    def sync(self, since: str | None) -> SyncResult: ...
    def health(self) -> Health: ...
    def set_activity_dates(self, *args: Any, **kwargs: Any) -> None: ...


@runtime_checkable
class PlanSource(Protocol):
    """ - task lists without logic."""

    def list_plans(self) -> list[ExtPlan]: ...
    def get_tasks(self, plan: str) -> list[Task]: ...
    def subscribe(self, plan: str) -> str | None: ...
    def sync(self, since: str | None) -> SyncResult: ...
    def health(self) -> Health: ...
    def add_comment(self, task: str, text: str) -> None: ...


@runtime_checkable
class DocumentSource(Protocol):
    """ - bodies are read for classification and discarded."""

    def list_changes(self, cursor: str | None) -> tuple[list[DocChange], str]: ...
    def stat(self, ref: ExternalRef) -> DocMeta: ...
    def read_text(self, ref: ExternalRef, max_bytes: int) -> str: ...
    def hash(self, ref: ExternalRef) -> str: ...
    def link(self, ref: ExternalRef) -> str: ...
    def sync(self, since: str | None) -> SyncResult: ...
    def health(self) -> Health: ...


@runtime_checkable
class MessageChannel(Protocol):

    def pull(self, since: str | None) -> list[InboundMessage]: ...
    def send(self, msg: OutboundMessage) -> str: ...
    def post_card(self, card: ApprovalCard) -> str: ...
    def sync(self, since: str | None) -> SyncResult: ...
    def health(self) -> Health: ...


@runtime_checkable
class CostSource(Protocol):

    def get_rows(self, since: str | None) -> list[CostRowIn]: ...
    def sync(self, since: str | None) -> SyncResult: ...
    def health(self) -> Health: ...


@runtime_checkable
class CalendarSource(Protocol):

    def list_events(self, window: tuple[datetime, datetime]) -> list[CalEvent]: ...
    def create_event(self, ev: CalEvent) -> str: ...
    def health(self) -> Health: ...


@runtime_checkable
class IdentitySource(Protocol):

    def authenticate(self, token: str) -> Principal: ...
    def groups(self, principal: Principal) -> list[str]: ...


@runtime_checkable
class RetrievalSource(Protocol):

    def search(self, query: str, k: int, filters: dict[str, Any]) -> list[Hit]: ...
    def read(self, id: str, max_bytes: int) -> str: ...
    def health(self) -> Health: ...


@runtime_checkable
class ModelProvider(Protocol):

    kind: str

    def complete(self, prompt: str, schema: dict[str, Any] | None, max_tokens: int) -> str | dict[str, Any]: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def tabular_fit_predict(self, X_ctx: Any, y_ctx: Any, X_query: Any, task: str) -> Prediction: ...
    def is_external(self) -> bool: ...
    def health(self) -> Health: ...


# --- model-neutral chat -----------------------------------------------
# The engine speaks only these types. Each LLM adapter translates them to its provider's wire
# format; no engine or agent code names a provider or a model.


class ToolSpec(BaseModel):
    """A tool the model may call. `parameters` is a JSON Schema object with additionalProperties false."""

    name: str
    description: str
    parameters: dict[str, Any]


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    """One turn. `tool_results` answer the previous assistant turn's `tool_calls` by id.
    `native` carries the provider's own assistant content so a tool loop can echo it back unchanged."""

    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    native: Any = None


class ChatRequest(BaseModel):
    system: str = ""
    messages: list[ChatMessage]
    tools: list[ToolSpec] = Field(default_factory=list)
    json_schema: dict[str, Any] | None = None  # structured output; mutually exclusive with tools
    max_tokens: int = 1500
    effort: Literal["low", "medium", "high"] | None = None  # a hint; adapters map or ignore it


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class ChatResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop: Literal["end", "tool", "length", "refused", "error"] = "end"
    usage: Usage = Field(default_factory=Usage)
    native: Any = None  # provider-native assistant content, echoed back in a tool loop

    def parsed(self) -> dict[str, Any]:
        """The structured-output payload, or {} when the text is not a JSON object."""
        import json as _json
        import re as _re

        found = _re.search(r"\{.*\}", self.text, _re.S)
        try:
            data = _json.loads(found.group(0)) if found else {}
        except ValueError:
            return {}
        return dict(data) if isinstance(data, dict) else {}


@runtime_checkable
class ChatModel(Protocol):
    """: a language model behind the neutral chat contract."""

    def chat(self, request: ChatRequest) -> ChatResponse: ...
    def is_external(self) -> bool: ...
    def health(self) -> Health: ...


CONTRACTS: dict[str, type] = {
    "ScheduleSource": ScheduleSource, "PlanSource": PlanSource, "DocumentSource": DocumentSource,
    "MessageChannel": MessageChannel, "CostSource": CostSource, "CalendarSource": CalendarSource,
    "IdentitySource": IdentitySource, "RetrievalSource": RetrievalSource, "ModelProvider": ModelProvider,
}


class ScheduleSourceBase:
    """Mixin for every ScheduleSource adapter: writes are declared and always refused.

    Hard limit 1. The harness proxy refuses too, so an
    adapter that forgets this base still cannot write.
    """

    def set_activity_dates(self, *args: Any, **kwargs: Any) -> None:
        raise Forbidden("Gatewright never writes to a schedule tool", limit="no_schedule_writes")
