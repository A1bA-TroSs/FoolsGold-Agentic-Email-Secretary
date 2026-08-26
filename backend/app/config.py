"""Central configuration. Everything local-first: nothing here points off-machine
except Microsoft Graph and whichever LLM provider the user configures."""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Fool's Gold"
VERSION = "1.0.0-dev"

# All user state lives in one directory the user can inspect or delete.
DATA_DIR = Path(os.environ.get("FOOLSGOLD_HOME", Path.home() / ".foolsgold"))
DB_PATH = DATA_DIR / "foolsgold.db"
KEY_PATH = DATA_DIR / "secret.key"

BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = int(os.environ.get("FOOLSGOLD_PORT", "8765"))
REDIRECT_PATH = "/api/auth/callback"
# Entra treats http://localhost as a valid loopback redirect for public clients.
REDIRECT_URI = f"http://localhost:{BACKEND_PORT}{REDIRECT_PATH}"

# Microsoft Graph
GRAPH_AUTHORITY = "https://login.microsoftonline.com/common"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# MSAL injects openid/profile/offline_access itself -- passing them explicitly errors.
GRAPH_SCOPES = ["User.Read", "Mail.Read"]

# Settings keys that are encrypted at rest.
SECRET_SETTINGS = {"anthropic_api_key", "openai_api_key"}

DEFAULT_SETTINGS = {
    "theme": "gold",
    # Where mail comes from. "applemail" needs no account registration at all,
    # so it is the default; "graph" talks to Outlook directly.
    "mail_source": "applemail",
    "applemail_root": "",
    "applemail_inbox_only": "true",
    # Your own address. Graph fills this in at sign-in; with Apple Mail you type
    # it, and it drives the strongest structural signal there is (To: you vs Cc:).
    "user_address": "",
    "llm_provider": "copilot",
    "copilot_model": "auto",
    "anthropic_model": "claude-sonnet-4-5",
    "openai_model": "gpt-4o-mini",
    "entra_client_id": "",
    "sync_days": "30",
    "sync_max_messages": "300",
    "classify_batch_size": "10",
    "auto_digest": "true",
}
