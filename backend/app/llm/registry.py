"""Builds the provider the user picked in Settings.

Two rules this file exists to enforce:

1. **Nothing runs on a credential the user did not choose.** There is no
   bundled key, and the default provider is `none` -- the app ranks mail
   structurally until somebody makes a deliberate choice. Shipping with a
   provider preselected meant a stranger's first sync quietly reached for
   whatever GitHub session happened to be on their machine.

2. **A missing key is a configuration answer, not a runtime failure.** It used
   to be discovered inside the batch loop, where it landed in a blanket
   `except` and was reported as an outage. `get_provider()` now refuses up
   front with `ProviderUnavailable`, which the pipeline already knows how to
   report as "AI is not set up".
"""
from __future__ import annotations

from .. import db
from .api_providers import AnthropicProvider, OpenAIProvider
from .base import LLMProvider, ProviderUnavailable
from .copilot_provider import CopilotProvider

PROVIDERS = ("none", "anthropic", "openai", "copilot")

# Which setting holds the key for each provider, so the UI and the checks below
# agree about what "configured" means.
KEY_SETTING = {"anthropic": "anthropic_api_key", "openai": "openai_api_key"}

_cached: tuple[str, str, LLMProvider] | None = None


def configured_provider() -> str:
    return (db.get_setting("llm_provider", "none") or "none").lower()


def is_configured(name: str | None = None) -> bool:
    """Whether the chosen provider could actually run right now.

    Copilot has no key of its own -- it uses a GitHub sign-in held outside this
    app -- so all we can say about it here is that it was chosen deliberately."""
    name = (name or configured_provider()).lower()
    if name == "none":
        return False
    if name == "copilot":
        return True
    setting = KEY_SETTING.get(name)
    return bool(setting and db.get_setting(setting, "").strip())


def get_provider() -> LLMProvider:
    """Cached per (provider, model) so a Copilot runtime is started once rather
    than once per classification batch."""
    global _cached
    name = configured_provider()

    if name == "none":
        raise ProviderUnavailable("AI is turned off in Settings; using structural signals only.")
    if name not in PROVIDERS:
        raise ProviderUnavailable(f"Unknown AI provider '{name}'. Pick one in Settings.")

    if name == "copilot":
        model = db.get_setting("copilot_model", "auto")
    elif name == "anthropic":
        model = db.get_setting("anthropic_model", "claude-sonnet-4-5")
    else:
        model = db.get_setting("openai_model", "gpt-4o-mini")

    # Refuse before building anything, so "you have not added a key yet" never
    # reaches the user disguised as a provider outage.
    setting = KEY_SETTING.get(name)
    if setting and not db.get_setting(setting, "").strip():
        raise ProviderUnavailable(
            f"No API key for {name}. Add one in Settings, or turn AI off."
        )

    if _cached and _cached[0] == name and _cached[1] == model:
        return _cached[2]

    reset_cache()
    if name == "copilot":
        provider: LLMProvider = CopilotProvider(model)
    elif name == "anthropic":
        provider = AnthropicProvider(db.get_setting("anthropic_api_key", ""), model)
    else:
        provider = OpenAIProvider(
            db.get_setting("openai_api_key", ""),
            model,
            db.get_setting("openai_base_url", ""),
        )

    _cached = (name, model, provider)
    return provider


def reset_cache() -> None:
    """Drop the cached provider, shutting it down if it holds anything.

    Copilot starts a long-lived runtime; dropping the reference alone leaked
    one on every settings save."""
    global _cached
    previous = _cached
    _cached = None
    if previous is None:
        return
    closer = getattr(previous[2], "close_sync", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001 - shutting down must never raise
            pass
