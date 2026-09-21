"""The cloud-AI consent gate (Apple 5.1.2(i), Korea PIPA overseas transfer).

The property that matters: **no mail reaches a cloud provider the user has not
explicitly allowed**, enforced where every AI call passes -- get_provider() --
not in the UI. Each test below is a way that property could quietly break.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import consent, db
from app.llm import registry
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
    yield tmp_path
    registry.reset_cache()


def _anthropic():
    db.set_setting("llm_provider", "anthropic")
    db.set_setting("anthropic_api_key", KEY)


def test_a_key_alone_does_not_open_the_door(store):
    _anthropic()
    with pytest.raises(ProviderUnavailable) as exc:
        registry.get_provider()
    assert exc.value.key == "aiConsentRequired"
    assert exc.value.vars == {"recipient": "Anthropic, PBC"}


def test_an_explicit_grant_opens_it(store):
    _anthropic()
    consent.grant("anthropic")
    assert registry.get_provider().name == "anthropic"


def test_withdrawing_closes_it_on_the_next_request_even_when_cached(store):
    _anthropic()
    consent.grant("anthropic")
    registry.get_provider()                # now cached
    assert consent.withdraw("anthropic") == 1
    with pytest.raises(ProviderUnavailable):
        registry.get_provider()


def test_a_grant_is_for_one_provider_not_all(store):
    _anthropic()
    consent.grant("anthropic")
    db.set_setting("llm_provider", "openai")
    db.set_setting("openai_api_key", KEY)
    with pytest.raises(ProviderUnavailable):
        registry.get_provider()


def test_a_new_server_is_a_new_recipient(store):
    """Allowing api.openai.com is not allowing whatever host the base URL is
    later pointed at."""
    db.set_setting("llm_provider", "openai")
    db.set_setting("openai_api_key", KEY)
    consent.grant("openai")
    assert registry.get_provider()
    db.set_setting("openai_base_url", "https://some-proxy.example.com/v1")
    registry.reset_cache()
    with pytest.raises(ProviderUnavailable) as exc:
        registry.get_provider()
    assert exc.value.vars["recipient"] == "some-proxy.example.com"


@pytest.mark.parametrize("base", ["http://localhost:1234/v1", "http://127.0.0.1:8080/v1",
                                  "http://[::1]:11434/v1"])
def test_a_server_on_this_mac_needs_no_permission(store, base):
    db.set_setting("llm_provider", "openai")
    db.set_setting("openai_api_key", KEY)
    db.set_setting("openai_base_url", base)
    assert consent.requires_consent("openai") is False
    assert registry.get_provider()


def test_local_models_and_off_never_ask(store):
    assert consent.requires_consent("ollama") is False
    assert consent.requires_consent("none") is False


def test_copilot_asks_too(store):
    """No key to paste is not the same as nothing being sent."""
    assert consent.requires_consent("copilot") is True
    assert consent.is_granted("copilot") is False


def test_a_new_disclosure_version_asks_again(store, monkeypatch):
    _anthropic()
    consent.grant("anthropic")
    monkeypatch.setattr(consent, "DISCLOSURE_VERSION", consent.DISCLOSURE_VERSION + 1)
    assert consent.is_granted("anthropic") is False


def test_the_ledger_keeps_withdrawn_grants(store):
    _anthropic()
    consent.grant("anthropic")
    consent.withdraw()
    consent.grant("anthropic")
    with db.connect() as conn:
        rows = conn.execute("SELECT withdrawn_at FROM ai_consent ORDER BY id").fetchall()
    assert [r["withdrawn_at"] is None for r in rows] == [False, True]


def test_granting_twice_records_once(store):
    _anthropic()
    consent.grant("anthropic")
    consent.grant("anthropic")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_consent").fetchone()[0] == 1


def test_the_settings_form_cannot_forge_a_grant(store):
    from app.main import app
    _anthropic()
    client = TestClient(app)
    r = client.patch("/api/settings", json={"values": {"ai_consent": "yes"}})
    assert r.status_code == 400
    assert consent.is_granted("anthropic") is False


def test_the_api_round_trip(store):
    from app.main import app
    _anthropic()
    client = TestClient(app)
    s = client.get("/api/ai/consent").json()
    assert s["required"] is True and s["granted"] is False
    assert s["recipient"] == "Anthropic, PBC" and s["country"] == "US"
    assert client.post("/api/ai/consent", json={"provider": "anthropic"}).json()["granted"] is True
    assert client.delete("/api/ai/consent", params={"provider": "anthropic"}).json()["granted"] is False


def test_the_pipeline_falls_back_and_names_the_reason(store):
    """Without permission the app still ranks -- structurally -- and says why in
    a key the UI can word, not an English sentence."""
    from app.llm.base import ProviderUnavailable as PU
    _anthropic()
    try:
        registry.get_provider()
    except PU as exc:
        assert exc.key == "aiConsentRequired"
    else:  # pragma: no cover
        pytest.fail("the gate let a request through")
