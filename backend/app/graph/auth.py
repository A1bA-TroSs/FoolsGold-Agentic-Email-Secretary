"""Microsoft Entra (Azure AD) OAuth for Outlook, via MSAL.

Authorization Code + PKCE against a *public* client -- there is no client
secret, because a desktop app cannot keep one. The whole MSAL token cache
(access token, refresh token, account record) is encrypted with the local
Fernet key before it touches disk.
"""
from __future__ import annotations

import secrets
from typing import Any

import msal

from .. import db
from ..config import GRAPH_AUTHORITY, GRAPH_SCOPES, REDIRECT_URI
from ..crypto import decrypt, encrypt


class AuthError(RuntimeError):
    pass


class NotConfigured(AuthError):
    """Raised before the user has pasted their Entra client ID into Settings."""


# In-memory only: one pending login at a time, cleared once redeemed.
_pending: dict[str, dict[str, Any]] = {}


# --------------------------------------------------------------------------
# token cache <-> encrypted sqlite blob
# --------------------------------------------------------------------------

def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    with db.connect() as conn:
        row = conn.execute("SELECT cache_blob FROM oauth_tokens WHERE id = 1").fetchone()
    if row and row["cache_blob"]:
        blob = decrypt(row["cache_blob"])
        if blob:
            cache.deserialize(blob)
    return cache


def _save_cache(cache: msal.SerializableTokenCache, account: str | None = None) -> None:
    if not cache.has_state_changed and account is None:
        return
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO oauth_tokens (id, account, cache_blob, updated_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  account = COALESCE(excluded.account, oauth_tokens.account), "
            "  cache_blob = excluded.cache_blob, updated_at = excluded.updated_at",
            (account, encrypt(cache.serialize()), db.now_iso()),
        )
        conn.commit()


def client_id() -> str:
    cid = db.get_setting("entra_client_id", "").strip()
    if not cid:
        raise NotConfigured(
            "No Microsoft Entra client ID configured. Open Settings and paste the "
            "Application (client) ID from your app registration -- see docs/ENTRA_SETUP.md."
        )
    return cid


def _app(cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    return msal.PublicClientApplication(
        client_id(), authority=GRAPH_AUTHORITY, token_cache=cache
    )


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------

def build_auth_url(login_hint: str | None = None) -> str:
    """Step 1: the user types their Outlook address, we hand back the Microsoft
    consent URL. The shell opens it in the *system* browser, never in-app, so
    the user can see the real login.microsoftonline.com address bar."""
    cache = _load_cache()
    app = _app(cache)
    state = secrets.token_urlsafe(24)
    flow = app.initiate_auth_code_flow(
        GRAPH_SCOPES,
        redirect_uri=REDIRECT_URI,
        state=state,
        login_hint=(login_hint or None),
        prompt="select_account" if not login_hint else None,
    )
    _pending.clear()
    _pending[state] = flow
    return flow["auth_uri"]


def redeem_code(query: dict[str, Any]) -> str:
    """Step 2: Microsoft redirects back to our loopback listener. Returns the
    signed-in account's address."""
    state = query.get("state")
    flow = _pending.pop(state, None) if state else None
    if flow is None:
        raise AuthError("No matching login in progress -- start the sign-in again.")

    cache = _load_cache()
    app = _app(cache)
    result = app.acquire_token_by_auth_code_flow(flow, query, scopes=GRAPH_SCOPES)
    if "access_token" not in result:
        raise AuthError(result.get("error_description") or result.get("error") or "Token exchange failed.")

    claims = result.get("id_token_claims") or {}
    account = claims.get("preferred_username") or claims.get("email") or ""
    _save_cache(cache, account=account)
    return account


def get_access_token() -> str:
    """Silent refresh on every call. MSAL returns the cached access token until
    it is close to expiry, then uses the refresh token automatically."""
    cache = _load_cache()
    app = _app(cache)
    accounts = app.get_accounts()
    if not accounts:
        raise AuthError("Not signed in.")
    result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])
    _save_cache(cache)
    if not result or "access_token" not in result:
        # Refresh itself failed (revoked, password change, MFA policy). Only now
        # do we push the user back to the login screen.
        raise AuthError("Session expired -- please sign in to Outlook again.")
    return result["access_token"]


def status() -> dict[str, Any]:
    try:
        cid = client_id()
    except NotConfigured:
        return {"configured": False, "signed_in": False, "account": None}
    cache = _load_cache()
    app = msal.PublicClientApplication(cid, authority=GRAPH_AUTHORITY, token_cache=cache)
    accounts = app.get_accounts()
    with db.connect() as conn:
        row = conn.execute("SELECT account FROM oauth_tokens WHERE id = 1").fetchone()
    stored = row["account"] if row else None
    return {
        "configured": True,
        "signed_in": bool(accounts),
        "account": (accounts[0].get("username") if accounts else None) or stored,
    }


def sign_out() -> None:
    _pending.clear()
    with db.connect() as conn:
        conn.execute("DELETE FROM oauth_tokens")
        conn.commit()
