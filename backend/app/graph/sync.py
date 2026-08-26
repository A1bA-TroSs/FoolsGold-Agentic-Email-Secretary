"""Fetch mail from Microsoft Graph into the local cache.

Read-only by design: v1.0 requests Mail.Read and never issues a write. If this
module ever needs a POST/PATCH, that is a 3.0 conversation, not a bugfix.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

import httpx

from .. import db
from ..config import GRAPH_BASE
from . import auth

SELECT_FIELDS = ",".join([
    "id", "conversationId", "subject", "from", "toRecipients", "ccRecipients",
    "receivedDateTime", "isRead", "hasAttachments", "importance", "webLink",
    "bodyPreview", "body", "parentFolderId",
])

_MAX_BODY_CHARS = 20000  # cap what we store; nobody needs a 2MB newsletter in SQLite


class _TextExtractor(HTMLParser):
    """Good-enough HTML -> text for feeding the model and for keyword matching.
    Deliberately not a full renderer: the detail pane shows the real HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in ("p", "br", "div", "tr", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r"\n\s*\n\s*\n+", "\n\n", joined)
        return joined.strip()


def html_to_text(html: str) -> str:
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return parser.text()


def _addresses(recipients: list[dict] | None) -> list[dict[str, str]]:
    out = []
    for r in recipients or []:
        addr = (r or {}).get("emailAddress") or {}
        out.append({"name": addr.get("name") or "", "address": (addr.get("address") or "").lower()})
    return out


def _to_row(msg: dict[str, Any]) -> dict[str, Any]:
    sender = (msg.get("from") or {}).get("emailAddress") or {}
    body = msg.get("body") or {}
    content = (body.get("content") or "")[:_MAX_BODY_CHARS]
    is_html = (body.get("contentType") or "").lower() == "html"
    return {
        "id": msg["id"],
        "conversation_id": msg.get("conversationId"),
        "subject": msg.get("subject") or "(no subject)",
        "from_name": sender.get("name") or "",
        "from_address": (sender.get("address") or "").lower(),
        "to_recipients": json.dumps(_addresses(msg.get("toRecipients"))),
        "cc_recipients": json.dumps(_addresses(msg.get("ccRecipients"))),
        "received_at": msg.get("receivedDateTime") or "",
        "is_read": 1 if msg.get("isRead") else 0,
        "has_attachments": 1 if msg.get("hasAttachments") else 0,
        "importance": msg.get("importance") or "normal",
        "web_link": msg.get("webLink") or "",
        "folder": msg.get("parentFolderId") or "",
        "body_preview": msg.get("bodyPreview") or "",
        "body_text": html_to_text(content) if is_html else content,
        "body_html": content if is_html else "",
        "synced_at": db.now_iso(),
    }


class GraphError(RuntimeError):
    pass


async def _get(client: httpx.AsyncClient, url: str, token: str, params: dict | None = None) -> dict:
    """One GET with backoff. Graph 429s carry Retry-After; honour it rather than
    hammering, and never drop the page we were fetching."""
    delay = 1.0
    for attempt in range(5):
        resp = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code in (429, 503, 504):
            wait = float(resp.headers.get("Retry-After", delay))
            await asyncio.sleep(min(wait, 30))
            delay = min(delay * 2, 30)
            continue
        if resp.status_code == 401:
            raise auth.AuthError("Microsoft rejected the access token.")
        if resp.status_code >= 400:
            raise GraphError(f"Graph {resp.status_code}: {resp.text[:300]}")
        return resp.json()
    raise GraphError("Microsoft Graph kept rate-limiting the request; try again in a minute.")


async def fetch_profile() -> dict[str, Any]:
    token = auth.get_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        data = await _get(client, f"{GRAPH_BASE}/me", token)
    return {
        "display_name": data.get("displayName"),
        "address": (data.get("mail") or data.get("userPrincipalName") or "").lower(),
    }


async def sync_inbox(days: int | None = None, max_messages: int | None = None) -> dict[str, Any]:
    """Pull recent inbox mail and upsert it. Called on demand from the refresh
    button and by the background poller while the window is open."""
    days = days if days is not None else int(db.get_setting("sync_days", "30") or 30)
    max_messages = max_messages if max_messages is not None else int(
        db.get_setting("sync_max_messages", "300") or 300
    )
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0)
    since_str = since.isoformat().replace("+00:00", "Z")

    token = auth.get_access_token()
    url = f"{GRAPH_BASE}/me/mailFolders/inbox/messages"
    params: dict | None = {
        "$select": SELECT_FIELDS,
        "$top": "50",
        "$orderby": "receivedDateTime desc",
        "$filter": f"receivedDateTime ge {since_str}",
    }

    fetched: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=60) as client:
        while url and len(fetched) < max_messages:
            payload = await _get(client, url, token, params)
            for msg in payload.get("value", []):
                fetched.append(_to_row(msg))
                if len(fetched) >= max_messages:
                    break
            url = payload.get("@odata.nextLink")
            params = None  # nextLink already carries the query string

    written = db.upsert_emails(fetched)
    return {"fetched": len(fetched), "written": written, "since": since_str}
