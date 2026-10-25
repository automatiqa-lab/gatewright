"""Entities of the record.

Every entity is a Pydantic model; the SQL tables are derived from these models in
`gw.record.tables` so the two never drift.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class Entity(BaseModel):
    """Base for every stored entity."""

    model_config = ConfigDict(use_enum_values=True, validate_assignment=True)
    id: str


# --- enums -----------------------------------------------------------------


class Archetype(StrEnum):
    greenfield = "greenfield"
    brownfield = "brownfield"
    extension = "extension"
    site_systems = "site_systems"
    compliance = "compliance"
    urban = "urban"
    other = "other"


class GateDecision(StrEnum):
    pending = "pending"
    passed = "pass"
    pass_with_actions = "pass_with_actions"
    hold = "hold"
    stop = "stop"


class CommitmentKind(StrEnum):
    gate_deliverable = "gate_deliverable"
    milestone = "milestone"
    action = "action"
    plan_task = "plan_task"
    tender_item = "tender_item"


class CommitmentStatus(StrEnum):
    open = "open"
    confirmed = "confirmed"
    flagged = "flagged"
    overdue = "overdue"
    submitted = "submitted"
    accepted = "accepted"
    waived = "waived"
    cancelled = "cancelled"


class PartyKind(StrEnum):
    internal_team = "internal_team"
    vendor = "vendor"
    contractor = "contractor"
    consultant = "consultant"
    authority = "authority"
    insurer = "insurer"
    utility = "utility"
    sponsor = "sponsor"
    other = "other"


class EvidenceKind(StrEnum):
    document = "document"
    permit_ref = "permit_ref"
    test_report = "test_report"
    link = "link"
    statement = "statement"


class TenderState(StrEnum):
    none = "none"
    spec = "spec"
    technical = "technical"
    commercial = "commercial"
    awarded = "awarded"


class RiskStatus(StrEnum):
    open = "open"
    mitigating = "mitigating"
    closed = "closed"
    realised = "realised"


class DependencyKind(StrEnum):
    shared_party = "shared_party"
    shared_person = "shared_person"
    sequence = "sequence"
    shared_slot = "shared_slot"
    shared_document = "shared_document"
    outage_window = "outage_window"
    permit_office = "permit_office"


class DependencyStatus(StrEnum):
    proposed = "proposed"
    accepted = "accepted"
    rejected = "rejected"


class AgentName(StrEnum):
    chaser = "chaser"
    gatekeeper = "gatekeeper"
    scout = "scout"
    tender = "tender"
    brief = "brief"
    reporter = "reporter"
    semantic = "semantic"
    forecast = "forecast"
    assistant = "assistant"


class AutonomyLevel(StrEnum):
    observe = "observe"
    propose = "propose"
    draft = "draft"
    act = "act"


class RunOutcome(StrEnum):
    running = "running"
    applied = "applied"
    drafted = "drafted"
    proposed = "proposed"
    rejected = "rejected"
    failed = "failed"


class ApprovalKind(StrEnum):
    escalation_l3 = "escalation_l3"
    gate_pack = "gate_pack"
    send_external = "send_external"
    dependency = "dependency"
    review_queue_item = "review_queue_item"
    policy_change = "policy_change"


class ApprovalDecision(StrEnum):
    pending = "pending"
    approved = "approved"
    approved_edited = "approved_edited"
    rejected = "rejected"


class Contract(StrEnum):
    ScheduleSource = "ScheduleSource"
    PlanSource = "PlanSource"
    DocumentSource = "DocumentSource"
    MessageChannel = "MessageChannel"
    CostSource = "CostSource"
    CalendarSource = "CalendarSource"
    IdentitySource = "IdentitySource"
    RetrievalSource = "RetrievalSource"
    ModelProvider = "ModelProvider"


class Role(StrEnum):
    portfolio_lead = "portfolio_lead"
    project_manager = "project_manager"
    sme = "sme"
    party_contact = "party_contact"
    viewer = "viewer"
    admin = "admin"


# --- value objects -----------------------------------------------------------


class ExtRef(BaseModel):
    """A reference into a connection: conn id plus external id."""

    conn_id: str
    external_id: str


class Contact(BaseModel):
    name: str
    email: str
    channel_pref: str = "mail"


class ResponseProfile(BaseModel):
    """Derived"""

    median_reply_days: float = 0.0
    silence_rate: float = 0.0
    flag_rate: float = 0.0
    sample_n: int = 0


class DeliverableSpec(BaseModel):
    """A deliverable copied from the gate model onto a gate instance at creation."""

    id: str
    name: str
    weight: float
    mandatory: bool = False
    evidence: str = "document"
    accept_roles: list[str] = Field(default_factory=list)
    party_role: str | None = None
    standard: str | None = None
    check: str | None = None
    tags: list[str] = Field(default_factory=list)


# --- entities ------------------------------------------------------------------


class Project(Entity):
    code: str
    name: str
    site: str
    country: str
    zone: str
    archetype: Archetype
    size: str = ""
    sponsor_user_id: str | None = None
    owner_user_id: str | None = None
    gate_model_id: str
    gate_model_rev: int = 0
    current_gate_instance_id: str | None = None
    estimate_class: int = 5
    capex_amount: float = 0.0
    capex_currency: str = "EUR"
    contingency_amount: float = 0.0
    contingency_drawn: float = 0.0
    schedule_source_conn_id: str | None = None
    plan_source_conn_ids: list[str] = Field(default_factory=list)
    baseline_finish: date | None = None
    forecast_finish: date | None = None
    start_date: date | None = None
    created_at: datetime
    archived_at: datetime | None = None


class GateInstance(Entity):
    project_id: str
    gate_model_id: str
    gate_model_rev: int
    gate_id: str
    name: str
    order: int
    planned_review_at: datetime
    actual_review_at: datetime | None = None
    threshold: float
    decided_by_roles: list[str] = Field(default_factory=list)
    decisions_allowed: list[str] = Field(default_factory=list)
    lead_time_days: int = 10
    actions_due_days: int = 30
    deliverables: list[DeliverableSpec] = Field(default_factory=list)
    readiness: float = 0.0
    decision: GateDecision = GateDecision.pending
    decided_by_user_id: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None


class WorkPackage(Entity):
    project_id: str
    code: str
    name: str
    discipline: str
    owner_user_id: str | None = None
    party_id: str | None = None
    tender_state: TenderState = TenderState.none
    schedule_activity_refs: list[ExtRef] = Field(default_factory=list)
    plan_task_refs: list[ExtRef] = Field(default_factory=list)
    window_start: date | None = None
    window_end: date | None = None


class Commitment(Entity):
    code: str
    project_id: str
    gate_instance_id: str | None = None
    work_package_id: str | None = None
    deliverable_id: str
    title: str
    kind: CommitmentKind
    owner_user_id: str
    party_id: str
    due_at: datetime
    original_due_at: datetime
    status: CommitmentStatus = CommitmentStatus.open
    evidence_id: str | None = None
    escalation_level: int = 0
    chase_count: int = 0
    last_chase_at: datetime | None = None
    first_unanswered_chase_at: datetime | None = None
    last_reply_at: datetime | None = None
    closed_at: datetime | None = None
    source_ref: ExtRef | None = None
    weight: float = 0.0
    mandatory: bool = False
    tags: list[str] = Field(default_factory=list)
    created_at: datetime


class Party(Entity):
    name: str
    kind: PartyKind
    country: str
    tz: str = "UTC"
    contacts: list[Contact] = Field(default_factory=list)
    response_profile: ResponseProfile = Field(default_factory=ResponseProfile)
    behaviour: str = "prompt"
    notes: str = ""


class Evidence(Entity):
    commitment_id: str
    kind: EvidenceKind
    uri: str
    title: str
    external_id: str | None = None
    revision: str | None = None
    sha256: str | None = None
    size: int | None = None
    received_at: datetime
    source_conn_id: str | None = None
    accepted_by_user_id: str | None = None
    accepted_at: datetime | None = None
    rejected_reason: str | None = None


class Risk(Entity):
    project_id: str
    title: str
    cause: str = ""
    effect: str = ""
    owner_user_id: str | None = None
    trigger: str = ""
    status: RiskStatus = RiskStatus.open
    contingency_reserved: float = 0.0
    contingency_drawn: float = 0.0
    linked_commitment_ids: list[str] = Field(default_factory=list)
    linked_lesson_ids: list[str] = Field(default_factory=list)


class Decision(Entity):
    project_id: str
    gate_instance_id: str | None = None
    title: str
    decided_by_user_id: str
    decided_at: datetime
    options_considered: list[str] = Field(default_factory=list)
    chosen: str
    rationale: str
    cost_of_delay_note: str | None = None


class Dependency(Entity):
    from_work_package_id: str
    to_work_package_id: str
    kind: DependencyKind
    proposed_by: str = "user"
    status: DependencyStatus = DependencyStatus.proposed
    decided_by_user_id: str | None = None
    decided_at: datetime | None = None
    reason: str = ""
    saved_days_estimate: float | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: datetime


class AgentRun(Entity):
    agent: AgentName
    trigger: str = "manual"
    started_at: datetime
    finished_at: datetime | None = None
    input_snapshot_ref: int = 0
    output_type: str = ""
    output_ref: str | None = None
    autonomy_level: AutonomyLevel = AutonomyLevel.observe
    needs_approval: bool = False
    approval_id: str | None = None
    cost_tokens: int = 0
    cost_seconds: float = 0.0
    outcome: RunOutcome = RunOutcome.running
    summary: str = ""
    error: str | None = None


class AgentOutput(Entity):
    """A stored agent output (draft, proposal or applied result) with citations."""

    agent_run_id: str
    agent: AgentName
    output_type: str
    disposition: str  # apply | draft | propose | observe
    payload: dict[str, Any] = Field(default_factory=dict)
    citations: list[str] = Field(default_factory=list)
    created_at: datetime

    @field_validator("payload")
    @classmethod
    def _disclose(cls, v: dict[str, Any], info: ValidationInfo) -> dict[str, Any]:
        """Art. 50 marking at the constructor: model-written keys get the envelope (COMPLIANCE.md)."""
        from gw.governance.disclosure import apply_declaration

        return apply_declaration(v, str(info.data.get("disposition", "")))


class Approval(Entity):
    agent_run_id: str
    output_id: str | None = None
    kind: ApprovalKind
    title: str = ""
    requested_at: datetime
    decided_by_user_id: str | None = None
    decided_at: datetime | None = None
    decision: ApprovalDecision = ApprovalDecision.pending
    edited_output_ref: str | None = None


class User(Entity):
    email: str
    name: str
    short: str = ""
    roles: list[str] = Field(default_factory=list)
    tz: str = "UTC"
    password_hash: str | None = None


class Lesson(Entity):
    code: str
    title: str
    text: str
    source_project_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    cited_count: int = 0


class Connection(Entity):
    contract: Contract
    adapter: str
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    config_ref: str | None = None
    scopes: list[str] = Field(default_factory=list)
    owner: str = ""
    sync_every_minutes: int = 15
    mapping: dict[str, Any] = Field(default_factory=dict)
    health: str = "ok"
    consecutive_failures: int = 0
    last_sync_at: datetime | None = None
    last_error: str | None = None
    cursor: str | None = None


# --- support tables ------------------------------------------------------------


class InboundItem(Entity):
    """Raw pulled item."""

    conn_id: str
    external_id: str
    kind: str  # message | document | comment
    received_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    processed_at: datetime | None = None
    classification: dict[str, Any] | None = None
    review_state: str | None = None  # None | pending | resolved
    commitment_id: str | None = None


class SemanticExample(Entity):
    text_hash: str
    text: str
    head: str  # intent | doc_type
    label: str
    chosen_link: str | None = None
    user_id: str | None = None
    at: datetime


class SignalRow(BaseModel):
    """Signal history row. Signal."""

    signal_id: str
    scope: str
    scope_id: str
    computed_at: datetime
    value: float
    threshold: list[float] | None = None
    trend_7d: float | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)


class ScheduleSnapshot(Entity):
    """One schedule sync for one project: critical float and DCMA result."""

    project_id: str
    conn_id: str
    data_date: datetime
    synced_at: datetime
    min_float_critical: float
    dcma_passed: int | None = None
    activity_count: int = 0
    changed: bool = True


class CostRow(Entity):
    project_code: str
    wbs: str
    kind: str  # commitment | actual | contingency_draw | estimate
    amount: float
    currency: str
    posted_at: date
    ref: str | None = None
    note: str | None = None


class Chase(Entity):
    """One reminder or escalation for one commitment (sent, queued or drafted)."""

    commitment_id: str
    project_id: str
    party_id: str
    level: int
    step: int = 0
    channel: str = "mail"
    status: str = "sent"  # sent | queued | drafted | war_room
    planned_for: datetime
    sent_at: datetime | None = None
    replied_at: datetime | None = None
    reply_intent: str | None = None
    gw_ref: str = ""
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str = ""
    text: str = ""
    agent_run_id: str | None = None
    approval_id: str | None = None


class FeatureRow(Entity):
    """Forecast feature row for a closed or open target."""

    target: str
    target_id: str
    as_of: datetime
    features: dict[str, Any] = Field(default_factory=dict)
    label: float | None = None
    delay_days: float | None = None
    synthetic: bool = False


class ForecastRow(Entity):
    target: str
    target_id: str
    run_id: str
    model_id: str
    context_size: int
    feature_hash: str
    p: float
    p50_days: float | None = None
    p90_days: float | None = None
    drivers: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


class CalibrationRow(Entity):
    model_id: str
    computed_at: datetime
    n: int
    brier: float
    ece: float
    table: list[dict[str, Any]] = Field(default_factory=list)


class PolicyRevision(Entity):
    rev: int
    yaml_text: str
    author: str
    note: str = ""
    diff: str = ""
    created_at: datetime


class GateModelRevision(Entity):
    model_id: str
    rev: int
    yaml_text: str
    yaml_hash: str
    published_by: str
    published_at: datetime


class SearchDoc(Entity):
    """One retrieval chunk = one entity; `id` is the entity id."""

    kind: str
    title: str
    text: str
    url: str
    text_hash: str
    model: str
    vector: list[float] = Field(default_factory=list)
    updated_at: datetime


class Meta(BaseModel):
    key: str
    value: str
