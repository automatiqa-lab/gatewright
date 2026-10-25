"""ASGI middleware: login redirect and CSRF for browser sessions.

Pure ASGI (not BaseHTTPMiddleware) so the request body can be read for the CSRF field and then
replayed to the route unchanged.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, quote

from gw.surfaces import auth

EXEMPT = ("/login", "/logout", "/static", "/healthz",
          "/api/v1/cards/")  # card callbacks carry their own signed token
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
MAX_FORM_BYTES = 2 * 1024 * 1024  # policies and gate models are the largest forms
# Trust mode serves the person at this machine only. Starlette's in-process TestClient reports
# "testclient"/"testserver", names no socket peer can present.
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}
FORWARDED = (b"x-forwarded-for", b"forwarded", b"x-real-ip", b"x-forwarded-host")
CSP = ("default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self' https://login.microsoftonline.com; "
       "base-uri 'none'; object-src 'none'")
HEADERS = [(b"content-security-policy", CSP.encode()), (b"x-content-type-options", b"nosniff"),
           (b"x-frame-options", b"DENY"), (b"referrer-policy", b"same-origin"),
           (b"permissions-policy", b"camera=(), microphone=(), geolocation=()")]


def _local_request(scope: dict[str, Any]) -> bool:
    """A request from this machine, not relayed by a proxy, addressed to localhost (C1)."""
    client = (scope.get("client") or ("", 0))[0]
    if client not in LOOPBACK:
        return False
    names = {k for k, _ in scope.get("headers", [])}
    if any(h in names for h in FORWARDED):
        return False
    host = _header(scope, b"host").lower()
    host = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    return host in LOCAL_HOSTS


def _same_origin(scope: dict[str, Any]) -> bool:
    """Browser state changes must come from this site (cross-site POSTs to localhost)."""
    if _header(scope, b"sec-fetch-site") == "cross-site":
        return False
    origin = _header(scope, b"origin")
    if not origin or origin == "null":
        return origin != "null"
    from urllib.parse import urlparse

    return urlparse(origin).netloc.lower() == _header(scope, b"host").lower()


def _cookies(scope: dict[str, Any]) -> dict[str, str]:
    raw = next((v.decode() for k, v in scope.get("headers", []) if k == b"cookie"), "")
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


def _header(scope: dict[str, Any], name: bytes) -> str:
    return next((v.decode() for k, v in scope.get("headers", []) if k == name), "")


class SecurityMiddleware:
    def __init__(self, app: Any, store_getter: Any, registry_getter: Any = None) -> None:
        self.app = app
        self.store_getter = store_getter
        self.registry_getter = registry_getter

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message.get("headers", [])}
                message = {**message, "headers": list(message.get("headers", []))
                           + [(k, v) for k, v in HEADERS if k not in have]}
            await send(message)

        await self._handle(scope, receive, with_headers)

    async def _handle(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not auth.login_required(self.store_getter()):
            if not _local_request(scope):
                await _respond(send, 403, b'{"title":"Forbidden","status":403,"detail":"trust mode serves localhost '
                                          b'only; set a password (gw user passwd) for login mode","limit":"trust_mode"}',
                               "application/problem+json")
                return
            if scope["method"] in UNSAFE and not _same_origin(scope):
                await _respond(send, 403, b'{"title":"Forbidden","status":403,"detail":"cross-site request refused",'
                                          b'"limit":"csrf"}', "application/problem+json")
                return
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        if path.startswith(EXEMPT):
            await self.app(scope, receive, send)
            return
        cookies = _cookies(scope)
        bearer = _header(scope, b"authorization").lower().startswith("bearer ")
        session = auth.read_session(cookies.get(auth.SESSION_COOKIE), self.store_getter())
        if not bearer and session is None:
            if path.startswith("/api/"):
                await _respond(send, 401, b'{"title":"Not signed in","status":401,"detail":"sign in or send a bearer token"}',
                               "application/problem+json")
            else:
                await _redirect(send, f"/login?next={quote(path)}")
            return
        if bearer:
            from gw.surfaces.principal import bearer_user

            uid = auth.resolve_bearer(self.store_getter(), _header(scope, b"authorization")[7:].strip(),
                                      self.registry_getter() if self.registry_getter else None)
            if uid is None:
                await _respond(send, 401, b'{"title":"Not signed in","status":401,"detail":"unknown bearer token"}',
                               "application/problem+json")
                return
            reset = bearer_user.set(uid)
            try:
                await self.app(scope, receive, send)
            finally:
                bearer_user.reset(reset)
            return
        if scope["method"] not in UNSAFE:
            await self.app(scope, receive, send)
            return
        # browser session + state change: double-submit CSRF token
        body = b""
        more = True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
            if len(body) > MAX_FORM_BYTES:
                await _respond(send, 413, b'{"title":"Payload too large","status":413}', "application/problem+json")
                return
        submitted = _header(scope, b"x-csrf-token")
        if not submitted and "application/x-www-form-urlencoded" in _header(scope, b"content-type"):
            submitted = (parse_qs(body.decode("utf-8", "replace")).get("csrf") or [""])[0]
        if not auth.csrf_ok(cookies.get(auth.CSRF_COOKIE), submitted):
            await _respond(send, 403, b'{"title":"Forbidden","status":403,"detail":"CSRF token missing or wrong",'
                                      b'"limit":"csrf"}', "application/problem+json")
            return
        sent = False

        async def replay() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            nxt: dict[str, Any] = await receive()
            return nxt

        await self.app(scope, replay, send)


async def _respond(send: Any, status: int, body: bytes, ctype: str) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def _redirect(send: Any, location: str) -> None:
    await send({"type": "http.response.start", "status": 303, "headers": [(b"location", location.encode())]})
    await send({"type": "http.response.body", "body": b""})
