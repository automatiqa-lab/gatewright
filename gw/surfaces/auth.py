"""Local authentication for the HTTP surfaces.

- Users with a password (argon2 hash) switch the server into login mode: every page and API call
  needs a signed session cookie or a bearer token; until then the server trusts localhost
  (`gw_user` cookie or `X-GW-User` header, see gw.surfaces.principal).
- Session cookies are HMAC-signed with a per-machine key kept outside the repository.
- CSRF: double-submit token (cookie `gw_csrf` + form field `csrf` or header `X-CSRF-Token`) on every
  state-changing request from a browser session.
- API bearer tokens are random, shown once, stored as SHA-256 hashes in the record's meta table.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from gw.record import models as m
from gw.record.store import Store

SESSION_COOKIE = "gw_session"
CSRF_COOKIE = "gw_csrf"
SESSION_HOURS = int(os.environ.get("GW_SESSION_HOURS", "12"))
_ph = PasswordHasher()


def key_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    return base / "gatewright" / "session.key"


def signing_key() -> bytes:
    """Per-machine signing key, created on first use, never stored in the repo or the database."""
    env = os.environ.get("GW_SESSION_KEY")
    if env:
        if len(env) < 32:
            raise RuntimeError("GW_SESSION_KEY must be at least 32 characters")
        return env.encode()
    p = key_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(secrets.token_bytes(32))
        os.chmod(p, 0o600)
    return p.read_bytes()


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(user: m.User, pw: str) -> bool:
    if not user.password_hash:
        return False
    try:
        return _ph.verify(user.password_hash, pw)
    except VerifyMismatchError:
        return False


def oidc_connections(store: Store) -> list[m.Connection]:
    return [c for c in store.find(m.Connection, contract="IdentitySource") if c.adapter == "oidc"]


def secure_cookies(request: Any) -> bool:
    """Cookies carry Secure behind TLS: the request came over https (proxy headers trusted) or the
    deployment says so with GW_PUBLIC_HTTPS=1."""
    return os.environ.get("GW_PUBLIC_HTTPS") == "1" or request.url.scheme == "https"


def login_required(store: Store) -> bool:
    """Login mode: any local password, or any OIDC identity source (team mode)."""
    return any(u.password_hash for u in store.all(m.User)) or bool(oidc_connections(store))


def provision_oidc_user(store: Store, conn_id: str, claims: dict[str, object], roles: list[str]) -> m.User:
    """Team mode: the identity provider is authoritative for name and roles."""
    from gw.record.ids import new_id

    email = str(claims.get("email") or claims.get("preferred_username") or "").lower()
    if not email:
        from gw.errors import Forbidden

        raise Forbidden(f"{conn_id}: the token carries no email", limit="login")
    user = store.find_one(m.User, email=email)
    name = str(claims.get("name") or email)
    if user is None:
        user = m.User(id=new_id("User"), email=email, name=name, short=email.split("@")[0], roles=roles)
        store.insert(user, actor=f"system:{conn_id}", kind="provisioned")
    elif sorted(user.roles) != sorted(roles) or user.name != name:
        user.roles, user.name = roles, name
        store.update(user, actor=f"system:{conn_id}", kind="roles_synced", payload={"roles": roles})
    return user


def resolve_bearer(store: Store, token: str, registry: object | None = None) -> str | None:
    """User id for a bearer token: a local API token, else an access token from an OIDC source."""
    uid = user_for_token(store, token)
    if uid or token.startswith("gwt_"):
        return uid
    conns = oidc_connections(store)
    if not conns:
        return None
    from gw.harness.registry import Registry

    reg = registry if isinstance(registry, Registry) else Registry(store)
    for conn in conns:
        idp = reg.get(conn.id).adapter
        try:
            claims = idp.verify(token)
        except Exception:
            continue
        principal = idp.authenticate(token)
        return provision_oidc_user(store, conn.id, claims, idp.groups(principal, claims)).id
    return None


def sign_blob(data: dict[str, object], minutes: int = 10) -> str:
    """Short-lived signed state for the OIDC redirect (state, nonce, PKCE verifier, next)."""
    body = json.dumps({**data, "exp": int(time.time()) + minutes * 60}, separators=(",", ":")).encode()
    return body.hex() + "." + hmac.new(signing_key(), body, hashlib.sha256).hexdigest()


def read_blob(token: str | None) -> dict[str, object] | None:
    if not token or "." not in token:
        return None
    body_hex, sig = token.rsplit(".", 1)
    try:
        body = bytes.fromhex(body_hex)
    except ValueError:
        return None
    if not hmac.compare_digest(hmac.new(signing_key(), body, hashlib.sha256).hexdigest(), sig):
        return None
    data = dict(json.loads(body))
    return data if int(data.get("exp", 0)) > time.time() else None


def _epoch(store: Store | None, user_id: str) -> int:
    return int(store.get_meta(f"session_epoch:{user_id}") or 0) if store is not None else 0


def revoke_sessions(store: Store, user_id: str) -> None:
    """End every session of a user (sign-out, password or role change)."""
    store.set_meta(f"session_epoch:{user_id}", str(_epoch(store, user_id) + 1))
    store.log("system:auth", "User", user_id, "sessions_revoked", {})


def make_session(user_id: str, hours: int = SESSION_HOURS, store: Store | None = None) -> str:
    body = json.dumps({"u": user_id, "exp": int(time.time()) + hours * 3600, "e": _epoch(store, user_id)},
                      separators=(",", ":")).encode()
    sig = hmac.new(signing_key(), body, hashlib.sha256).hexdigest()
    return body.hex() + "." + sig


def read_session(token: str | None, store: Store | None = None) -> str | None:
    """User id from a valid, unexpired, unrevoked session token, else None."""
    if not token or "." not in token:
        return None
    body_hex, sig = token.rsplit(".", 1)
    try:
        body = bytes.fromhex(body_hex)
    except ValueError:
        return None
    if not hmac.compare_digest(hmac.new(signing_key(), body, hashlib.sha256).hexdigest(), sig):
        return None
    data = json.loads(body)
    if data.get("exp", 0) <= time.time():
        return None
    if store is not None and int(data.get("e", 0)) != _epoch(store, str(data["u"])):
        return None
    return str(data["u"])


# --- login throttling: per client and per account, in memory ---------------------------

_FAILS: dict[str, list[float]] = {}
WINDOW_S = int(os.environ.get("GW_LOGIN_WINDOW_SECONDS", "900"))
MAX_FAILS = int(os.environ.get("GW_LOGIN_MAX_FAILS", "8"))
_DUMMY = PasswordHasher().hash(secrets.token_urlsafe(16))


def _recent(key: str) -> list[float]:
    now = time.time()
    _FAILS[key] = [t for t in _FAILS.get(key, []) if now - t < WINDOW_S]
    return _FAILS[key]


def _prune() -> None:
    if len(_FAILS) > 10_000:  # bounded memory under a credential-stuffing run
        for key in [k for k in _FAILS if not _recent(k)]:
            _FAILS.pop(key, None)


def login_allowed(client: str, account: str) -> bool:
    _prune()
    return all(len(_recent(k)) < MAX_FAILS for k in (f"c:{client}", f"a:{account.strip().lower()}"))


def login_attempt(client: str, account: str, ok: bool) -> None:
    for k in (f"c:{client}", f"a:{account.strip().lower()}"):
        if ok:
            _FAILS.pop(k, None)
        else:
            _recent(k).append(time.time())


def verify_dummy(pw: str) -> bool:
    """Spend the same argon2 time for an unknown account, so timing does not reveal which exist."""
    with contextlib.suppress(Exception):
        PasswordHasher().verify(_DUMMY, pw)
    return False


def new_csrf() -> str:
    return secrets.token_urlsafe(24)


def csrf_ok(cookie: str | None, submitted: str | None) -> bool:
    return bool(cookie) and bool(submitted) and hmac.compare_digest(str(cookie), str(submitted))


def create_api_token(store: Store, user: m.User) -> str:
    """Mint a bearer token for a user; only its hash is kept. Shown once."""
    token = "gwt_" + secrets.token_urlsafe(32)
    tokens = json.loads(store.get_meta("api_tokens") or "{}")
    tokens[hashlib.sha256(token.encode()).hexdigest()] = {"user": user.id, "created": int(time.time())}
    store.set_meta("api_tokens", json.dumps(tokens))
    store.log("system:auth", "User", user.id, "api_token_created", {})
    return token


def user_for_token(store: Store, token: str) -> str | None:
    tokens = json.loads(store.get_meta("api_tokens") or "{}")
    entry = tokens.get(hashlib.sha256(token.encode()).hexdigest())
    return str(entry["user"]) if entry else None


def api_tokens(store: Store) -> dict[str, dict[str, object]]:
    """Hashed API tokens: {sha256: {user, created}} (the clear token is never stored)."""
    return dict(json.loads(store.get_meta("api_tokens") or "{}"))


def revoke_api_tokens(store: Store, user_id: str) -> int:
    tokens = api_tokens(store)
    keep = {k: v for k, v in tokens.items() if v.get("user") != user_id}
    store.set_meta("api_tokens", json.dumps(keep))
    store.log("system:auth", "User", user_id, "api_tokens_revoked", {"n": len(tokens) - len(keep)})
    return len(tokens) - len(keep)
