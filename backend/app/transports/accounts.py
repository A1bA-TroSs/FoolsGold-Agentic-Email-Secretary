"""What this app remembers about each address the user sends from.

Its own table, created on first use, so that adding it touches no shared
schema. One row per address: the servers discovery found (so it runs once,
not on every send), the password encrypted at rest, and how the last send
actually went -- delivered, or handed to Drafts -- so the next one can say
what will happen before the user presses the button.
"""
from __future__ import annotations

import json
from typing import Any

from .. import db
from ..crypto import decrypt, encrypt
from .autoconfig import ServerConfig

_READY: set[str] = set()


def _conn():
    conn = db.connect()
    key = str(db.DB_PATH)
    if key not in _READY:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS send_accounts ("
            " address TEXT PRIMARY KEY,"
            " config TEXT,"                  # ServerConfig as JSON
            " secret BLOB,"                  # Fernet, never plaintext
            " last_mode TEXT,"               # delivered | drafts
            " updated_at TEXT)")
        conn.commit()
        _READY.add(key)
    return conn


def _key(address: str) -> str:
    return (address or "").strip().lower()


def config(address: str) -> ServerConfig | None:
    with _conn() as conn:
        row = conn.execute("SELECT config FROM send_accounts WHERE address = ?",
                           (_key(address),)).fetchone()
    if not row or not row["config"]:
        return None
    try:
        return ServerConfig(**json.loads(row["config"]))
    except (TypeError, ValueError):
        return None


def remember_config(address: str, found: ServerConfig) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO send_accounts (address, config, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(address) DO UPDATE SET config = excluded.config, "
            "updated_at = excluded.updated_at",
            (_key(address), json.dumps(found.as_dict()), db.now_iso()))
        conn.commit()


def password(address: str) -> str:
    with _conn() as conn:
        row = conn.execute("SELECT secret FROM send_accounts WHERE address = ?",
                           (_key(address),)).fetchone()
    return decrypt(row["secret"]) if row and row["secret"] else ""


def remember_password(address: str, secret: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO send_accounts (address, secret, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(address) DO UPDATE SET secret = excluded.secret, "
            "updated_at = excluded.updated_at",
            (_key(address), encrypt(secret), db.now_iso()))
        conn.commit()


def forget_password(address: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE send_accounts SET secret = NULL WHERE address = ?", (_key(address),))
        conn.commit()


def remember_mode(address: str, mode: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE send_accounts SET last_mode = ? WHERE address = ?",
                     (mode, _key(address)))
        conn.commit()


def summary() -> list[dict[str, Any]]:
    """For Settings: which addresses can send, never the secret itself."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT address, config, secret IS NOT NULL AS has_secret, last_mode "
            "FROM send_accounts ORDER BY address").fetchall()
    out = []
    for row in rows:
        cfg = json.loads(row["config"]) if row["config"] else {}
        out.append({"address": row["address"], "ready": bool(row["has_secret"]),
                    "last_mode": row["last_mode"], "found_by": cfg.get("source"),
                    "oauth_only": bool(cfg.get("oauth_only"))})
    return out
