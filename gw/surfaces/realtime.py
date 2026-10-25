"""Real-time reactions: record events start bounded agent runs.A worker reads committed events from the event log by sequence number (so it sees every process's
writes and never runs inside a committing transaction). Matching events (policies.realtime
.reactions: entity, kinds, ignored actors) become reaction keys such as `gatekeeper:DC-01`; a key
waits `debounce_seconds` so a burst of replies produces one run, and each action is capped per hour
(a skipped reaction is logged). Due reactions run through the same governed paths as the scheduler
(run_agent, process_inbound, dispatch_outbox) with trigger `event:<rule>`, under the lock the
scheduler holds. An action never reacts to its own actor.

One reactor per deployment: on Postgres the worker holds an advisory lock, so only one process
reacts; DuckDB is single-process. The reactor keeps no durable state: it starts from the current end
of the log, and a restart loses at most a debounce window, which the next scheduled run covers.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from gw.record import models as m

log = logging.getLogger("gw.realtime")
ACTIONS = ("gatekeeper", "process_inbound", "dispatch_outbox", "signals")
OWN_ACTOR = {"gatekeeper": "agent:gatekeeper", "process_inbound": "agent:semantic", "dispatch_outbox": "agent:chaser"}


@dataclass
class Pending:
    action: str
    scope: str | None
    rule: str
    due: float


class Reactor:
    """One per running service. `start()` runs a worker thread; tests call `drain(force=True)`."""

    def __init__(self, svc: Any, lock: threading.Lock | None = None) -> None:
        self.svc = svc
        self.lock = lock or threading.Lock()
        self._pending: dict[str, Pending] = {}
        self._history: dict[str, deque[float]] = defaultdict(deque)
        self._mu = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ran: deque[dict[str, Any]] = deque(maxlen=200)
        self.cursor = svc.store.last_seq()
        self._leader_conn: Any = None

    # --- configuration ---------------------------------------------------------------------------

    def _config(self) -> dict[str, Any]:
        cfg = dict(self.svc.policies().get("realtime", {}) or {})
        return cfg if cfg.get("enabled") else {}

    # --- intake (runs inside the committing thread: cheap, never raises) ---------------------------

    def poll(self) -> int:
        """Read new committed events and queue matching reactions. Returns how many events were read."""
        batch = self.svc.store.events_after(self.cursor, limit=2000)
        if batch:
            self.cursor = int(batch[-1]["seq"])
            self.on_events(self.svc.store, batch)
        return len(batch)

    def on_events(self, store: Any, batch: list[dict[str, Any]]) -> None:
        if store is not self.svc.store:
            return
        try:
            cfg = self._config()
            if not cfg:
                return
            delay = float(cfg.get("debounce_seconds", 20))
            for i, rule in enumerate(cfg.get("reactions") or []):
                when = rule.get("when") or {}
                kinds = set(when.get("kind") or [])
                ignored = set(rule.get("ignore_actors") or []) | {OWN_ACTOR.get(rule["run"], "")}
                for ev in batch:
                    if ev["entity_type"] != when.get("entity") or (kinds and ev["kind"] not in kinds):
                        continue
                    if str(ev.get("actor", "")) in ignored or str(ev.get("actor", "")).startswith("system:realtime"):
                        continue
                    scope = self._scope(rule["run"], ev) if rule["run"] == "gatekeeper" else None
                    key = f"{rule['run']}:{scope or '*'}"
                    with self._mu:
                        if key not in self._pending:
                            self._pending[key] = Pending(rule["run"], scope, f"{when.get('entity')}#{i}",
                                                         time.monotonic() + delay)
        except Exception:  # intake must never break a commit
            log.exception("realtime intake failed")

    def _scope(self, action: str, ev: dict[str, Any]) -> str | None:
        """The project a gatekeeper reaction is about (None = every project)."""
        st = self.svc.store
        et, eid = ev["entity_type"], ev["entity_id"]
        commitment = None
        if et == "Commitment":
            commitment = st.find_one(m.Commitment, id=eid)
        elif et == "Evidence":
            evd = st.find_one(m.Evidence, id=eid)
            commitment = st.find_one(m.Commitment, id=evd.commitment_id) if evd else None
        if commitment is None:
            return None
        project = st.find_one(m.Project, id=commitment.project_id)
        return project.code if project else None

    # --- execution -----------------------------------------------------------------------------------

    def drain(self, force: bool = False) -> list[dict[str, Any]]:
        """Run every due reaction (all pending ones when `force`). Returns what ran or was skipped."""
        cfg = self._config()
        cap = int(cfg.get("max_runs_per_action_per_hour", 30)) if cfg else 0
        now = time.monotonic()
        with self._mu:
            due = [k for k, p in self._pending.items() if force or p.due <= now]
            items = [(k, self._pending.pop(k)) for k in due]
        done: list[dict[str, Any]] = []
        for _key, p in items:
            hist = self._history[p.action]
            while hist and now - hist[0] > 3600:
                hist.popleft()
            if cap and len(hist) >= cap:
                done.append({"action": p.action, "scope": p.scope, "rule": p.rule, "outcome": "rate_limited"})
                self.svc.store.log("system:realtime", "Reactor", p.action, "rate_limited",
                                   {"scope": p.scope, "rule": p.rule, "cap_per_hour": cap})
                continue
            hist.append(now)
            done.append(self._run(p))
        self.ran.extend(done)
        return done

    def _run(self, p: Pending) -> dict[str, Any]:
        trigger = f"event:{p.rule}"
        out: dict[str, Any] = {"action": p.action, "scope": p.scope, "rule": p.rule}
        try:
            with self.lock:
                if p.action == "gatekeeper":
                    res = self.svc.run_gatekeeper(p.scope, trigger=trigger)
                    out["outcome"] = str(res.run.outcome)
                elif p.action == "process_inbound":
                    out["outcome"] = "ok"
                    out["result"] = self.svc.process_inbound(trigger)
                elif p.action == "dispatch_outbox":
                    out["outcome"] = "ok"
                    out["result"] = {"sent": self.svc.dispatch_outbox()}
                elif p.action == "signals":
                    out["outcome"] = "ok"
                    out["result"] = {"signals": self.svc.recompute_signals()}
            self.svc.store.log("system:realtime", "Reactor", p.action, "reacted",
                               {"scope": p.scope, "rule": p.rule, "outcome": out.get("outcome")})
        except Exception as e:
            log.exception("realtime %s failed", p.action)
            out["outcome"] = f"failed: {type(e).__name__}"
        return out

    def _is_leader(self) -> bool:
        """Postgres: hold an advisory lock so exactly one process reacts. Other databases: always."""
        engine = self.svc.store.engine
        if engine.dialect.name != "postgresql":
            return True
        if self._leader_conn is None:
            import sqlalchemy as sa

            conn = engine.connect()
            if conn.execute(sa.text("select pg_try_advisory_lock(778241)")).scalar():
                self._leader_conn = conn  # held for the life of the process
            else:
                conn.close()
        return self._leader_conn is not None

    def start(self, tick: float = 2.0) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(tick):
                try:
                    if self._is_leader():
                        self.poll()
                        self.drain()
                except Exception:  # the worker survives a bad tick; the next one retries
                    log.exception("realtime tick failed")

        self._thread = threading.Thread(target=loop, name="gw-realtime", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self._leader_conn is not None:
            self._leader_conn.close()
            self._leader_conn = None


def attach(svc: Any, lock: threading.Lock | None = None) -> Reactor:
    """Create the service's reactor; `start()` begins reading the event log from its current end."""
    reactor = Reactor(svc, lock)
    svc.reactor = reactor
    return reactor
