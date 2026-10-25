"""OIDC IdentitySource (Entra ID, Google).

Authorization-code flow with PKCE for the browser; ID and access tokens verified against the
issuer's JWKS (signature, issuer, audience, expiry, nonce). Groups or app roles from the token are
mapped to Gatewright roles with `config.group_map`. The client secret, when the provider requires
one, is read by key name at call time and never stored.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from gw.errors import Forbidden
from gw.harness.contracts import Principal
from gw.harness.registry import register, resolve_config, secret
from gw.record import models as m


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@register("oidc")
class OidcIdentity:
    """Config: {issuer, client_id, [secret_ref], [scopes], [audience], group_map: {group: role},
    [default_roles], [allowed_domains]}."""

    def __init__(self, conn: m.Connection, transport: httpx.BaseTransport | None = None) -> None:
        self.conn = conn
        self.cfg = resolve_config(conn.config)
        self.issuer = str(self.cfg.get("issuer", "")).rstrip("/")
        self.client_id = str(self.cfg.get("client_id", ""))
        self.http = httpx.Client(timeout=15, transport=transport)
        self._disco: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_at = 0.0

    # --- provider metadata ------------------------------------------------------------

    def discovery(self) -> dict[str, Any]:
        if self._disco is None:
            r = self.http.get(f"{self.issuer}/.well-known/openid-configuration")
            r.raise_for_status()
            self._disco = dict(r.json())
        return self._disco

    def jwks(self, force: bool = False) -> dict[str, Any]:
        if force or self._jwks is None or time.time() - self._jwks_at > 3600:
            r = self.http.get(self.discovery()["jwks_uri"])
            r.raise_for_status()
            self._jwks, self._jwks_at = dict(r.json()), time.time()
        return self._jwks

    # --- browser flow -------------------------------------------------------------------

    def authorize_url(self, redirect_uri: str, state: str, nonce: str, challenge: str) -> str:
        scopes = self.cfg.get("scopes") or ["openid", "email", "profile"]
        q = {"client_id": self.client_id, "response_type": "code", "redirect_uri": redirect_uri,
             "scope": " ".join(scopes), "state": state, "nonce": nonce, "code_challenge": challenge,
             "code_challenge_method": "S256"}
        return f"{self.discovery()['authorization_endpoint']}?{urlencode(q)}"

    def exchange_code(self, code: str, redirect_uri: str, verifier: str) -> dict[str, Any]:
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": self.client_id, "code_verifier": verifier}
        sec = secret(self.cfg.get("secret_ref"))
        if sec:
            data["client_secret"] = sec
        r = self.http.post(self.discovery()["token_endpoint"], data=data)
        if r.status_code >= 400:
            raise Forbidden(f"{self.conn.id}: code exchange refused ({r.status_code})", limit="login")
        return dict(r.json())

    # --- token verification ----------------------------------------------------------------

    def verify(self, token: str, nonce: str | None = None, audience: str | None = None) -> dict[str, Any]:
        """Verify a JWT from this issuer; refresh the JWKS once on an unknown key id."""
        import jwt

        header = jwt.get_unverified_header(token)
        for attempt in (0, 1):
            keys = {k["kid"]: k for k in self.jwks(force=attempt == 1).get("keys", [])}
            if header.get("kid") in keys:
                key = jwt.PyJWK(keys[header["kid"]]).key
                break
        else:
            raise Forbidden(f"{self.conn.id}: unknown signing key", limit="login")
        try:
            claims = dict(jwt.decode(token, key, algorithms=["RS256", "ES256"],
                                     audience=audience or self.cfg.get("audience") or self.client_id,
                                     issuer=self.discovery().get("issuer", self.issuer),
                                     options={"require": ["exp", "iss", "aud"]}))
        except jwt.PyJWTError as e:
            raise Forbidden(f"{self.conn.id}: token rejected ({type(e).__name__})", limit="login") from e
        if nonce is not None and claims.get("nonce") != nonce:
            raise Forbidden(f"{self.conn.id}: nonce mismatch", limit="login")
        domains = self.cfg.get("allowed_domains")
        if not domains:  # a multi-tenant issuer would otherwise let any account in
            raise Forbidden(f"{self.conn.id}: config.allowed_domains is required", limit="login")
        if claims.get("email_verified") is False:
            raise Forbidden(f"{self.conn.id}: e-mail not verified by the provider", limit="login")
        email = str(claims.get("email") or claims.get("preferred_username") or "")
        if email.split("@")[-1].lower() not in [d.lower() for d in domains]:
            raise Forbidden(f"{self.conn.id}: account domain not allowed", limit="login")
        return claims

    # --- contract ---------------------------------------------------------------------------

    def authenticate(self, token: str) -> Principal:
        c = self.verify(token)
        email = str(c.get("email") or c.get("preferred_username") or "")
        return Principal(subject=str(c["sub"]), email=email.lower(), name=str(c.get("name") or email))

    def groups(self, principal: Principal, claims: dict[str, Any] | None = None) -> list[str]:
        """Gatewright roles from token groups or app roles via `group_map`."""
        gmap: dict[str, str] = dict(self.cfg.get("group_map") or {})
        found = list((claims or {}).get("groups") or []) + list((claims or {}).get("roles") or [])
        roles = sorted({gmap[g] for g in found if g in gmap})
        return roles or list(self.cfg.get("default_roles") or ["viewer"])

    def health(self) -> Any:
        from gw.harness.contracts import Health

        try:
            self.discovery()
            return Health(status="ok", detail=self.issuer)
        except Exception as e:
            return Health(status="down", detail=f"{type(e).__name__}")
