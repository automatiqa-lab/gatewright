"""Safe check expressions for deliverables.`simpleeval` with a locked namespace. Names are validated
against a whitelist at publish time so scoring never meets an unknown name.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from simpleeval import SimpleEval

ALLOWED_ROOTS: dict[str, set[str]] = {
    "project": {"estimate_class", "capex_amount", "contingency_amount", "contingency_drawn", "archetype", "country", "zone"},
    "dcma14": {"passed", "total"},
    "estimate": {"class_", "drift"},
    "evidence": {"kind", "accepted", "count"},
    "commitment": {"status", "escalation_level", "chase_count", "days_to_due"},
}
ALLOWED_FUNCS = {"days_until", "has_evidence"}
ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Compare, ast.Eq, ast.NotEq,
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Name, ast.Attribute, ast.Load, ast.Constant, ast.Call,
)


def validate_check(expr: str) -> list[str]:
    """Return a list of errors (empty = valid)."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return [f"syntax error: {e.msg}"]
    errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            errors.append(f"construct not allowed: {type(node).__name__}")
        if isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name):
                errors.append("nested attribute access is not allowed")
            elif node.value.id not in ALLOWED_ROOTS:
                errors.append(f"unknown name: {node.value.id}")
            elif node.attr not in ALLOWED_ROOTS[node.value.id]:
                errors.append(f"unknown name: {node.value.id}.{node.attr}")
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCS):
            errors.append("only days_until() and has_evidence() may be called")
        if isinstance(node, ast.Name) and node.id not in ALLOWED_ROOTS and node.id not in ALLOWED_FUNCS:
            errors.append(f"unknown name: {node.id}")
    return errors


@dataclass
class CheckContext:
    """Values a check may read. Missing data (e.g. no schedule) evaluates the check to unknown."""

    project: dict[str, Any] = field(default_factory=dict)
    dcma14: dict[str, Any] | None = None
    estimate: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    commitment: dict[str, Any] = field(default_factory=dict)
    evidence_kinds: set[str] = field(default_factory=set)
    today: date | None = None


def evaluate_check(expr: str, ctx: CheckContext) -> bool | None:
    """Evaluate a validated check. Returns None when a referenced source is absent."""
    if "dcma14." in expr and ctx.dcma14 is None:
        return None
    today = ctx.today or date.today()

    def days_until(d: Any) -> int:
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        if isinstance(d, datetime):
            d = d.date()
        return int((d - today).days)

    def has_evidence(kind: str) -> bool:
        return kind in ctx.evidence_kinds

    names = {
        "project": SimpleNamespace(**ctx.project),
        "dcma14": SimpleNamespace(**(ctx.dcma14 or {})),
        "estimate": SimpleNamespace(**ctx.estimate),
        "evidence": SimpleNamespace(**ctx.evidence),
        "commitment": SimpleNamespace(**ctx.commitment),
    }
    ev = SimpleEval(names=names, functions={"days_until": days_until, "has_evidence": has_evidence})
    try:
        return bool(ev.eval(expr))
    except Exception:
        return None
