"""Art. 50 machine channel on HTTP.JSON responses that carry an `automatiqa-disclosure/1` envelope get `X-AI-Generated: true`;
HTML sheets that show model-written content (an element with `data-ai-scope`) get the same header
and the `ai-generated`, `ai-scope` and `ai-schema` meta tags in their head. Responses without
generated content pass untouched. MCP results carry the envelope in their payload instead.
"""

from __future__ import annotations

import re
from typing import Any

from gw.governance.disclosure import SCHEMA, html_meta

_JSON_MARK = re.compile(rb'"schema"\s*:\s*"' + re.escape(SCHEMA.encode()) + rb'"')
_HTML_SCOPE = re.compile(rb'data-ai-scope="([a-z0-9_,:-]+)"')
_LIMIT = 8 * 1024 * 1024  # never buffer more than this; larger bodies pass unmarked-by-header


class DisclosureMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        start: dict[str, Any] | None = None
        chunks: list[bytes] = []
        size = 0
        passthrough = False

        async def wrapped(message: dict[str, Any]) -> None:
            nonlocal start, size, passthrough
            if message["type"] == "http.response.start":
                ctype = dict(message.get("headers", [])).get(b"content-type", b"")
                if not (ctype.startswith(b"application/json") or ctype.startswith(b"text/html")):
                    passthrough = True
                    await send(message)
                else:
                    start = message
                return
            if passthrough or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            size += len(chunks[-1])
            if message.get("more_body") and size < _LIMIT:
                return
            if message.get("more_body"):  # too large to inspect: send as received, unmarked by header
                await send(start)
                await send({"type": "http.response.body", "body": b"".join(chunks), "more_body": True})
                passthrough = True
                return
            body = b"".join(chunks)
            headers = [(k, v) for k, v in start.get("headers", [])]
            ctype = dict(headers).get(b"content-type", b"")
            mark = False
            if ctype.startswith(b"application/json"):
                mark = bool(_JSON_MARK.search(body))
            else:
                scopes = sorted({s for m in _HTML_SCOPE.findall(body) for s in m.decode().split(",") if s})
                if scopes and b'name="ai-generated"' not in body:
                    body = body.replace(b"</head>", html_meta(scopes).encode() + b"</head>", 1)
                    mark = True
            if mark:
                headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
                headers += [(b"content-length", str(len(body)).encode()), (b"x-ai-generated", b"true")]
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body, "more_body": bool(message.get("more_body"))})
            passthrough = True  # anything after an oversized first slice streams through

        await self.app(scope, receive, wrapped)
