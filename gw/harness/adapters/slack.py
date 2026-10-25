"""Slack MessageChannel.

Config: channel_id, token_ref (bot token, read by key name at call time). Pull reads
conversations.history; send posts chat.postMessage with the gw-ref in the text footer and in
message metadata; approval cards are Block Kit with Approve / Edit / Hold buttons whose values
carry the signed card token. The harness enforces the hard limit on send (approval required for
sponsors and authorities) before this adapter is reached.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from gw.harness.contracts import ApprovalCard, Health, InboundMessage, OutboundMessage, SyncResult
from gw.harness.registry import register, resolve_config, secret
from gw.record import models as m


@register("slack_api")
class SlackApi:
    def __init__(self, conn: m.Connection, transport: httpx.BaseTransport | None = None) -> None:
        cfg = resolve_config(conn.config)
        self.channel = str(cfg.get("channel_id", ""))
        self.token_ref = cfg.get("token_ref")
        self.client = httpx.Client(base_url="https://slack.com/api", timeout=30, transport=transport)

    def _call(self, method: str, http: str = "get", **payload: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {secret(self.token_ref) or ''}"}
        r = (self.client.get(f"/{method}", params=payload, headers=headers) if http == "get" else
             self.client.post(f"/{method}", json=payload, headers={**headers, "Content-Type": "application/json"}))
        r.raise_for_status()
        data = dict(r.json())
        if not data.get("ok"):
            raise RuntimeError(f"slack {method}: {data.get('error', 'error')}")
        return data

    def pull(self, since: str | None) -> list[InboundMessage]:
        params: dict[str, Any] = {"channel": self.channel, "limit": 200}
        if since:
            params["oldest"] = str(datetime.fromisoformat(since).timestamp())
        out: list[InboundMessage] = []
        while True:
            page = self._call("conversations.history", **params)
            for msg in page.get("messages", []):
                if msg.get("subtype"):
                    continue
                meta = (msg.get("metadata") or {}).get("event_payload") or {}
                out.append(InboundMessage(
                    id=msg["ts"], sender=str(msg.get("user", "")), text=msg.get("text", ""),
                    received_at=datetime.fromtimestamp(float(msg["ts"]), UTC), thread_id=msg.get("thread_ts"),
                    headers={"x-gw-ref": str(meta["gw_ref"])} if meta.get("gw_ref") else {}))
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return sorted(out, key=lambda x: x.received_at)
            params["cursor"] = cursor

    def send(self, msg: OutboundMessage) -> str:
        res = self._call("chat.postMessage", http="post", channel=self.channel,
                         text=f"*{msg.subject}*\n{msg.text}\n_gw-ref {msg.gw_ref}_",
                         metadata={"event_type": "gw_chase", "event_payload": {"gw_ref": msg.gw_ref}})
        return str(res.get("ts", msg.gw_ref))

    def post_card(self, card: ApprovalCard) -> str:
        buttons = [{"type": "button", "text": {"type": "plain_text", "text": a},
                    "action_id": f"gw_{a.lower()}", "style": "primary" if a == "Approve" else None,
                    "value": json.dumps({"approval": card.approval_id, "action": a.lower(), "token": card.token})}
                   for a in card.actions]
        for b in buttons:
            if b["style"] is None:
                del b["style"]
        res = self._call("chat.postMessage", http="post", channel=self.channel, text=card.title, blocks=[
            {"type": "header", "text": {"type": "plain_text", "text": card.title[:150]}},
            {"type": "section", "text": {"type": "mrkdwn", "text": card.body[:2900] or " "}},
            {"type": "actions", "block_id": f"gw_{card.approval_id}", "elements": buttons}])
        return str(res.get("ts", ""))

    def sync(self, since: str | None) -> SyncResult:
        msgs = self.pull(since)
        return SyncResult(pulled=len(msgs), cursor=max((x.received_at.isoformat() for x in msgs), default=since))

    def health(self) -> Health:
        try:
            self._call("auth.test")
            return Health(status="ok", detail="slack")
        except Exception as e:
            return Health(status="down", detail=str(e)[:200])
