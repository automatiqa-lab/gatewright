"""Gate model load, validate, publish and revisions.

Gate models are data: nothing in code names a gate, a threshold or a deliverable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from pydantic import BaseModel, Field

from gw import paths
from gw.errors import Unprocessable
from gw.record import clock
from gw.record import models as m
from gw.record.ids import new_id
from gw.record.store import Store
from gw.standards.checks import validate_check

KNOWN_ROLES = {"portfolio_lead", "project_manager", "project_director", "site_lead", "sponsor",
               "investment_committee", "admin"}
ALLOWED_DECISIONS = {"pass", "pass_with_actions", "hold", "stop"}
ARCHETYPES = {a.value for a in m.Archetype}


class Gate(BaseModel):
    id: str
    name: str
    order: int
    purpose: str = ""
    threshold: float
    decisions: list[str]
    decided_by: list[str]
    lead_time_days: int = 10
    actions_due_days: int = 30
    deliverables: list[m.DeliverableSpec]


class GateModel(BaseModel):
    """A parsed gate model."""

    id: str
    name: str
    rev: int = 0
    applies_to: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    gates: list[Gate]

    def gate(self, gate_id: str) -> Gate:
        for g in self.gates:
            if g.id == gate_id:
                return g
        raise KeyError(f"{self.id}: no gate {gate_id}")

    def next_gate(self, gate_id: str) -> Gate | None:
        ordered = sorted(self.gates, key=lambda g: g.order)
        for i, g in enumerate(ordered):
            if g.id == gate_id:
                return ordered[i + 1] if i + 1 < len(ordered) else None
        return None


@dataclass
class ValidationReport:
    errors: list[tuple[str, str]] = field(default_factory=list)  # (json pointer, message)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@cache
def _schema() -> dict[str, Any]:
    return dict(json.loads((paths.SCHEMAS / "gatemodel.schema.json").read_text(encoding="utf-8")))


def _pointer(path: Any) -> str:
    return "/" + "/".join(str(p) for p in path) if path else ""


def validate(data: dict[str, Any]) -> ValidationReport:
    """Schema plus the rules"""
    rep = ValidationReport()
    validator = jsonschema.Draft202012Validator(_schema())
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        msg = err.message
        if "does not match" in msg and "accept_roles" in [str(p) for p in err.absolute_path]:
            msg = f"unknown role {err.instance!r}"
        rep.errors.append((_pointer(err.absolute_path), msg))
    gates = data.get("gates") or []
    if not isinstance(gates, list):
        return rep
    ids = [g.get("id") for g in gates if isinstance(g, dict)]
    if len(ids) != len(set(ids)):
        rep.errors.append(("/gates", "gate ids must be unique"))
    orders = [g.get("order") for g in gates if isinstance(g, dict)]
    ints = [o for o in orders if isinstance(o, int)]
    if len(ints) != len(orders) or any(b <= a for a, b in zip(ints, ints[1:], strict=False)):
        rep.errors.append(("/gates", "order must be strictly increasing"))
    for gi, g in enumerate(gates):
        if not isinstance(g, dict):
            continue
        dels = g.get("deliverables") or []
        total = sum(float(d.get("weight", 0)) for d in dels if isinstance(d, dict))
        if dels and abs(total - 100) > 1e-6:
            rep.warnings.append(f"{g.get('id')}: deliverable weights sum to {total:g}, not 100; normalised at scoring")
        for di, d in enumerate(dels):
            if not isinstance(d, dict):
                continue
            for role in d.get("accept_roles") or []:
                if role not in KNOWN_ROLES and not str(role).startswith("sme:"):
                    rep.errors.append((f"/gates/{gi}/deliverables/{di}/accept_roles", f"unknown role {role!r}"))
            check = d.get("check")
            if check:
                for e in validate_check(check):
                    rep.errors.append((f"/gates/{gi}/deliverables/{di}/check", e))
    # de-duplicate (schema pattern and rule may both fire on the same role)
    rep.errors = list(dict.fromkeys(rep.errors))
    return rep


def parse(text: str) -> tuple[GateModel, ValidationReport]:
    """Parse YAML text; raises Unprocessable with a JSON pointer on errors."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise Unprocessable("gate model must be a mapping", pointer="")
    rep = validate(data)
    if not rep.ok:
        pointer, msg = rep.errors[0]
        raise Unprocessable(f"{msg} (+{len(rep.errors) - 1} more)" if len(rep.errors) > 1 else msg,
                            pointer=pointer, warnings=rep.warnings)
    return GateModel.model_validate(data), rep


def load_file(path: str | Path) -> GateModel:
    model, _ = parse(Path(path).read_text(encoding="utf-8"))
    return model


def shipped_files() -> list[Path]:
    """Gate model YAML files shipped in examples/gatemodels/ (rev 0)."""
    return sorted(paths.GATEMODELS.glob("*.yaml"))


# --- revisions (stored in the record) -------------------------------------------------


def publish(store: Store, text: str, user: str) -> GateModel:
    """Store a new immutable revision; rev = previous + 1 (rev 0 = the shipped file)."""
    model, _ = parse(text)
    existing = store.find(m.GateModelRevision, model_id=model.id, order_by="-rev", limit=1)
    rev = existing[0].rev + 1 if existing else 0
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    model.rev = rev
    store.insert(m.GateModelRevision(
        id=new_id("GMR"), model_id=model.id, rev=rev, yaml_text=text, yaml_hash=h,
        published_by=user, published_at=clock.now(),
    ), actor=f"user:{user}" if not user.startswith(("system", "user:")) else user, kind="published",
        payload={"model_id": model.id, "rev": rev, "hash": h})
    return model


def install_shipped(store: Store) -> list[GateModel]:
    """Publish rev 0 of every shipped model that is not yet in the record."""
    out = []
    for f in shipped_files():
        model, _ = parse(f.read_text(encoding="utf-8"))
        if not store.find(m.GateModelRevision, model_id=model.id, limit=1):
            out.append(publish(store, f.read_text(encoding="utf-8"), "system"))
    return out


def get(store: Store, model_id: str, rev: int | None = None) -> GateModel:
    """A stored revision (latest when rev is None)."""
    where: dict[str, Any] = {"model_id": model_id}
    if rev is not None:
        where["rev"] = rev
    rows = store.find(m.GateModelRevision, order_by="-rev", limit=1, **where)
    if not rows:
        from gw.errors import NotFound

        raise NotFound(f"gate model {model_id} rev {rev} not found")
    model, _ = parse(rows[0].yaml_text)
    model.rev = rows[0].rev
    return model


def list_models(store: Store) -> list[m.GateModelRevision]:
    latest: dict[str, m.GateModelRevision] = {}
    for r in store.find(m.GateModelRevision, order_by="rev"):
        latest[r.model_id] = r
    return list(latest.values())


def instance_from_gate(project: m.Project, model: GateModel, gate: Gate, planned_review_at: Any,
                       threshold_override: float | None = None) -> m.GateInstance:
    """Create a GateInstance that copies threshold, deliverables and weights."""
    return m.GateInstance(
        id=new_id("GateInstance"), project_id=project.id, gate_model_id=model.id,
        gate_model_rev=model.rev, gate_id=gate.id, name=gate.name, order=gate.order,
        planned_review_at=planned_review_at,
        threshold=threshold_override if threshold_override is not None else gate.threshold,
        decided_by_roles=list(gate.decided_by), decisions_allowed=list(gate.decisions),
        lead_time_days=gate.lead_time_days, actions_due_days=gate.actions_due_days,
        deliverables=[d.model_copy(deep=True) for d in gate.deliverables],
    )
