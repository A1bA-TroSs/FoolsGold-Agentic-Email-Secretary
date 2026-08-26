"""Builds the provider the user picked in Settings."""
from __future__ import annotations

from .. import db
from .api_providers import AnthropicProvider, OpenAIProvider
from .base import LLMProvider, ProviderUnavailable
from .copilot_provider import CopilotProvider

PROVIDERS = ("copilot", "anthropic", "openai", "none")

_cached: tuple[str, str, LLMProvider] | None = None


def get_provider() -> LLMProvider:
    """Cached per (provider, model) so the Copilot runtime is started once, not
    once per classification batch."""
    global _cached
    name = (db.get_setting("llm_provider", "copilot") or "copilot").lower()

    if name == "none":
        raise ProviderUnavailable("AI is turned off in Settings; using structural signals only.")

    if name == "copilot":
        model = db.get_setting("copilot_model", "auto")
    elif name == "anthropic":
        model = db.get_setting("anthropic_model", "claude-sonnet-4-5")
    elif name == "openai":
        model = db.get_setting("openai_model", "gpt-4o-mini")
    else:
        raise ProviderUnavailable(f"Unknown provider '{name}'.")

    if _cached and _cached[0] == name and _cached[1] == model:
        return _cached[2]

    if name == "copilot":
        provider: LLMProvider = CopilotProvider(model)
    elif name == "anthropic":
        provider = AnthropicProvider(db.get_setting("anthropic_api_key", ""), model)
    else:
        provider = OpenAIProvider(db.get_setting("openai_api_key", ""), model)

    _cached = (name, model, provider)
    return provider


def reset_cache() -> None:
    global _cached
    _cached = None
