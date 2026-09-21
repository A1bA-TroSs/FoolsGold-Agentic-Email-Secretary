"""Bring-your-own-key: the paths a stranger's first hour actually takes.

The happy path was never the risk. The risks are a key that is missing, wrong,
expired or rate limited, a model name the account cannot use, and a reply cut
off at the token limit -- all of which used to arrive as the same opaque
sentence, and one of which permanently degraded the inbox.

Nothing here talks to a real API. What is under test is everything wrapped
around the request: selection, retry, truncation, and what the user is told.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import consent, db
from app.llm import registry
from app.llm.api_providers import AnthropicProvider, OpenAIProvider, scrub
from app.llm.base import ProviderUnavailable

KEY = "sk-test-0123456789abcdefghijklmnopqrstuvwxyz"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    from app import crypto, config
    monkeypatch.setattr(config, "KEY_PATH", tmp_path / "secret.key")
    monkeypatch.setattr(crypto, "KEY_PATH", tmp_path / "secret.key")
    monkeypatch.setattr(crypto, "DATA_DIR", tmp_path)
    monkeypatch.setattr(crypto, "_fernet", None)
    db.init_db()
    registry.reset_cache()
    return tmp_path


@pytest.fixture()
def no_sleep(monkeypatch):
    """Skip the backoff waits. Capture the real sleep first -- patching with a
    lambda that calls asyncio.sleep recurses into itself."""
    real = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real(0))


def _transport(handler):
    """Drive a provider against a scripted server."""
    return httpx.MockTransport(handler)


def _run(provider, handler):
    """Run `complete()` with httpx pointed at a scripted handler."""
    import app.llm.api_providers as mod
    real = httpx.AsyncClient

    class Patched(real):
        def __init__(self, *a, **kw):
            kw["transport"] = _transport(handler)
            super().__init__(*a, **kw)

    mod.httpx.AsyncClient = Patched
    try:
        return asyncio.run(provider.complete("sys", "user"))
    finally:
        mod.httpx.AsyncClient = real


def _anthropic_ok(text="[]", stop_reason="end_turn"):
    def handler(request):
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
        })
    return handler


# --------------------------------------------------------------------------
# nothing runs on a credential the user did not choose
# --------------------------------------------------------------------------

def test_no_provider_is_selected_out_of_the_box(store):
    """Shipping with one preselected meant a stranger's first sync reached for
    whatever session happened to be on their machine."""
    assert db.get_setting("llm_provider") == "none"
    with pytest.raises(ProviderUnavailable):
        registry.get_provider()


def test_choosing_a_provider_without_a_key_refuses_up_front(store):
    """It used to be discovered inside the batch loop, where it was reported as
    a provider outage rather than as "you have not finished setting this up"."""
    db.set_setting("llm_provider", "anthropic")
    with pytest.raises(ProviderUnavailable) as err:
        registry.get_provider()
    assert "key" in str(err.value).lower()
    assert registry.is_configured() is False


def test_a_key_makes_it_configured(store):
    db.set_setting("llm_provider", "anthropic")
    db.set_setting("anthropic_api_key", KEY)
    assert registry.is_configured() is True
    consent.grant("anthropic")    # a key alone no longer sends mail anywhere
    assert registry.get_provider().name == "anthropic"


def test_an_unknown_provider_name_is_refused_not_guessed(store):
    db.set_setting("llm_provider", "gemini")
    with pytest.raises(ProviderUnavailable):
        registry.get_provider()


def test_the_openai_endpoint_is_configurable(store):
    """So the same provider serves Azure, OpenRouter or a local server. The
    constructor took a base_url that nothing ever passed."""
    db.set_setting("llm_provider", "openai")
    db.set_setting("openai_api_key", KEY)
    db.set_setting("openai_base_url", "https://openrouter.ai/api/v1/")
    consent.grant("openai")
    assert registry.get_provider().base_url == "https://openrouter.ai/api/v1"


def test_the_endpoint_falls_back_to_openai_when_blank(store):
    db.set_setting("llm_provider", "openai")
    db.set_setting("openai_api_key", KEY)
    consent.grant("openai")
    assert registry.get_provider().base_url == "https://api.openai.com/v1"


# --------------------------------------------------------------------------
# a key you can actually remove
# --------------------------------------------------------------------------

def test_a_key_can_be_cleared(store):
    """Encrypting "" produces a perfectly good token, so the row stayed
    non-empty: the UI reported the key as saved while every request failed,
    and there was no way to remove one."""
    db.set_setting("anthropic_api_key", KEY)
    assert db.all_settings()["anthropic_api_key_set"] is True

    db.set_setting("anthropic_api_key", "")
    assert db.all_settings()["anthropic_api_key_set"] is False
    assert db.get_setting("anthropic_api_key") == ""


def test_a_stored_key_is_never_returned_in_the_clear(store):
    db.set_setting("anthropic_api_key", KEY)
    assert db.all_settings()["anthropic_api_key"] == "********"


def test_whitespace_only_is_treated_as_cleared(store):
    db.set_setting("anthropic_api_key", "   ")
    assert db.all_settings()["anthropic_api_key_set"] is False


# --------------------------------------------------------------------------
# keys never reach a log, a banner or an error string
# --------------------------------------------------------------------------

def test_scrub_removes_the_key_by_value():
    assert KEY not in scrub(f"failed with {KEY} attached", KEY)


def test_scrub_removes_anything_key_shaped_it_did_not_send():
    """Some gateways echo the request headers into their error body."""
    for leak in ("sk-abcdefghijklmnop", "ghp_ABCDEFGHIJKLMNOP",
                 "Bearer eyJhbGciOiJIUzI1NiJ9", "A" * 40):
        assert leak not in scrub(f"upstream said: {leak}")


def test_an_error_carries_the_message_not_the_body(store):
    def handler(request):
        return httpx.Response(401, json={
            "error": {"type": "authentication_error", "message": "invalid x-api-key"},
            "echoed_headers": {"x-api-key": KEY},
        })
    with pytest.raises(ProviderUnavailable) as err:
        _run(AnthropicProvider(KEY), handler)
    text = str(err.value)
    assert "invalid x-api-key" in text
    assert KEY not in text
    assert "echoed_headers" not in text


def test_a_rejected_key_says_so_plainly(store):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "nope"}})
    with pytest.raises(ProviderUnavailable) as err:
        _run(AnthropicProvider(KEY), handler)
    assert "rejected the key" in str(err.value)


def test_an_unknown_model_names_the_model(store):
    def handler(request):
        return httpx.Response(404, json={"error": {"message": "model not found"}})
    with pytest.raises(ProviderUnavailable) as err:
        _run(AnthropicProvider(KEY, "claude-does-not-exist"), handler)
    assert "claude-does-not-exist" in str(err.value)


# --------------------------------------------------------------------------
# rate limits are the expected case for a new key
# --------------------------------------------------------------------------

def test_a_rate_limit_is_retried_and_then_succeeds(store, no_sleep):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "0"},
                                  json={"error": {"message": "slow down"}})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "[]"}]})

    assert _run(AnthropicProvider(KEY), handler) == "[]"
    assert calls["n"] == 3, "it should have retried, not failed on the first 429"


def test_a_persistent_rate_limit_eventually_gives_up_with_a_clear_message(store, no_sleep):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    with pytest.raises(ProviderUnavailable) as err:
        _run(AnthropicProvider(KEY), handler)
    assert "rate limit" in str(err.value).lower()
    assert calls["n"] > 1


def test_a_server_error_is_retried(store, no_sleep):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="upstream down")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "[]"}]})

    assert _run(AnthropicProvider(KEY), handler) == "[]"


def test_a_rejected_key_is_not_retried(store, monkeypatch):
    """401 will not fix itself, and retrying it wastes the user's quota."""
    monkeypatch.setattr(asyncio, "sleep", lambda *_: asyncio.sleep(0))
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"message": "nope"}})

    with pytest.raises(ProviderUnavailable):
        _run(AnthropicProvider(KEY), handler)
    assert calls["n"] == 1


