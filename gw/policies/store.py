"""Policies: schema, loader, revisions, defaults.

Agents read `current()` at run time; nothing caches policy values across runs.
Hard limits are not here - they are enforced in `gw.harness` and `gw.governance`.
"""

from __future__ import annotations

import difflib
import functools
import json
from functools import cache
from typing import Any

import jsonschema
import yaml

from gw import paths
from gw.errors import Unprocessable
from gw.record import clock
from gw.record import models as m
from gw.record.ids import new_id
from gw.record.store import Store

# Default exposure weights; used when policies.exposure.weights is empty.
DEFAULT_EXPOSURE_WEIGHTS: dict[str, dict[str, float]] = {
    "schedule": {"float_erosion": 0.4, "schedule_staleness": 0.2, "dcma_pass": 0.2, "forecast_slip": 0.2},
    "cost": {"estimate_drift": 0.5, "contingency_burn": 0.5},
    "permits": {"authority_silence": 0.5, "permit_unconfirmed": 0.5},
    "design": {"design_gap": 0.6, "evidence_rejections_30d": 0.4},
    "vendors": {"vendor_response": 0.4, "vendor_flag_rate": 0.3, "vendor_forecast_slip": 0.3},
    "site": {"site_float_erosion": 0.5, "safety_incidents": 0.5},
    "systems": {"systems_gap": 0.6, "integrator_forecast_slip": 0.4},
    "ops_readiness": {"ops_readiness_gap": 1.0},
    "safety": {"hse_status": 0.5, "reviewer_silence": 0.5},
    "dependencies": {"deps_at_risk": 0.6, "deps_unreviewed": 0.4},
    "forecast": {"forecast_slip": 1.0},
    # dimensions of non-capital models (integration-100-day)
    "people": {"people_gap": 1.0},
    "customers": {"customers_gap": 1.0},
    "compliance": {"compliance_gap": 0.6, "authority_silence": 0.4},
}


@cache
def _schema() -> dict[str, Any]:
    return dict(json.loads((paths.SCHEMAS / "policies.schema.json").read_text(encoding="utf-8")))


def validate_text(text: str) -> dict[str, Any]:
    """Validate policy YAML; raise Unprocessable with a JSON pointer on the first error."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise Unprocessable(f"YAML error: {e}", pointer="") from e
    if not isinstance(data, dict):
        raise Unprocessable("policies must be a mapping", pointer="")
    validator = jsonschema.Draft202012Validator(_schema())
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        pointer = "/" + "/".join(str(p) for p in first.absolute_path)
        detail = first.message
        if list(first.absolute_path)[-1:] == ["l3_requires_approval"]:
            detail = ("hard limit: L3 escalations to parties of kind sponsor or authority always need "
                      "approval; l3_requires_approval cannot be false")
        raise Unprocessable(detail, pointer=pointer)
    return dict(data)


def default_text() -> str:
    return paths.POLICIES_DEFAULT.read_text(encoding="utf-8")


def install_default(store: Store, author: str = "system") -> None:
    if not store.find(m.PolicyRevision, limit=1):
        set_policies(store, default_text(), author, note="default policies")


def set_policies(store: Store, text: str, author: str, note: str = "") -> m.PolicyRevision:
    """Validate and store a new revision with its diff; writes one event ( intro)."""
    validate_text(text)
    prev = store.find(m.PolicyRevision, order_by="-rev", limit=1)
    rev = prev[0].rev + 1 if prev else 1
    diff = ""
    if prev:
        diff = "".join(difflib.unified_diff(prev[0].yaml_text.splitlines(True), text.splitlines(True),
                                            f"rev {prev[0].rev}", f"rev {rev}"))
    pr = m.PolicyRevision(id=new_id("PR"), rev=rev, yaml_text=text, author=author, note=note,
                          diff=diff, created_at=clock.now())
    store.insert(pr, actor=author if ":" in author else f"user:{author}", kind="policy_revision",
                 payload={"rev": rev, "note": note})
    return pr


def current_revision(store: Store) -> m.PolicyRevision:
    rows = store.find(m.PolicyRevision, order_by="-rev", limit=1)
    if not rows:
        install_default(store)
        rows = store.find(m.PolicyRevision, order_by="-rev", limit=1)
    return rows[0]


@functools.lru_cache(maxsize=1)
def shipped_defaults() -> dict[str, Any]:
    """The default policy file shipped with Gatewright: the single source of default values."""
    from gw import paths

    try:
        data = yaml.safe_load(paths.POLICIES_DEFAULT.read_text(encoding="utf-8"))
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


class Policies:
    """Read-only view over one policy revision with typed helpers."""

    def __init__(self, data: dict[str, Any], rev: int) -> None:
        self.data = data
        self.rev = rev

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.data.get(name) or {})

    def get(self, dotted: str, default: Any = None) -> Any:
        """A setting from this revision, else the shipped default policy file, else `default`."""
        for source in (self.data, shipped_defaults()):
            cur: Any = source
            for part in dotted.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    break
                cur = cur[part]
            else:
                return cur
        return default

    def thresholds(self, signal_id: str) -> tuple[float, float] | None:
        t = self.get(f"signals.thresholds.{signal_id}")
        return (float(t[0]), float(t[1])) if t else None

    def exposure_weights(self) -> dict[str, dict[str, float]]:
        w = self.get("exposure.weights") or {}
        merged = {k: dict(v) for k, v in DEFAULT_EXPOSURE_WEIGHTS.items()}
        for dim, ws in w.items():
            merged[dim] = dict(ws)
        return merged

    def autonomy(self, agent: str) -> str:
        return str(self.get(f"agents.autonomy.{agent}", "observe"))


def current(store: Store) -> Policies:
    """Current policies, read fresh on every call ( intro)."""
    rev = current_revision(store)
    return Policies(dict(yaml.safe_load(rev.yaml_text)), rev.rev)


def from_default() -> Policies:
    return Policies(dict(yaml.safe_load(default_text())), 0)
