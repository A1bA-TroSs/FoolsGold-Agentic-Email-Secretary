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


def test_state_reports_how_many_rows_are_shown_as_a_guess(client):
    """Exploration is otherwise unobservable. Without a count, "working and
    nothing selected" and "silently broken" look identical from the UI -- and
    it WAS silently broken: the mail list filtered out every noise row,
    including the ones exploration had just chosen."""
    assert client.get("/api/ranking").json()["explored_count"] == 0

    for i, explored in enumerate([1, 0, 1]):
        db.save_classification({
            "email_id": f"x{i}", "bucket": "noise", "deadline": None, "rationale": "",
            "score": 0, "matched": "", "model": "t", "source": "structural",
            "created_at": db.now_iso(), "actionability": 0.1, "relevance": 0.1,
            "explored": explored, "model_bucket": "noise",
        })
    assert client.get("/api/ranking").json()["explored_count"] == 2


def test_rescore_rederives_buckets_without_a_model_call(client, monkeypatch):
    """A ranking-rule change was invisible until "Re-read everything", which
    calls the model over the whole mailbox -- the cheap fix gated behind the
    expensive one."""
    from app import db, pipeline

    called = []
    monkeypatch.setattr(pipeline, "get_provider",
                        lambda *a, **k: called.append(1) or (_ for _ in ()).throw(AssertionError))
    db.upsert_emails([dict(
        id="r1", conversation_id=None, subject="Weekly round-up: 40% off",
        from_name="ShopCo", from_address="noreply@shop.com", to_recipients="[]",
        cc_recipients="[]", received_at="2026-09-16T09:00:00+00:00", is_read=0,
        is_answered=0, is_flagged=0, has_attachments=0, importance="normal",
        web_link="", folder="INBOX", body_preview="", body_text="Sale ends soon",
        body_html="", synced_at=db.now_iso())])
    db.save_classification({
        "email_id": "r1", "bucket": "action", "deadline": None, "rationale": "",
        "score": 90, "matched": "", "model": "old", "source": "llm",
        "created_at": db.now_iso(), "actionability": 0.9, "relevance": 0.9,
        "explored": 0, "model_bucket": "action", "reason_code": "none", "reason_arg": "",
    })

    body = client.post("/api/ranking/rescore").json()
    assert body["rescored"] >= 1
    assert not called, "rescore must not reach for a provider"
    with db.connect() as conn:
        assert conn.execute(
            "SELECT bucket FROM classifications WHERE email_id='r1'").fetchone()[0] == "noise", (
            "a promotional email stored as 'action' under the old rules must be re-derived")
