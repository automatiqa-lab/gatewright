"""SQL tables derived from the Pydantic entities.

Only dialect-neutral column types are used so the same metadata runs on DuckDB
(solo) and Postgres (team).
"""

from __future__ import annotations

import types
import typing
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel

from gw.record import models as m

metadata = sa.MetaData()

_event_seq = sa.Sequence("events_seq")

events = sa.Table(
    "events",
    metadata,
    sa.Column("seq", sa.BigInteger, _event_seq, server_default=_event_seq.next_value(), primary_key=True),
    sa.Column("at", sa.DateTime, nullable=False, quote=True),
    sa.Column("actor", sa.String, nullable=False),
    sa.Column("entity_type", sa.String, nullable=False),
    sa.Column("entity_id", sa.String, nullable=False),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("payload_json", sa.JSON),
    sa.Column("agent_run_id", sa.String),
)


def _unwrap(annotation: Any) -> tuple[Any, bool]:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return (args[0] if len(args) == 1 else args[0]), True
    return annotation, False


def _column_type(annotation: Any) -> sa.types.TypeEngine[Any]:
    base, _ = _unwrap(annotation)
    origin = typing.get_origin(base)
    if origin in (list, dict) or base in (list, dict) or (isinstance(base, type) and issubclass(base, BaseModel)):
        return sa.JSON()
    if base is Any:
        return sa.JSON()
    if isinstance(base, type) and issubclass(base, Enum):
        return sa.String()
    if base is bool:
        return sa.Boolean()
    if base is int:
        return sa.BigInteger()
    if base is float:
        return sa.Double()
    if base is datetime:
        return sa.DateTime()
    if base is date:
        return sa.Date()
    return sa.String()


def table_for(model: type[BaseModel], name: str, pk: str | tuple[str, ...] = "id") -> sa.Table:
    """Build a table whose columns mirror the model fields."""
    pks = (pk,) if isinstance(pk, str) else pk
    cols = []
    for fname, field in model.model_fields.items():
        cols.append(sa.Column(fname, _column_type(field.annotation), primary_key=fname in pks, quote=True))
    return sa.Table(name, metadata, *cols)


ENTITY_TABLES: dict[type[BaseModel], sa.Table] = {
    m.Project: table_for(m.Project, "projects"),
    m.GateInstance: table_for(m.GateInstance, "gate_instances"),
    m.WorkPackage: table_for(m.WorkPackage, "work_packages"),
    m.Commitment: table_for(m.Commitment, "commitments"),
    m.Party: table_for(m.Party, "parties"),
    m.Evidence: table_for(m.Evidence, "evidence"),
    m.Risk: table_for(m.Risk, "risks"),
    m.Decision: table_for(m.Decision, "decisions"),
    m.Dependency: table_for(m.Dependency, "dependencies"),
    m.AgentRun: table_for(m.AgentRun, "agent_runs"),
    m.AgentOutput: table_for(m.AgentOutput, "agent_outputs"),
    m.Approval: table_for(m.Approval, "approvals"),
    m.User: table_for(m.User, "users"),
    m.Lesson: table_for(m.Lesson, "lessons"),
    m.Connection: table_for(m.Connection, "connections"),
    m.InboundItem: table_for(m.InboundItem, "inbound_items"),
    m.SemanticExample: table_for(m.SemanticExample, "semantic_examples"),
    m.ScheduleSnapshot: table_for(m.ScheduleSnapshot, "schedule_snapshots"),
    m.CostRow: table_for(m.CostRow, "cost_rows"),
    m.Chase: table_for(m.Chase, "chases"),
    m.FeatureRow: table_for(m.FeatureRow, "features"),
    m.ForecastRow: table_for(m.ForecastRow, "forecasts"),
    m.CalibrationRow: table_for(m.CalibrationRow, "calibration"),
    m.PolicyRevision: table_for(m.PolicyRevision, "policy_revisions"),
    m.GateModelRevision: table_for(m.GateModelRevision, "gate_model_revisions"),
    m.SearchDoc: table_for(m.SearchDoc, "search_index"),
    m.Meta: table_for(m.Meta, "meta", pk="key"),
}

signals = table_for(m.SignalRow, "signals", pk=())

JSON_FIELDS: dict[type[BaseModel], set[str]] = {
    model: {c.name for c in table.columns if isinstance(c.type, sa.JSON)}
    for model, table in ENTITY_TABLES.items()
}
JSON_FIELDS[m.SignalRow] = {"threshold", "inputs"}


def _naive_utc(v: Any) -> Any:
    if isinstance(v, datetime) and v.tzinfo is not None:
        return v.astimezone(UTC).replace(tzinfo=None)
    return v


def to_row(entity: BaseModel) -> dict[str, Any]:
    """Serialise an entity into a table row (JSON fields in JSON mode, UTC naive datetimes)."""
    json_fields = JSON_FIELDS.get(type(entity), set())
    row: dict[str, Any] = {}
    for fname in type(entity).model_fields:
        value = getattr(entity, fname)
        if fname in json_fields:
            row[fname] = entity.model_dump(mode="json", include={fname})[fname]
        elif isinstance(value, Enum):
            row[fname] = value.value
        else:
            row[fname] = _naive_utc(value)
    return row


def from_row[T: BaseModel](model: type[T], row: Any) -> T:
    """Rehydrate a model from a row; datetimes come back as UTC-aware."""
    data = {}
    for k, v in row.items():
        if isinstance(v, datetime) and v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        data[k] = v
    return model.model_validate(data)
