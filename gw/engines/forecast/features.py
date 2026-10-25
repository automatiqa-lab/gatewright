"""Forecast features.

One function builds a feature row from a state dict, so history (as-of rows written
by the demo generator) and live scoring use the same definition.
"""

from __future__ import annotations

from typing import Any

CATEGORICAL = ["deliverable_type", "gate_id", "gate_model_id", "project_archetype", "country", "party_kind",
               "vendor_lead_time_class"]
NUMERIC = ["days_to_due", "deliverable_mandatory", "weight", "party_median_reply_days", "party_silence_rate",
           "party_flag_rate", "party_sample_n", "chases_sent", "replies", "silence_days", "escalation_level",
           "owner_open_items", "owner_overdue_items", "open_dependencies_on_this", "changes_since_last_gate",
           "estimate_class", "month_of_year", "evidence_submitted", "days_since_last_schedule_sync",
           "float_on_path", "is_permit", "is_insurer", "prior_slips_same_party_same_type",
           "prior_slip_rate_same_party_same_type", "flagged", "synthetic"]
FEATURES = NUMERIC + CATEGORICAL

def _policy_map(key: str) -> dict[str, str]:
    from gw.policies.store import shipped_defaults

    return dict((shipped_defaults().get("forecast") or {}).get(key) or {})


LEAD_TIME_CLASS = _policy_map("lead_time_class")


def deliverable_type(deliverable_id: str, kind: str) -> str:
    """Coarse type of a deliverable from its id (stable across gate models)."""
    d = deliverable_id.lower()
    for key, typ in _policy_map("deliverable_types").items():
        if key in d:
            return typ
    return kind


def build(state: dict[str, Any]) -> dict[str, Any]:
    """Feature row from a state dict; missing values become neutral defaults."""
    row: dict[str, Any] = {}
    for f in NUMERIC:
        v = state.get(f)
        row[f] = float(v) if v is not None else (-1.0 if f == "float_on_path" else 0.0)
    for f in CATEGORICAL:
        row[f] = str(state.get(f) or "none")
    return row
