"""Where an entity lives in the web UI.

Ids are prefixed (gw/record/ids.py), so the entity type is read from the id when the caller
does not know it. Unknown or deleted entities resolve to None and render as plain text.
"""

from __future__ import annotations

from typing import Any

from gw.record import models as m
from gw.record.ids import PREFIX
from gw.record.store import Store

_BY_PREFIX = sorted(((p, t) for t, p in PREFIX.items()), key=lambda x: -len(x[0]))
_SHEET = {"Dependency": "/deps", "InboundItem": "/inbox", "Lesson": "/standards/lessons", "Connection": "/harness",
          "AgentRun": "/agents", "User": "/settings", "WorkPackage": "/tender", "PolicyRevision": "/policies",
          "GateModelRevision": "/standards/gatemodels", "ForecastRow": "/forecast", "Party": "/chasers"}
_APPROVAL = {"escalation_l3": "/chasers", "send_external": "/chasers", "dependency": "/deps",
             "review_queue_item": "/inbox", "policy_change": "/policies"}


def entity_type(entity_id: str) -> str | None:
    """The entity type an id belongs to, from its prefix (longest prefix wins: DEP- before D-)."""
    for prefix, kind in _BY_PREFIX:
        if entity_id.startswith(prefix):
            return kind
    return None


class Linker:
    """Resolves ids to sheet URLs with a few cached lookups; one per request."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._projects: dict[str, str] | None = None

    def projects(self) -> dict[str, str]:
        if self._projects is None:
            self._projects = {p.id: p.code for p in self.store.all(m.Project)}
        return self._projects

    def _get(self, model: type[Any], entity_id: str) -> Any:
        return self.store.find_one(model, id=entity_id)

    def url(self, entity_id: str | None, kind: str | None = None) -> str | None:
        if not entity_id:
            return None
        kind = kind or entity_type(entity_id)
        if kind == "Commitment":
            c = self._get(m.Commitment, entity_id)
            return f"/commitments/{c.code}" if c else None
        if kind in ("Evidence", "Chase"):
            row = self._get(m.Evidence if kind == "Evidence" else m.Chase, entity_id)
            return self.url(row.commitment_id, "Commitment") if row else None
        if kind == "Project":
            code = self.projects().get(entity_id)
            return f"/projects/{code}" if code else None
        if kind == "GateInstance":
            gi = self._get(m.GateInstance, entity_id)
            code = self.projects().get(gi.project_id) if gi else None
            return f"/projects/{code}/gates/{gi.gate_id}" if gi and code else None
        if kind == "Approval":
            ap = self._get(m.Approval, entity_id)
            if ap is None:
                return None
            if str(ap.kind) == "gate_pack" and ap.output_id:
                out = self._get(m.AgentOutput, ap.output_id)
                if out and out.payload.get("project") and out.payload.get("gate"):
                    return f"/projects/{out.payload['project']}/gates/{out.payload['gate']}/pack"
            return _APPROVAL.get(str(ap.kind), "/agents")
        if kind == "AgentOutput":
            out = self._get(m.AgentOutput, entity_id)
            if out and out.output_type == "Report":
                return f"/reports?period={out.payload.get('period', 'weekly')}"
            return "/agents"
        return _SHEET.get(kind or "")
