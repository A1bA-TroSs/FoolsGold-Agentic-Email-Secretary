"""The HTTP surface. These are thin on purpose -- the rules are tested in
test_relevance.py and test_learning.py, and what remains to check here is that
the endpoints are actually reachable and that the failure cases fail loudly
rather than returning a confident default."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, learning
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    with TestClient(app) as c:
        yield c


def test_state_reports_learning_off_on_a_fresh_install(client):
    body = client.get("/api/ranking").json()
    assert body["learning"]["enabled"] is False
    assert body["learning"]["epoch_start"] == ""
    assert body["thresholds"]["action"] > 0


def test_starting_the_epoch_switches_learning_on(client):
    assert client.post("/api/ranking/epoch").json()["enabled"] is True
    assert client.get("/api/ranking").json()["learning"]["enabled"] is True


def test_the_volume_control_refuses_when_there_is_nothing_to_measure(client):
    """409, not a plausible-looking threshold. Solving a boundary from an empty
    sample would return a number with nothing behind it."""
    r = client.post("/api/ranking/volume", json={"target_per_day": 5})
    assert r.status_code == 409


def test_the_volume_control_works_once_mail_is_classified(client):
    for i, score in enumerate([0.95, 0.9, 0.8, 0.6, 0.4, 0.2]):
        db.save_classification({
            "email_id": f"e{i}", "bucket": "fyi", "deadline": None, "rationale": "",
            "score": 0, "matched": "", "model": "t", "source": "structural",
            "created_at": db.now_iso(), "actionability": score, "relevance": 0.5,
            "explored": 0,
        })
    body = client.post("/api/ranking/volume", json={"target_per_day": 2}).json()
    assert body["sampled"] == 6
    kept = [s for s in [0.95, 0.9, 0.8, 0.6, 0.4, 0.2] if s >= body["action"]]
    assert len(kept) == 2


def test_explain_is_404_for_an_email_that_was_never_classified(client):
    assert client.get("/api/ranking/explain/nope").status_code == 404


def test_events_returns_refusals_too(client):
    db.record_learning_event("e1", "explicit_correction", 1.0, False, "pre-epoch")
    body = client.get("/api/ranking/events").json()
    assert body["events"], "an empty list would make the assert below vacuous"
    assert body["events"][0]["reason"] == "pre-epoch"
    assert body["summary"]["refused_by_reason"]["pre-epoch"] == 1


def test_reset_clears_learned_weights_only(client):
    db.save_ranking_weights({"topic_match": 0.4})
    assert db.ranking_weights()
    assert client.post("/api/ranking/reset").json()["weights"] == {}
