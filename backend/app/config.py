"""Central configuration. Everything local-first: nothing here points off-machine
except Microsoft Graph and whichever LLM provider the user configures."""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Fools Gold"
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
    # UI chrome only. Mail subjects, senders and bodies are never translated.
    "ui_language": "en",
    # Where mail comes from. "applemail" needs no account registration at all,
    # so it is the default; "graph" talks to Outlook directly.
    "mail_source": "applemail",
    "applemail_root": "",
    "applemail_inbox_only": "true",
    # Your own address. Graph fills this in at sign-in; with Apple Mail you type
    # it, and it drives the strongest structural signal there is (To: you vs Cc:).
    "user_address": "",
    # No provider is preselected. The app must never reach for a credential the
    # user did not choose, and until they choose one it ranks mail on
    # structural signals -- which works, and says so.
    "llm_provider": "none",
    "copilot_model": "auto",
    "anthropic_model": "claude-sonnet-4-5",
    "openai_model": "gpt-4o-mini",
    # Anything speaking the OpenAI API: Azure, OpenRouter, a local server.
    "openai_base_url": "",
    "entra_client_id": "",
    "sync_days": "30",
    "sync_max_messages": "300",
    "classify_batch_size": "10",
    # --- adaptive ranking -----------------------------------------------
    # The fyi/action boundary, as two numbers that can actually be moved. A
    # category has no knob; these do.
    "theta_relevance": "0.35",
    "theta_action": "0.55",
    # How many things the user wants on today's action list. Nobody knows what
    # 0.63 means; everybody knows this number, so it is the control we expose
    # and theta_action is solved from it.
    "target_action_volume": "8",
    # Learning is OFF until this is set, and it is set by hand. The app spent
    # weeks being clicked through for QA; a default of "learn from whatever is
    # already in the tables" would have baked that in permanently on first run.
    "learning_epoch_start": "",
    # A fraction of suppressed mail is surfaced anyway, labelled as a guess. A
    # perfectly accurate ranker maximises feedback-loop degeneracy, and `noise`
    # is otherwise a one-way door. 0 disables it.
    "explore_one_in": "20",
    "auto_digest": "true",
    # Daily checklist reminders. Times are local wall-clock, HH:MM, and the
    # Electron shell -- not this process -- is what actually posts them.
    "notify_enabled": "true",
    "notify_morning": "09:00",
    "notify_evening": "21:00",
}
