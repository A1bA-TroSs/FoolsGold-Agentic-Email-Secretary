"""Permission before any mail leaves this Mac for a cloud AI provider.

Why this exists
---------------
Ranking and briefing send message text -- sender, recipients, subject, and up
to ~3,600 characters of body -- to whichever provider Settings names. For a
local model (Ollama, or an OpenAI-compatible server on this machine) nothing
leaves the device. For a cloud provider it does, and three rules then apply:

* Apple guideline 5.1.2(i): "clearly disclose where personal data will be
  shared with third parties, including with third-party AI, and obtain
  explicit permission before doing so." Binding for the App Store; we meet it
  for the notarised DMG too, because it is simply what users are owed.
* Korea PIPA: an overseas transfer needs the user told the recipient, country,
  items, purpose and retention, and told they may refuse and what refusing
  costs -- then asked, separately from anything else.
* Hong Kong PDPO DPP1(3)/DPP3: purpose stated at collection; no new use
  without consent.

What "granted" means
--------------------
A grant is bound to three things, and changing any of them asks again:

1. the provider (anthropic / openai / copilot),
2. the *recipient* -- for OpenAI-compatible providers the host in
   `openai_base_url`, so pointing the app at a different server is a new
   recipient, not the same permission,
3. `DISCLOSURE_VERSION` -- bump it whenever what is sent, or to whom, changes
   in a way the dialog would have to say differently.

The gate is enforced in `llm.registry.get_provider()`, the single door every
AI call goes through, not in the UI. A UI check is a courtesy; a backend check
is the guarantee -- nothing that forgets to ask can send.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from . import db

# Bump when the dialog's content would change: what is sent, to whom, or why.
DISCLOSURE_VERSION = 1

# Proper nouns stay in English; the UI words everything else. Country is an
# ISO 3166 code so the frontend can name it in the reader's language
# (Intl.DisplayNames) instead of the backend deciding the language.
RECIPIENTS: dict[str, dict[str, str]] = {
    "anthropic": {"recipient": "Anthropic, PBC", "country": "US",
                  "policy_url": "https://www.anthropic.com/legal/privacy"},
    "openai": {"recipient": "OpenAI, L.L.C.", "country": "US",
               "policy_url": "https://openai.com/policies/privacy-policy/"},
    "copilot": {"recipient": "GitHub, Inc. (Microsoft)", "country": "US",
                "policy_url": "https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement"},
}

CLOUD_PROVIDERS = frozenset(RECIPIENTS)
_OPENAI_DEFAULT_HOST = "api.openai.com"


def _host(url: str) -> str:
    return (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()


def _is_loopback(host: str) -> bool:
    if host in ("localhost", "") or host.endswith(".localhost"):
        return host != ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def disclosure(provider: str) -> dict[str, Any] | None:
    """Who would receive mail for `provider`, or None when nothing leaves.

    An OpenAI-compatible base URL on this machine (LM Studio, llama.cpp,
    vLLM on localhost) is local: no recipient, no consent needed. Any other
    base URL is a recipient in its own right, named by its host -- we do not
    know who runs it, and the dialog says so rather than guessing."""
    provider = (provider or "").lower()
    if provider not in CLOUD_PROVIDERS:
        return None
    info = dict(RECIPIENTS[provider])
    if provider == "openai":
        base = (db.get_setting("openai_base_url", "") or "").strip()
        host = _host(base) if base else _OPENAI_DEFAULT_HOST
        if _is_loopback(host):
            return None
        if host != _OPENAI_DEFAULT_HOST:
            info = {"recipient": host, "country": "", "policy_url": ""}
    return {"provider": provider, "version": DISCLOSURE_VERSION, **info}


def requires_consent(provider: str) -> bool:
    return disclosure(provider) is not None


def _current_grant(provider: str, recipient: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM ai_consent WHERE provider = ? AND recipient = ? AND version = ?"
            " AND withdrawn_at IS NULL ORDER BY id DESC LIMIT 1",
            (provider, recipient, DISCLOSURE_VERSION),
        ).fetchone()
    return dict(row) if row else None


def is_granted(provider: str) -> bool:
    info = disclosure(provider)
    if info is None:
        return True          # nothing leaves the machine; nothing to ask
    return _current_grant(info["provider"], info["recipient"]) is not None


def status(provider: str) -> dict[str, Any]:
    info = disclosure(provider)
    if info is None:
        return {"provider": (provider or "").lower(), "required": False, "granted": True}
    grant = _current_grant(info["provider"], info["recipient"])
    return {**info, "required": True, "granted": grant is not None,
            "granted_at": grant["granted_at"] if grant else None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def grant(provider: str) -> dict[str, Any]:
    info = disclosure(provider)
    if info is None:
        return status(provider)
    if _current_grant(info["provider"], info["recipient"]) is None:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO ai_consent (provider, recipient, version, granted_at)"
                " VALUES (?, ?, ?, ?)",
                (info["provider"], info["recipient"], DISCLOSURE_VERSION, _now()),
            )
    return status(provider)


def withdraw(provider: str | None = None) -> int:
    """Withdraw a provider's permission, or every provider's when None.
    Returns how many live grants were closed. The rows stay, closed -- the
    record of what was once agreed is part of what the user is owed."""
    sql = "UPDATE ai_consent SET withdrawn_at = ? WHERE withdrawn_at IS NULL"
    args: list[Any] = [_now()]
    if provider:
        sql += " AND provider = ?"
        args.append(provider.lower())
    with db.connect() as conn:
        return conn.execute(sql, args).rowcount
