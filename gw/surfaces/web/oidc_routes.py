"""Browser sign-in through an OIDC identity source (Entra, Google).GET /login/oidc/{conn}       -> redirect to the provider with state, nonce and a PKCE challenge
GET /login/oidc/callback     -> verify state, exchange the code, verify the ID token, provision the
                                user with roles from groups, set the session cookie.
"""

from __future__ import annotations

import secrets
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from gw.harness.adapters.oidc import pkce_pair
from gw.harness.contracts import Principal
from gw.surfaces import auth
from gw.surfaces.principal import service

router = APIRouter()
FLOW_COOKIE = "gw_oidc"


def _callback(request: Request) -> str:
    return str(request.url_for("oidc_callback"))


@router.get("/login/oidc/callback", name="oidc_callback")
def oidc_callback(request: Request, code: str = "", state: str = "", error: str = "") -> Response:
    svc = service(request)
    flow = auth.read_blob(request.cookies.get(FLOW_COOKIE))
    if error or not code or flow is None or flow.get("state") != state:
        svc.store.log("system:auth", "User", "unknown", "login_failed", {"via": "oidc"})
        return RedirectResponse(f"/login?err={quote('sign-in was not completed')}", status_code=303)
    conn = str(flow["conn"])
    idp = svc.registry.get(conn).adapter
    try:
        tokens = idp.exchange_code(code, _callback(request), str(flow["verifier"]))
        claims = idp.verify(tokens["id_token"], nonce=str(flow["nonce"]), audience=idp.client_id)
        principal = Principal(subject=str(claims["sub"]), email=str(claims.get("email") or ""),
                              name=str(claims.get("name") or ""))
        roles = idp.groups(principal, claims)
        user = auth.provision_oidc_user(svc.store, conn, claims, roles)
    except Exception as e:  # any provider or verification failure is a refused sign-in
        svc.store.log("system:auth", "User", "unknown", "login_failed", {"via": "oidc", "error": type(e).__name__})
        return RedirectResponse(f"/login?err={quote('sign-in refused by verification')}", status_code=303)
    svc.store.log(f"user:{user.id}", "User", user.id, "login", {"via": conn})
    resp = RedirectResponse(str(flow.get("next") or "/"), status_code=303)
    resp.delete_cookie(FLOW_COOKIE)
    secure = auth.secure_cookies(request)
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(user.id, store=svc.store), samesite="lax", httponly=True,
                    max_age=auth.SESSION_HOURS * 3600, secure=secure)
    resp.set_cookie(auth.CSRF_COOKIE, auth.new_csrf(), samesite="strict", httponly=False, secure=secure)
    return resp


@router.get("/login/oidc/{conn}")
def oidc_start(request: Request, conn: str, next: str = "/") -> Response:
    svc = service(request)
    if conn not in {c.id for c in auth.oidc_connections(svc.store)}:
        return RedirectResponse(f"/login?err={quote('unknown identity provider')}", status_code=303)
    idp = svc.registry.get(conn).adapter
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    resp = RedirectResponse(idp.authorize_url(_callback(request), state, nonce, challenge), status_code=303)
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    resp.set_cookie(FLOW_COOKIE, auth.sign_blob({"conn": conn, "state": state, "nonce": nonce, "verifier": verifier,
                                                 "next": target}), httponly=True, samesite="lax", max_age=600,
                    secure=auth.secure_cookies(request))
    return resp
