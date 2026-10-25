"""Resolve the principal for HTTP surfaces.for solo mode.

Login mode (any user has a password): the principal comes from a bearer token
(`Authorization: Bearer gwt_...`) or the signed `gw_session` cookie; nothing else is trusted.
Trust mode (no passwords yet, localhost only): the `gw_user` cookie or `X-GW-User` header,
defaulting to the first portfolio lead. Team mode (OIDC) replaces both.
"""

from __future__ import annotations

from contextvars import ContextVar

from fastapi import Request

from gw.errors import GwError
from gw.record import models as m
from gw.surfaces import auth
from gw.surfaces.service import Service

# Set by the security middleware for bearer-authenticated requests (MCP over HTTP reads it).
bearer_user: ContextVar[str | None] = ContextVar("gw_bearer_user", default=None)


class Unauthenticated(GwError):
    status = 401
    title = "Not signed in"


def service(request: Request) -> Service:
    svc: Service = request.app.state.svc
    return svc


def principal(request: Request) -> m.User:
    svc = service(request)
    if auth.login_required(svc.store):
        header = request.headers.get("authorization", "")
        uid = None
        if header.lower().startswith("bearer "):
            uid = auth.resolve_bearer(svc.store, header[7:].strip(), svc.registry)
        if uid is None:
            uid = auth.read_session(request.cookies.get(auth.SESSION_COOKIE), svc.store)
        if uid is None:
            raise Unauthenticated("sign in at /login or send a bearer token")
        return svc.store.get(m.User, uid)
    key = request.headers.get("x-gw-user") or request.cookies.get("gw_user")
    return svc.user(key or None)
