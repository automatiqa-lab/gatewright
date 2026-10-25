"""Outbound webhooks and approval cards.

Subscriptions live in the record (meta `webhooks`: url, events, secret_ref). Bodies are signed
with HMAC-SHA256 over the raw JSON (`X-GW-Signature: sha256=<hex>`), the secret being read by key
name from the environment at send time (never stored). Delivery runs on a background thread so
a slow receiver never blocks a transaction; every attempt is an event.

Events: approval.requested, gate.at_risk, escalation.l4, brief.ready, connection.degraded.
Approval requests are also posted as cards to MessageChannels with scope `post_cards` when
`policies.channels.teams_cards_for_approvals` is on; card buttons carry a signed, single-use token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import threading
from typing import Any

import httpx

from gw.governance.disclosure import with_readable_line
from gw.harness.registry import secret
from gw.record import clock
from gw.record import models as m
from gw.record.store import Store, add_listener

EVENTS = ("approval.requested", "gate.at_risk", "escalation.l4", "brief.ready", "connection.degraded")
_q: queue.Queue[tuple[Store, dict[str, Any], dict[str, Any]]] = queue.Queue()
_worker: threading.Thread | None = None
_sync_delivery = False  # tests deliver inline


def subscriptions(store: Store) -> list[dict[str, Any]]:
    return list(json.loads(store.get_meta("webhooks") or "[]"))


def subscribe(store: Store, url: str, events: list[str], secret_ref: str) -> dict[str, Any]:
    bad = [e for e in events if e not in EVENTS]
    if bad:
        from gw.errors import Unprocessable

        raise Unprocessable(f"unknown events {bad}; known: {list(EVENTS)}", "/events")
    subs = [s for s in subscriptions(store) if s["url"] != url]
    sub = {"url": url, "events": events, "secret_ref": secret_ref}
    subs.append(sub)
    store.set_meta("webhooks", json.dumps(subs))
    return sub


def sign(body: bytes, key: str) -> str:
    return "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def card_token(approval_id: str, user_id: str) -> str:
    """Signed, single-use (checked against the approval's pending state) card token."""
    from gw.surfaces.auth import signing_key

    msg = f"{approval_id}:{user_id}".encode()
    return f"{user_id}.{hmac.new(signing_key(), msg, hashlib.sha256).hexdigest()}"


def verify_card_token(approval_id: str, token: str) -> str | None:
    user_id, _, _ = token.partition(".")
    return user_id if hmac.compare_digest(card_token(approval_id, user_id), token) else None


def _deliver(store: Store, sub: dict[str, Any], payload: dict[str, Any]) -> None:
    body = json.dumps(payload, default=str, separators=(",", ":")).encode()
    key = secret(sub.get("secret_ref")) or ""
    headers = {"Content-Type": "application/json", "X-GW-Event": payload["event"]}
    if key:
        headers["X-GW-Signature"] = sign(body, key)
    try:
        r = httpx.post(sub["url"], content=body, headers=headers, timeout=10)
        ok, status = r.status_code < 300, str(r.status_code)
    except httpx.HTTPError as e:
        ok, status = False, str(e)[:120]
    store.log("system:webhooks", "Webhook", sub["url"][:80], "delivered" if ok else "delivery_failed",
              {"event": payload["event"], "status": status, "signed": bool(key)})


def _run() -> None:
    while True:
        store, sub, payload = _q.get()
        try:
            _deliver(store, sub, payload)
        finally:
            _q.task_done()


def emit(store: Store, event: str, data: dict[str, Any]) -> int:
    """Queue `event` to every subscription that wants it; returns the number queued."""
    global _worker
    payload = {"event": event, "at": clock.now().isoformat(), "data": data}
    n = 0
    for sub in subscriptions(store):
        if event in sub["events"]:
            if _sync_delivery:
                _deliver(store, sub, payload)
            else:
                if _worker is None or not _worker.is_alive():
                    _worker = threading.Thread(target=_run, daemon=True)
                    _worker.start()
                _q.put((store, sub, payload))
            n += 1
    return n


def post_approval_card(store: Store, approval: m.Approval) -> None:
    from gw.harness.contracts import ApprovalCard
    from gw.harness.registry import Registry
    from gw.policies.store import current

    if not current(store).get("channels.teams_cards_for_approvals", False):
        return
    reg = Registry(store)
    for conn in reg.list("MessageChannel"):
        if "post_cards" not in conn.scopes:
            continue
        out = store.find_one(m.AgentOutput, id=approval.output_id) if approval.output_id else None
        body = (out.payload.get("text") or out.payload.get("says") or out.payload.get("reason") or "") if out else ""
        body = with_readable_line(str(body)[:1400], out.payload.get("ai_generated")) if out else ""
        approvers = [u for u in store.all(m.User) if {"project_manager", "portfolio_lead"} & set(u.roles)]
        token = card_token(approval.id, approvers[0].id) if approvers else ""
        try:
            reg.get(conn.id).post_card(ApprovalCard(approval_id=approval.id, title=approval.title or str(approval.kind),
                                                    body=str(body)[:1500], token=token))
        except Exception:
            continue


def on_events(store: Store, batch: list[dict[str, Any]]) -> None:
    """Store listener: translate committed record events into webhook events."""
    if not subscriptions(store) and not any(e["entity_type"] == "Approval" for e in batch):
        return
    for ev in batch:
        et, kind, eid = ev["entity_type"], ev["kind"], ev["entity_id"]
        try:
            if et == "Approval" and kind == "created":
                ap = store.get(m.Approval, eid)
                emit(store, "approval.requested", {"approval": ap.id, "kind": ap.kind, "title": ap.title})
                post_approval_card(store, ap)
            elif et == "Chase" and kind == "created":
                ch = store.get(m.Chase, eid)
                if ch.status == "war_room":
                    emit(store, "escalation.l4", {"commitment": ch.commitment_id, "gw_ref": ch.gw_ref})
            elif et == "AgentOutput" and kind == "created":
                out = store.get(m.AgentOutput, eid)
                if out.output_type == "Brief":
                    emit(store, "brief.ready", {"output": out.id, "user": out.payload.get("user")})
            elif et == "Connection" and kind == "health":
                if (ev.get("payload_json") or {}).get("health") in ("degraded", "down"):
                    emit(store, "connection.degraded", {"connection": eid, "health": ev["payload_json"]["health"]})
            elif et == "GateInstance" and kind == "readiness":
                gi = store.get(m.GateInstance, eid)
                days = (gi.planned_review_at - clock.now()).days
                from gw.policies.store import current

                if gi.readiness < gi.threshold and days <= int(current(store).get("signals.gate_horizon_days", 30)):
                    emit(store, "gate.at_risk", {"gate_instance": gi.id, "gate": gi.gate_id, "readiness": gi.readiness,
                                                 "threshold": gi.threshold, "review_in_days": days})
        except Exception:
            continue


add_listener(on_events)


def chase_card_token(chase_id: str) -> str:
    """Signed token for a chase card's CONFIRM / FLAG buttons, bound to the chase."""
    from gw.surfaces.auth import signing_key

    return hmac.new(signing_key(), f"chase:{chase_id}".encode(), hashlib.sha256).hexdigest()


def verify_chase_card_token(chase_id: str, token: str) -> bool:
    return hmac.compare_digest(chase_card_token(chase_id), token or "")