# --------------------------------------------------------------------------
# a truncated reply is not a broken key
# --------------------------------------------------------------------------

def test_a_reply_cut_off_at_the_token_limit_says_so(store):
    """It arrives as a valid 200 carrying unparseable JSON, which looked
    exactly like a rejected key -- and the fix is a different setting."""
    with pytest.raises(ProviderUnavailable) as err:
        _run(AnthropicProvider(KEY), _anthropic_ok(text='[{"id":', stop_reason="max_tokens"))
    assert "cut off" in str(err.value)
    assert "emails per AI request" in str(err.value)


def test_openai_truncation_is_caught_too(store):
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "length", "message": {"content": '[{"id":'}}],
        })
    with pytest.raises(ProviderUnavailable) as err:
        _run(OpenAIProvider(KEY), handler)
    assert "cut off" in str(err.value)


def test_a_refusal_is_not_reported_as_a_json_error(store):
    """`content: null` used to become "" and surface as "no JSON found"."""
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "content_filter", "message": {"content": None}}],
        })
    with pytest.raises(ProviderUnavailable) as err:
        _run(OpenAIProvider(KEY), handler)
    assert "refused" in str(err.value)


def test_html_from_a_captive_portal_is_named_as_such(store):
    def handler(request):
        return httpx.Response(200, text="<html>Sign in to the network</html>")
    with pytest.raises(ProviderUnavailable) as err:
        _run(AnthropicProvider(KEY), handler)
    assert "not JSON" in str(err.value)


def test_a_good_reply_comes_back_whole(store):
    assert _run(AnthropicProvider(KEY), _anthropic_ok(text='[{"id":"a"}]')) == '[{"id":"a"}]'


def test_openai_sends_the_configured_base_url(store):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _run(OpenAIProvider(KEY, "gpt-4o-mini", "https://example.test/v1"), handler)
    assert seen["url"] == "https://example.test/v1/chat/completions"
