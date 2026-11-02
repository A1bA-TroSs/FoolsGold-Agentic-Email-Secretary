"""F5: download an Ollama model from Settings instead of a terminal.

A small fake Ollama answers `POST /api/pull` with the streamed lines the real
one sends, so these run the actual HTTP path: progress, success, an error
line, a dropped stream, an unreachable host, one pull at a time, and name
checks that refuse before anything is sent."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app import db
from app.llm import ollama_pull


class FakeOllama:
    """`script` is the list of JSON objects streamed back; `gate`, when set,
    holds the stream open after the first line until the test releases it."""

    def __init__(self):
        self.script: list[dict] = []
        self.status = 200
        self.gate: threading.Event | None = None
        self.requests: list[dict] = []
        self.tags: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"models": [{"name": n} for n in fake.tags]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                fake.requests.append({"path": self.path, "body": json.loads(self.rfile.read(length) or b"{}")})
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                for i, line in enumerate(fake.script):
                    self.wfile.write((json.dumps(line) + "\n").encode())
                    self.wfile.flush()
                    if i == 0 and fake.gate is not None:
                        fake.gate.wait(5)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        if self.gate is not None:
            self.gate.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    f = FakeOllama()
    yield f
    f.close()


@pytest.fixture
def client(tmp_path, monkeypatch, fake):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(ollama_pull, "_state", ollama_pull._idle())
    db.init_db()
    db.set_setting("ollama_host", fake.host)
    db.set_setting("ollama_model", "qwen3.5:9b")
    from app.main import app
    with TestClient(app) as c:
        yield c


def wait_until_stopped(client, seconds=5.0) -> dict:
    end = time.time() + seconds
    while time.time() < end:
        s = client.get("/api/ollama/pull").json()
        if not s["running"]:
            return s
        time.sleep(0.02)
    raise AssertionError("pull never finished")


PROGRESS = [
    {"status": "pulling manifest"},
    {"status": "pulling abc", "digest": "sha256:abc", "total": 1000, "completed": 250},
    {"status": "pulling abc", "digest": "sha256:abc", "total": 1000, "completed": 1000},
    {"status": "verifying sha256 digest"},
    {"status": "success"},
]


def test_pull_reports_progress_then_done(client, fake):
    fake.script = PROGRESS
    fake.gate = threading.Event()
    r = client.post("/api/ollama/pull", json={"model": "qwen3.5:4b"})
    assert r.status_code == 202
    assert r.json()["running"] is True          # returned before the stream ended
    fake.gate.set()
    s = wait_until_stopped(client)
    assert s["done"] is True and s["error"] == ""
    assert s["completed"] == s["total"] == 1000
    assert fake.requests[0]["path"] == "/api/pull"
    assert fake.requests[0]["body"]["model"] == "qwen3.5:4b"


def test_progress_is_visible_mid_download(client, fake):
    fake.script = [PROGRESS[1], *PROGRESS[2:]]
    fake.gate = threading.Event()
    client.post("/api/ollama/pull", json={"model": "qwen3.5:4b"})
    end = time.time() + 3
    while time.time() < end and client.get("/api/ollama/pull").json()["completed"] != 250:
        time.sleep(0.02)
    mid = client.get("/api/ollama/pull").json()
    assert mid["running"] is True and mid["completed"] == 250 and mid["total"] == 1000
    fake.gate.set()
    assert wait_until_stopped(client)["done"] is True


def test_a_second_pull_is_refused_while_one_runs(client, fake):
    fake.script = PROGRESS
    fake.gate = threading.Event()
    assert client.post("/api/ollama/pull", json={"model": "qwen3.5:4b"}).status_code == 202
    assert client.post("/api/ollama/pull", json={"model": "qwen3.5:9b"}).status_code == 409
    fake.gate.set()
    wait_until_stopped(client)
    assert len(fake.requests) == 1
    # Once finished, another can start.
    assert client.post("/api/ollama/pull", json={"model": "qwen3.5:9b"}).status_code == 202
    wait_until_stopped(client)


@pytest.mark.parametrize("line, kind", [
    ({"error": "pull model manifest: file does not exist"}, "unknown_model"),
    ({"error": "write /models/blobs/x: no space left on device"}, "disk"),
    ({"error": "something new"}, "other"),
])
def test_an_error_line_ends_in_an_error_state(client, fake, line, kind):
    fake.script = [{"status": "pulling manifest"}, line]
    client.post("/api/ollama/pull", json={"model": "qwen3.5:4b"})
    s = wait_until_stopped(client)
    assert s["done"] is False and s["error"] and s["error_kind"] == kind


def test_a_stream_that_stops_early_is_an_error_not_done(client, fake):
    fake.script = PROGRESS[:2]
    client.post("/api/ollama/pull", json={"model": "qwen3.5:4b"})
    s = wait_until_stopped(client)
    assert s["done"] is False and s["error"]


def test_an_unreachable_host_ends_as_no_server(client, fake):
    fake.close()
    client.post("/api/ollama/pull", json={"model": "qwen3.5:4b"})
    s = wait_until_stopped(client)
    assert s["error_kind"] == "no_server" and s["running"] is False


@pytest.mark.parametrize("name", [
    "", "   ", "qwen 3.5", "qwen;rm -rf /", "$(id)", "../../etc/passwd",
    "a/../b", "Qwen", "x" * 121, "qwen:", "/qwen",
])
def test_bad_names_are_refused_before_any_request(client, fake, name):
    r = client.post("/api/ollama/pull", json={"model": name})
    assert r.status_code == 400
    assert fake.requests == []
    assert client.get("/api/ollama/pull").json()["running"] is False


@pytest.mark.parametrize("name", ["qwen3.5:4b", "llama3.2", "library/qwen3.5:9b", "hf.co/user/model-gguf:q4_k_m"])
def test_ordinary_names_are_accepted(name):
    assert ollama_pull.valid_name(name)


def test_a_pull_never_changes_the_chosen_model(client, fake):
    fake.script = PROGRESS
    client.post("/api/ollama/pull", json={"model": "qwen3.5:4b"})
    wait_until_stopped(client)
    assert db.get_setting("ollama_model", "") == "qwen3.5:9b"


def test_suggestions_mark_what_fits_this_mac(monkeypatch):
    monkeypatch.setattr(ollama_pull, "memory_gb", lambda: 8.0)
    s = {x["model"]: x for x in ollama_pull.suggestions()}
    assert s["qwen3.5:4b"]["fits"] and s["qwen3.5:4b"]["suggested"]
    assert not s["qwen3.5:9b"]["fits"] and not s["qwen3.5:9b"]["suggested"]
    monkeypatch.setattr(ollama_pull, "memory_gb", lambda: 32.0)
    s = {x["model"]: x for x in ollama_pull.suggestions()}
    assert s["qwen3.5:9b"]["suggested"] and not s["qwen3.5:4b"]["suggested"]


def test_models_endpoint_says_when_ollama_is_not_answering(client, fake):
    fake.tags = ["qwen3.5:4b"]
    m = client.get("/api/ollama/models").json()
    assert m["reachable"] is True and m["installed"] == ["qwen3.5:4b"]
    fake.close()
    m = client.get("/api/ollama/models").json()
    assert m["reachable"] is False and m["installed"] == []
