"""Store: entity persistence plus the append-only event log.

: every mutation writes the entity and one
event in the same transaction; agent snapshots are event positions (`seq`).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel

from gw.errors import NotFound
from gw.record import clock
from gw.record import models as m
from gw.record.tables import ENTITY_TABLES, events, from_row, metadata, signals, to_row

# Change-listeners (signals recompute) subscribe here; record never imports them.
_listeners: list[Any] = []


def add_listener(fn: Any) -> None:
    """Register a callable `(store, event_dict)` run after each committed event batch."""
    _listeners.append(fn)


class Store:
    """Thin repository over SQLAlchemy Core."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = sa.create_engine(url)
        # The open transaction and its unflushed events belong to one thread: request threads, the
        # scheduler and the real-time worker share a Store, never a transaction.
        self._local = threading.local()
        self.listeners_enabled = True
        self.registry: Any = None  # set by the Service that owns this store

    @property
    def _conn(self) -> sa.Connection | None:
        conn: sa.Connection | None = getattr(self._local, "conn", None)
        return conn

    @_conn.setter
    def _conn(self, value: sa.Connection | None) -> None:
        self._local.conn = value

    @property
    def _pending_events(self) -> list[dict[str, Any]]:
        if not hasattr(self._local, "pending"):
            self._local.pending = []
        pending: list[dict[str, Any]] = self._local.pending
        return pending

    @_pending_events.setter
    def _pending_events(self, value: list[dict[str, Any]]) -> None:
        self._local.pending = value

    # --- lifecycle -------------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path = "gw.duckdb") -> Store:
        """Open (and create if needed) a DuckDB file, or any SQLAlchemy URL."""
        url = str(path)
        if "://" not in url:
            url = "duckdb:///" + (url if url == ":memory:" else Path(url).as_posix())
        store = cls(url)
        store.create_all()
        today = store.get_meta("today")
        if today:
            clock.freeze(datetime.fromisoformat(today))
        return store

    def create_all(self) -> None:
        metadata.create_all(self.engine)

    def drop_all(self) -> None:
        metadata.drop_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def tx(self) -> Iterator[sa.Connection]:
        """One transaction; nested calls join the outer one."""
        if self._conn is not None:
            yield self._conn
            return
        try:
            with self.engine.begin() as conn:
                self._conn = conn
                try:
                    yield conn
                finally:
                    self._conn = None
        except BaseException:
            self._pending_events = []
            raise
        batch, self._pending_events = self._pending_events, []
        if batch and self.listeners_enabled:
            for fn in _listeners:
                fn(self, batch)

    # --- events ----------------------------------------------------------------------

    def _event(self, conn: sa.Connection, actor: str, entity: BaseModel | None, kind: str,
               payload: dict[str, Any] | None, agent_run_id: str | None,
               entity_type: str | None = None, entity_id: str | None = None) -> None:
        ev = {
            "at": clock.now().replace(tzinfo=None),
            "actor": actor,
            "entity_type": entity_type or type(entity).__name__,
            "entity_id": entity_id or str(getattr(entity, "id", "")),
            "kind": kind,
            "payload_json": payload or {},
            "agent_run_id": agent_run_id,
        }
        conn.execute(events.insert(), ev)
        self._pending_events.append(ev)

    def log(self, actor: str, entity_type: str, entity_id: str, kind: str,
            payload: dict[str, Any] | None = None, agent_run_id: str | None = None) -> None:
        """Write a bare event (no entity row changes)."""
        with self.tx() as conn:
            self._event(conn, actor, None, kind, payload, agent_run_id, entity_type, entity_id)

    def last_seq(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(sa.select(sa.func.max(events.c.seq))).scalar() or 0)

    def events_after(self, seq: int, limit: int = 1000) -> list[dict[str, Any]]:
        """Committed events with a sequence number above `seq`, oldest first (real-time cursor)."""
        q = sa.select(events).where(events.c.seq > seq).order_by(events.c.seq).limit(limit)
        with self._read() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]

    def count_events_after(self, seq: int, skip_actor_prefixes: tuple[str, ...] = ()) -> tuple[int, int]:
        """(changes, finished agent runs) since `seq`, counted in SQL (the /live badge)."""
        cond = [events.c.seq > seq] + [sa.not_(events.c.actor.like(f"{p}%")) for p in skip_actor_prefixes]
        runs = sa.and_(events.c.entity_type == "AgentRun", events.c.kind == "finished")
        q = sa.select(sa.func.count(), sa.func.sum(sa.case((runs, 1), else_=0))).where(*cond)
        with self._read() as conn:
            row = conn.execute(q).one()
        return int(row[0] or 0), int(row[1] or 0)

    def events(self, entity_id: str | None = None, entity_type: str | None = None,
               actor: str | None = None, since: datetime | None = None,
               agent_run_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        q = sa.select(events)
        if entity_id:
            q = q.where(events.c.entity_id == entity_id)
        if entity_type:
            q = q.where(events.c.entity_type == entity_type)
        if actor:
            q = q.where(events.c.actor == actor)
        if agent_run_id:
            q = q.where(events.c.agent_run_id == agent_run_id)
        if since:
            q = q.where(events.c.at >= since.replace(tzinfo=None))
        q = q.order_by(events.c.seq.desc()).limit(limit)
        with self._read() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]

    def count_events(self) -> int:
        with self._read() as conn:
            return int(conn.execute(sa.select(sa.func.count()).select_from(events)).scalar() or 0)

    # --- entities --------------------------------------------------------------------

    @contextmanager
    def _read(self) -> Iterator[sa.Connection]:
        if self._conn is not None:
            yield self._conn
        else:
            with self.engine.connect() as conn:
                yield conn

    def insert(self, entity: BaseModel, actor: str = "system", kind: str = "created",
               payload: dict[str, Any] | None = None, agent_run_id: str | None = None) -> None:
        table = ENTITY_TABLES[type(entity)]
        with self.tx() as conn:
            conn.execute(table.insert(), to_row(entity))
            if not isinstance(entity, m.Meta):
                self._event(conn, actor, entity, kind, payload, agent_run_id)

    def insert_many(self, entities: Sequence[BaseModel], actor: str = "system",
                    kind: str = "created", agent_run_id: str | None = None) -> None:
        """Bulk insert of one entity type with one event each (demo generator, sync)."""
        if not entities:
            return
        table = ENTITY_TABLES[type(entities[0])]
        with self.tx() as conn:
            self._bulk(conn, table, [to_row(e) for e in entities])
            evs: list[dict[str, Any]] = [{
                "at": clock.now().replace(tzinfo=None), "actor": actor,
                "entity_type": type(e).__name__, "entity_id": str(getattr(e, "id", "")),
                "kind": kind, "payload_json": {}, "agent_run_id": agent_run_id,
            } for e in entities]
            self._bulk(conn, events, evs)
            self._pending_events.extend(evs)

    def _bulk(self, conn: sa.Connection, table: sa.Table, rows: list[dict[str, Any]]) -> None:
        """Bulk insert; on DuckDB large batches go through a DataFrame in the same transaction."""
        if not rows:
            return
        if conn.dialect.name != "duckdb" or len(rows) < 200:
            conn.execute(table.insert(), rows)
            return
        import json

        import pandas as pd

        cols = [c.name for c in table.columns if c.name in rows[0]]
        json_cols = {c.name for c in table.columns if isinstance(c.type, sa.JSON)}
        data = {c: [json.dumps(r.get(c)) if c in json_cols and r.get(c) is not None else r.get(c) for r in rows]
                for c in cols}
        df = pd.DataFrame(data, columns=cols)
        raw = conn.connection.dbapi_connection
        assert raw is not None
        raw.register("_gw_bulk", df)
        try:
            col_sql = ", ".join(f'"{c}"' for c in cols)
            sel = ", ".join(f'CAST("{c}" AS JSON)' if c in json_cols else f'"{c}"' for c in cols)
            raw.execute(f'INSERT INTO "{table.name}" ({col_sql}) SELECT {sel} FROM _gw_bulk')
        finally:
            raw.unregister("_gw_bulk")

    def update(self, entity: BaseModel, actor: str = "system", kind: str = "updated",
               payload: dict[str, Any] | None = None, agent_run_id: str | None = None) -> None:
        table = ENTITY_TABLES[type(entity)]
        pk = "key" if isinstance(entity, m.Meta) else "id"
        row = to_row(entity)
        with self.tx() as conn:
            res = conn.execute(table.update().where(table.c[pk] == row[pk]).values(**row))
            if res.rowcount == 0:
                raise NotFound(f"{type(entity).__name__} {row[pk]} not found")
            if not isinstance(entity, m.Meta):
                self._event(conn, actor, entity, kind, payload, agent_run_id)

    def update_many(self, entities: Sequence[BaseModel], actor: str = "system",
                    kind: str = "updated") -> None:
        for e in entities:
            self.update(e, actor=actor, kind=kind)

    def get[T: BaseModel](self, model: type[T], id: str) -> T:
        found = self.find_one(model, id=id)
        if found is None:
            raise NotFound(f"{model.__name__} {id} not found")
        return found

    def find_one[T: BaseModel](self, model: type[T], **where: Any) -> T | None:
        rows = self.find(model, limit=1, **where)
        return rows[0] if rows else None

    def find[T: BaseModel](self, model: type[T], order_by: str | None = None,
                           limit: int | None = None, **where: Any) -> list[T]:
        """Equality filters; a list or tuple value means IN."""
        table = ENTITY_TABLES[model]
        q = sa.select(table)
        for col, val in where.items():
            if isinstance(val, (list, tuple, set)):
                q = q.where(table.c[col].in_(list(val)))
            elif val is None:
                q = q.where(table.c[col].is_(None))
            else:
                q = q.where(table.c[col] == (val.value if hasattr(val, "value") else val))
        if order_by:
            desc = order_by.startswith("-")
            ocol = table.c[order_by.lstrip("-")]
            q = q.order_by(ocol.desc() if desc else ocol)
        if limit:
            q = q.limit(limit)
        with self._read() as conn:
            return [from_row(model, r._mapping) for r in conn.execute(q)]

    def all[T: BaseModel](self, model: type[T]) -> list[T]:
        return self.find(model)

    def count(self, model: type[BaseModel], **where: Any) -> int:
        table = ENTITY_TABLES[model]
        q = sa.select(sa.func.count()).select_from(table)
        for col, val in where.items():
            q = q.where(table.c[col] == val)
        with self._read() as conn:
            return int(conn.execute(q).scalar() or 0)

    # --- signals ---------------------------------------------------------------------

    def write_signals(self, rows: Iterable[m.SignalRow]) -> None:
        """Only `gw.signals` calls this."""
        data = [to_row(r) for r in rows]
        if data:
            with self.tx() as conn:
                conn.execute(signals.insert(), data)

    def latest_signals(self, signal_id: str | None = None, scope: str | None = None,
                       scope_id: str | None = None) -> list[m.SignalRow]:
        """Latest row per (signal, scope, scope_id)."""
        q = sa.select(signals)
        if signal_id:
            q = q.where(signals.c.signal_id == signal_id)
        if scope:
            q = q.where(signals.c.scope == scope)
        if scope_id:
            q = q.where(signals.c.scope_id == scope_id)
        q = q.order_by(signals.c.computed_at)
        latest: dict[tuple[str, str, str], m.SignalRow] = {}
        with self._read() as conn:
            for r in conn.execute(q):
                row = from_row(m.SignalRow, r._mapping)
                latest[(row.signal_id, row.scope, row.scope_id)] = row
        return list(latest.values())

    def signal_history(self, signal_id: str, scope: str, scope_id: str,
                       since: datetime | None = None) -> list[m.SignalRow]:
        q = sa.select(signals).where(signals.c.signal_id == signal_id,
                                     signals.c.scope == scope, signals.c.scope_id == scope_id)
        if since:
            q = q.where(signals.c.computed_at >= since.replace(tzinfo=None))
        with self._read() as conn:
            return [from_row(m.SignalRow, r._mapping) for r in conn.execute(q.order_by(signals.c.computed_at))]

    def clear_signals_at(self, computed_at: datetime) -> None:
        with self.tx() as conn:
            conn.execute(signals.delete().where(signals.c.computed_at == computed_at.replace(tzinfo=None)))

    # --- meta ------------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.find_one(m.Meta, key=key)
        return row.value if row else None

    def set_meta(self, key: str, value: str) -> None:
        if self.find_one(m.Meta, key=key):
            self.update(m.Meta(key=key, value=value))
        else:
            self.insert(m.Meta(key=key, value=value))

    def execute(self, stmt: Any) -> Any:
        with self._read() as conn:
            return conn.execute(stmt)
