"""The local provider, against a faithful Ollama over real HTTP.

No model runs here -- the container cannot download one and could not usefully
run a 9B if it could. What CAN be verified without a model is everything that
has historically gone wrong: the request shape, the schema, the envelope, and
every failure mode reported as itself rather than as a generic outage. The one
thing left is whether a real 9B produces good judgement, and
`scripts/check_local_model.py` runs that on the machine that has one.

The stub speaks Ollama's documented `/api/chat` and `/api/tags` contract,
including `done_reason` and the 404 for a model that was never pulled.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.llm.base import ProviderUnavailable
from app.llm.ollama_provider import CLASSIFY_SCHEMA, OllamaProvider

STATE: dict = {}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode() if isinstance(payload, (dict, list)) else payload.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._send(STATE.get("tags_code", 200),
                              {"models": STATE.get("models", [{"name": "qwen3.5:9b"}])})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        STATE["last_request"] = json.loads(self.rfile.read(length) or b"{}")
        if STATE.get("chat_code", 200) != 200:
            return self._send(STATE["chat_code"], {"error": STATE.get("chat_error", "boom")})
        if STATE.get("not_json"):
            return self._send(200, "<html>a proxy ate this</html>")
        self._send(200, {
            "model": "qwen3.5:9b",
            "message": {"role": "assistant", "content": STATE.get("content", "{}")},
            "done": True,
            "done_reason": STATE.get("done_reason", "stop"),
        })


@pytest.fixture()
def server():
    STATE.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def emails(n=2):
    return [{"id": f"e{i}", "subject": f"Subject {i}", "from_name": "X",
             "from_address": "x@y.com", "body_text": "Body.", "received_at":
             "2026-09-16T09:00:00+00:00", "to_recipients": "[]", "cc_recipients": "[]"}
            for i in range(n)]


# --------------------------------------------------------------------------
# the request
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_schema_makes_a_wrong_bucket_impossible_to_emit(server):
    """The single highest-value line in the provider. An enum in the schema
    means "actoin", "Action needed" and a paragraph of reasoning cannot be
    produced -- rather than being things the parser has to survive."""
    STATE["content"] = json.dumps({"results": [
        {"id": "e0", "bucket": "action"}, {"id": "e1", "bucket": "noise"}]})
    await OllamaProvider("m", server).classify_batch(emails(), ["thesis"])

    fmt = STATE["last_request"]["format"]
    bucket = fmt["properties"]["results"]["items"]["properties"]["bucket"]
    assert bucket["enum"] == ["action", "fyi", "noise"]
    assert fmt == CLASSIFY_SCHEMA


@pytest.mark.asyncio
async def test_thinking_is_off_and_sampling_is_deterministic(server):
    """Qwen3.x reasons by default: pure latency on a task needing no reasoning
    trace, on a laptop also running the user's real work. Temperature 0 is what
    makes a disagreement between two runs mean something."""
    STATE["content"] = json.dumps({"results": []})
    await OllamaProvider("m", server).classify_batch(emails(1), [])
    req = STATE["last_request"]
    assert req["think"] is False
    assert req["options"]["temperature"] == 0
    assert req["stream"] is False
    assert req["keep_alive"] == "30m", "a burst-then-idle workload must not reload every burst"


# --------------------------------------------------------------------------
# the response
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_envelope_is_unwrapped(server):
    STATE["content"] = json.dumps({"results": [
        {"id": "e0", "bucket": "action", "deadline": "2026-09-20",
         "tasks": [{"title": "Reply to Lee", "due": "2026-09-20"}]},
        {"id": "e1", "bucket": "fyi"}]})
    out = await OllamaProvider("m", server).classify_batch(emails(), [])
    assert [c.bucket for c in out] == ["action", "fyi"]
    assert out[0].tasks[0].title == "Reply to Lee"


@pytest.mark.asyncio
async def test_a_bare_array_still_works(server):
    """A model that ignored the envelope is still giving the right answer.
    Failing here would turn a cosmetic difference into an outage."""
    STATE["content"] = json.dumps([{"id": "e0", "bucket": "noise"},
                                   {"id": "e1", "bucket": "fyi"}])
    out = await OllamaProvider("m", server).classify_batch(emails(), [])
    assert [c.bucket for c in out] == ["noise", "fyi"]


# --------------------------------------------------------------------------
# every failure reported as itself
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_truncated_answer_is_refused_not_parsed(server):
    """`done_reason: length` means the JSON stopped mid-object. Handing that to
    the parser yields a confident wrong answer for whichever emails came first
    -- worse than no answer, because nothing looks broken."""
    STATE["content"] = '{"results": [{"id": "e0", "bucket": "act'
    STATE["done_reason"] = "length"
    with pytest.raises(ProviderUnavailable, match="token limit"):
        await OllamaProvider("m", server).classify_batch(emails(), [])


@pytest.mark.asyncio
async def test_a_model_that_was_never_pulled_says_so(server):
    STATE["chat_code"] = 404
    with pytest.raises(ProviderUnavailable, match="ollama pull"):
        await OllamaProvider("qwen3.5:9b", server).classify_batch(emails(), [])


@pytest.mark.asyncio
async def test_no_server_at_all_names_the_host(server):
    with pytest.raises(ProviderUnavailable, match="No local model server"):
        await OllamaProvider("m", "http://127.0.0.1:1").classify_batch(emails(), [])


@pytest.mark.asyncio
async def test_a_non_json_body_is_reported_verbatim(server):
    STATE["not_json"] = True
    with pytest.raises(ProviderUnavailable, match="not JSON"):
        await OllamaProvider("m", server).classify_batch(emails(), [])


@pytest.mark.asyncio
async def test_a_500_carries_the_server_text(server):
    STATE["chat_code"] = 500
    STATE["chat_error"] = "out of memory"
    with pytest.raises(ProviderUnavailable, match="500"):
        await OllamaProvider("m", server).classify_batch(emails(), [])


# --------------------------------------------------------------------------
# check() answers what Settings is actually asking
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_confirms_the_chosen_model_is_present(server):
    STATE["models"] = [{"name": "qwen3.5:9b"}, {"name": "gemma3:4b"}]
    ok, detail = await OllamaProvider("qwen3.5:9b", server).check()
    assert ok and "ready" in detail


@pytest.mark.asyncio
async def test_check_names_what_is_available_when_the_choice_is_missing(server):
    """It names what IS here, and no longer says "ollama pull".

    The old message ended with `Try: ollama pull qwen3.5:9b`. On the machine
    this was reported from that sentence asked for a six-gigabyte download
    from someone who had deliberately pulled the 4b because it is the one that
    fits a laptop. The useful sentence is "you have these" -- the assertion
    changed because the decision changed, not because the code drifted.
    """
    STATE["models"] = [{"name": "gemma3:4b"}]
    result = await OllamaProvider("qwen3.5:9b", server).check()
    ok, detail = result
    assert not ok
    assert "gemma3:4b" in detail
    assert "ollama pull" not in detail
    # And it says so in a form the UI can translate.
    assert result.key == "aiModelMissingHave"
    assert result.vars["have"] == "gemma3:4b"
    assert result.models == ["gemma3:4b"]


@pytest.mark.asyncio
async def test_check_fails_when_only_a_different_SIZE_is_installed(server):
    """The bug that made Settings say "ready" while every call 404'd.

    The old test was `installed.startswith(configured.split(":")[0])` --
    "same family, close enough". `qwen3.5:4b` starts with `qwen3.5`, so a
    configured `qwen3.5:9b` matched it and the check passed, on a machine
    where `/api/chat` refused the model on every single request. **A check
    that cannot fail for the actual reason is not a check.**
    """
    STATE["models"] = [{"name": "qwen3.5:4b"}]
    ok, _ = await OllamaProvider("qwen3.5:9b", server).check()
    assert not ok


@pytest.mark.asyncio
async def test_check_accepts_a_quantisation_of_the_same_model(server):
    STATE["models"] = [{"name": "qwen3.5:9b-q4_K_M"}]
    ok, _ = await OllamaProvider("qwen3.5:9b", server).check()
    assert ok


def test_model_matching_rules():
    from app.llm.ollama_provider import model_matches
    assert model_matches("qwen3.5:9b", "qwen3.5:9b")
    assert model_matches("qwen3.5:9b", "qwen3.5:9b-q4_K_M")
    # A bare family name means "the one I have".
    assert model_matches("qwen3.5", "qwen3.5:4b")
    # A different size is a different model, whatever it shares as a prefix.
    assert not model_matches("qwen3.5:9b", "qwen3.5:4b")
    assert not model_matches("qwen3.5:9b", "qwen3.5:90b")
    assert not model_matches("qwen3.5:9b", "llama3:8b")
    assert not model_matches("", "qwen3.5:4b")
    assert not model_matches("qwen3.5:9b", "")


@pytest.mark.asyncio
async def test_a_missing_model_names_itself_for_translation(server):
    """The banner used to read as a Korean sentence with an English one bolted
    into the middle. The exception carries a key so the UI can word it."""
    from app.llm.base import ProviderUnavailable
    STATE["chat_code"] = 404
    with pytest.raises(ProviderUnavailable) as caught:
        await OllamaProvider("qwen3.5:9b", server).classify_batch(
            [{"id": "e1", "subject": "s", "body_text": "b", "from_address": "a@b.c"}], [])
    assert caught.value.key == "aiModelMissing"
    assert caught.value.vars == {"model": "qwen3.5:9b"}


@pytest.mark.asyncio
async def test_check_distinguishes_a_running_server_with_no_models(server):
    STATE["models"] = []
    ok, detail = await OllamaProvider("qwen3.5:9b", server).check()
    assert not ok and "no models" in detail


@pytest.mark.asyncio
async def test_check_does_not_generate(server):
    """A generation check on a 9B that must load first takes tens of seconds and
    reports a timeout as a misconfiguration."""
    STATE["models"] = [{"name": "qwen3.5:9b"}]
    await OllamaProvider("qwen3.5:9b", server).check()
    assert "last_request" not in STATE, "check() must not POST to /api/chat"


# --------------------------------------------------------------------------
# it is selectable, and it needs no credential
# --------------------------------------------------------------------------

def test_a_local_model_needs_no_key(tmp_path, monkeypatch):
    from app import db
    from app.llm import registry

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    registry.reset_cache()
    db.set_setting("llm_provider", "ollama")
    assert registry.is_configured() is True
    provider = registry.get_provider()
    assert provider.name == "ollama"
    assert provider.model == "qwen3.5:9b"
    registry.reset_cache()
