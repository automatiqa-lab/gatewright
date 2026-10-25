"""Connection registry: instantiate adapters, guard them, track health, schedule sync.

and the harness side of the hard limits.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from gw.errors import Forbidden, NotFound
from gw.harness.contracts import CONTRACTS, Health, SyncResult
from gw.record import clock
from gw.record import models as m
from gw.record.store import Store

log = logging.getLogger("gw.harness")

AdapterFactory = Callable[[m.Connection], Any]
ADAPTERS: dict[str, AdapterFactory] = {}

# Methods that need a scope beyond the connection's base read scope.
METHOD_SCOPES = {
    "send": "send_as",
    "deliver_to_self": "seed_demo",
    "upload": "seed_demo",  # demo seeding only; never granted on a production connection
    "post_card": "post_cards",
    "add_comment": "comment",
    "create_event": "create_events",
    "complete": "infer",
    "embed": "infer",
    "tabular_fit_predict": "infer",
    "authenticate": "login",
    "groups": "login",
}
UNGUARDED = {"health", "is_external", "kind", "set_activity_dates", "name"}


def register(name: str) -> Callable[[AdapterFactory], AdapterFactory]:
    """Decorator: register an adapter class under its config name."""

    def deco(cls: AdapterFactory) -> AdapterFactory:
        ADAPTERS[name] = cls
        return cls

    return deco


def resolve_config(config: dict[str, Any]) -> dict[str, Any]:
    """Replace `$ENV` values; `*_ref` keys stay names (secrets are read by the adapter at call time)."""
    out: dict[str, Any] = {}
    for k, v in config.items():
        if isinstance(v, str) and re.fullmatch(r"\$[A-Z0-9_]+", v):
            out[k] = os.environ.get(v[1:], "")
        else:
            out[k] = v
    return out


def secret(ref: str | None) -> str | None:
    """Read a secret by key name from the environment (`GW_SECRET_<NAME>`). Never logged or stored."""
    if not ref:
        return None
    return os.environ.get("GW_SECRET_" + re.sub(r"[^A-Z0-9]", "_", ref.upper()))


class Guarded:
    """Proxy that enforces scopes and the schedule-write hard limit on any adapter."""

    def __init__(self, conn: m.Connection, adapter: Any, store: Store | None = None) -> None:
        self._conn = conn
        self._adapter = adapter
        self._store = store

    @property
    def adapter(self) -> Any:
        return self._adapter

    def set_activity_dates(self, *args: Any, **kwargs: Any) -> None:
        raise Forbidden(f"{self._conn.id}: Gatewright never writes to a schedule tool",
                        limit="no_schedule_writes")

    def send(self, msg: Any) -> str:
        """Hard limit 2: no message to a sponsor or authority without an approved Approval of THIS message:
        the approval's output must name the same party and reference, carry the same text as approved, and
        list every recipient. Model-written text leaves with the readable Art. 50 line (COMPLIANCE.md)."""
        approved_text = msg.text
        if getattr(msg, "ai_generated", None):
            from gw.governance.disclosure import with_readable_line

            msg = msg.model_copy(update={"text": with_readable_line(msg.text, msg.ai_generated)})
        if "send_as" not in self._conn.scopes:
            raise Forbidden(f"{self._conn.id}: send needs scope 'send_as'", limit="scope")
        if self._store is None:
            raise Forbidden(f"{self._conn.id}: send needs the record to check approvals",
                            limit="approval_required")
        if not msg.party_id:
            # internal delivery (e.g. the morning brief): every recipient must be a registered user
            users = {u.email.lower() for u in self._store.all(m.User)}
            outsiders = [a for a in list(msg.to) + list(msg.cc) if a.lower() not in users]
            if outsiders or not msg.to:
                raise Forbidden(f"{msg.gw_ref}: internal message to a non-user address", limit="internal_only")
            return str(self._adapter.send(msg))
        party = self._store.get(m.Party, msg.party_id)
        if party.kind in (m.PartyKind.sponsor, m.PartyKind.authority):
            approval = self._store.find_one(m.Approval, id=msg.approval_id) if msg.approval_id else None
            if approval is None or approval.decision not in (m.ApprovalDecision.approved,
                                                               m.ApprovalDecision.approved_edited):
                raise Forbidden(f"{msg.gw_ref}: message to {party.kind} {party.name} needs an approved approval",
                                limit="approval_required")
            out = self._store.find_one(m.AgentOutput, id=approval.output_id) if approval.output_id else None
            pl = out.payload if out is not None else {}
            mismatch = [what for what, ok in (
                ("output", out is not None),
                ("party", pl.get("party_id") == msg.party_id),
                ("reference", pl.get("gw_ref") == msg.gw_ref),
                ("text", pl.get("text") == approved_text),
                ("recipients", set(msg.to) <= set(pl.get("to") or []) and set(msg.cc) <= set(pl.get("cc") or [])),
            ) if not ok]
            if mismatch:
                raise Forbidden(f"{msg.gw_ref}: approval {approval.id} does not cover this message "
                                f"({', '.join(mismatch)} differ)", limit="approval_required")
        return str(self._adapter.send(msg))

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._adapter, name)
        if name in UNGUARDED or not callable(attr):
            return attr
        needed = METHOD_SCOPES.get(name, "read")
        if needed not in self._conn.scopes and not (needed == "read" and "link" in self._conn.scopes and name in {"hash", "link", "stat"}):
            def refused(*a: Any, **k: Any) -> Any:
                raise Forbidden(f"{self._conn.id}: {name} needs scope {needed!r}", limit="scope")

            return refused
        return attr


class Registry:
    """Holds connections and adapter instances for one store."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._instances: dict[str, Guarded] = {}
        self._overrides: dict[str, Any] = {}

    # --- configuration ------------------------------------------------------------

    def add(self, spec: dict[str, Any], actor: str = "system") -> m.Connection:
        """Add a connection from a connections.yaml entry."""
        if spec["contract"] not in CONTRACTS:
            raise NotFound(f"unknown contract {spec['contract']}")
        conn = m.Connection(
            id=spec["id"], contract=spec["contract"], adapter=spec["adapter"], name=spec.get("name", spec["id"]),
            config=spec.get("config") or {}, config_ref=(spec.get("config") or {}).get("secret_ref"),
            scopes=spec.get("scopes") or ["read"], owner=str(spec.get("owner", "")),
            sync_every_minutes=int(spec.get("sync_every_minutes", 15)), mapping=spec.get("mapping") or {},
        )
        if self.store.find_one(m.Connection, id=conn.id):
            self.store.update(conn, actor=actor, kind="reconfigured")
        else:
            self.store.insert(conn, actor=actor)
        self._instances.pop(conn.id, None)
        return conn

    def add_file(self, path: str | Path, actor: str = "system") -> list[m.Connection]:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return [self.add(spec, actor) for spec in data.get("connections", [])]

    def override(self, conn_id: str, adapter: Any) -> None:
        """Inject an adapter instance (tests and the demo use fakes)."""
        self._overrides[conn_id] = adapter
        self._instances.pop(conn_id, None)

    def list(self, contract: str | None = None) -> list[m.Connection]:
        return self.store.find(m.Connection, contract=contract) if contract else self.store.all(m.Connection)

    def connection(self, conn_id: str) -> m.Connection:
        return self.store.get(m.Connection, conn_id)

    def get(self, conn_id: str) -> Guarded:
        """Guarded adapter for a connection."""
        if conn_id in self._instances:
            return self._instances[conn_id]
        conn = self.connection(conn_id)
        if conn_id in self._overrides:
            adapter = self._overrides[conn_id]
        else:
            factory = ADAPTERS.get(conn.adapter)
            if factory is None:
                from gw.harness import adapters  # noqa: F401  (registers adapters)

                factory = ADAPTERS.get(conn.adapter)
            if factory is None:
                raise NotFound(f"{conn_id}: no adapter named {conn.adapter}")
            adapter = factory(conn)
        guarded = Guarded(conn, adapter, self.store)
        self._instances[conn_id] = guarded
        return guarded

    def satisfies(self, conn_id: str) -> bool:
        conn = self.connection(conn_id)
        return isinstance(self.get(conn_id).adapter, CONTRACTS[str(conn.contract)])

    # --- health ---------------------------------------------------------------------

    def record_result(self, conn: m.Connection, ok: bool, error: str | None = None) -> m.Connection:
        """`degraded` after one failure, `down` after three; ok resets."""
        if ok:
            conn.consecutive_failures = 0
            conn.health = "ok"
            conn.last_error = None
        else:
            conn.consecutive_failures += 1
            conn.health = "down" if conn.consecutive_failures >= 3 else "degraded"
            conn.last_error = (error or "error")[:500]
        self.store.update(conn, actor="system", kind="health", payload={"health": conn.health})
        return conn

    def test(self, conn_id: str) -> Health:
        conn = self.connection(conn_id)
        try:
            h: Health = self.get(conn_id).health()
            ok = h.status == "ok"
            self.record_result(conn, ok, None if ok else h.detail)
            return h
        except Exception as e:  # adapter failure is a health fact, not a crash
            conn = self.record_result(conn, False, str(e))
            return Health(status="down" if conn.health == "down" else "degraded", detail=str(e))

    # --- sync -----------------------------------------------------------------------

    def sync(self, conn_id: str) -> SyncResult:
        """Pull from one connection and ingest normalised facts into the record."""
        from gw.harness.ingest import ingest

        conn = self.connection(conn_id)
        try:
            result = ingest(self, conn)
            conn = self.connection(conn_id)
            conn.last_sync_at = clock.now()
            conn.cursor = result.cursor or conn.cursor
            self.store.update(conn, actor="system", kind="synced",
                              payload={"pulled": result.pulled, "changed": result.changed})
            self.record_result(conn, not result.errors, "; ".join(result.errors) or None)
            return result
        except Forbidden:
            raise
        except Exception as e:
            log.warning("sync failed", extra={"conn_id": conn_id, "error": str(e)})
            self.record_result(conn, False, str(e))
            return SyncResult(errors=[str(e)])

    def sync_all(self) -> dict[str, SyncResult]:
        out: dict[str, SyncResult] = {}
        for conn in self.list():
            if conn.contract in ("ScheduleSource", "PlanSource", "DocumentSource", "MessageChannel", "CostSource",
                                 "CalendarSource"):
                out[conn.id] = self.sync(conn.id)
        return out
