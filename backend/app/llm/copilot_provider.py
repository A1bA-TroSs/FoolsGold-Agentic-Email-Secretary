"""GitHub Copilot SDK provider -- the default for v1.0.

Why this and not the old free inference API: GitHub Models was retired on
2026-07-30 (playground, catalog, inference API and BYOK, for everyone). The
Copilot SDK went GA on 2026-06-02 and is the supported route for a Copilot
subscriber, including Copilot Free and student Pro.

Auth is GitHub's OAuth device flow, handled by the SDK; credentials land in the
system keychain, so Fools Gold never sees or stores a GitHub token.

Cost note: Copilot bills in premium requests, not tokens. That is why the
caller batches ~10 emails per request and caches every result in SQLite --
a full inbox should cost a handful of requests a day, not hundreds.

We deliberately create sessions with no tools and no skills: this is a pure
one-shot text call, not an agent that should be reading the user's filesystem.
"""
from __future__ import annotations

import asyncio
from typing import Any

from .base import LLMProvider, ProviderUnavailable

_TIMEOUT_SECONDS = 120.0


def _response_text(event: Any) -> str:
    """Pull the assistant text out of the final SessionEvent, tolerantly --
    the SDK's event payload shape is not part of its stable contract."""
    if event is None:
        return ""
    data = getattr(event, "data", event)
    for attr in ("text", "content", "message"):
        value = getattr(data, attr, None)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            parts = []
            for chunk in value:
                if isinstance(chunk, str):
                    parts.append(chunk)
                else:
                    inner = getattr(chunk, "text", None) or (
                        chunk.get("text") if isinstance(chunk, dict) else None
                    )
                    if isinstance(inner, str):
                        parts.append(inner)
            if parts:
                return "".join(parts)
    if isinstance(data, dict):
        for key in ("text", "content", "message"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key]
    return str(data)


class CopilotProvider(LLMProvider):
    name = "copilot"

    def __init__(self, model: str = "auto") -> None:
        self.model = model or "auto"
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> Any:
        try:
            from copilot import CopilotClient
        except ImportError as exc:  # pragma: no cover - install-time problem
            raise ProviderUnavailable(
                "The Copilot SDK is not installed. Run: pip install github-copilot-sdk"
            ) from exc

        async with self._lock:
            if self._client is None:
                client = CopilotClient(log_level="error")
                try:
                    await client.start()
                except Exception as exc:  # noqa: BLE001
                    raise ProviderUnavailable(
                        f"Could not start the Copilot runtime: {exc}. "
                        "Sign in with 'copilot' once in a terminal, or run the device-flow "
                        "login from Settings."
                    ) from exc
                self._client = client
        return self._client

    async def auth_status(self) -> dict[str, Any]:
        """Surfaced in Settings so the user can see whether Copilot is signed in
        before they wonder why classification is falling back to structural."""
        try:
            client = await self._get_client()
            status = await client.get_auth_status()
        except ProviderUnavailable as exc:
            return {"signed_in": False, "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"signed_in": False, "detail": str(exc)}
        signed_in = bool(getattr(status, "authenticated", None) or getattr(status, "signed_in", None))
        return {
            "signed_in": signed_in,
            "detail": getattr(status, "user", None) or getattr(status, "status", "") or "",
        }

    async def list_models(self) -> list[str]:
        try:
            client = await self._get_client()
            models = await client.list_models()
        except Exception:  # noqa: BLE001
            return []
        names = []
        for m in models or []:
            ident = getattr(m, "id", None) or getattr(m, "name", None)
            if ident:
                names.append(str(ident))
        return names

    async def complete(self, system: str, user: str) -> str:
        client = await self._get_client()
        session = await client.create_session(
            model=None if self.model in ("", "auto") else self.model,
            system_message={"mode": "replace", "content": system},
            available_tools=[],       # no file, shell or MCP access
            enable_skills=False,
            skip_custom_instructions=True,
            streaming=False,
            client_name="foolsgold",
        )
        try:
            event = await session.send_and_wait(user, timeout=_TIMEOUT_SECONDS)
            text = _response_text(event)
            if not text.strip():
                raise ProviderUnavailable("Copilot returned an empty response.")
            return text
        finally:
            try:
                await session.disconnect()
            except Exception:  # noqa: BLE001 - never let cleanup mask the real error
                pass

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.stop()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
