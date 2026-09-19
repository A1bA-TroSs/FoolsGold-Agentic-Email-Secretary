"""Claims 9-11 of `scripts/check_local_model.py`, without the model.

That script is the only thing that can judge a real model, and it is also the
only thing that was exercising this path at all -- so the path was broken for
as long as nobody had a model to run it with. These reproduce its arithmetic
from fixed classifications, so the container catches a regression here without
anyone pulling 3.4GB of weights.

What they pin is the single sentence the whole two-axis design rests on:
editing the priority list must change the ranking, and must do it without
calling a model.
"""
from __future__ import annotations

import pytest

from app import db, learning, pipeline, relevance
from app.llm.base import Classification, ExtractedTask

TOPIC = "CO-OP"
BARE_PRIOR = 0.4013          # sigmoid(PRIOR_BIAS): what R is when nothing fires


def _email(eid, subject, body, **over):
    row = dict(id=eid, conversation_id=None, subject=subject, from_name="Career Centre",
               from_address="coop@uni.edu", to_recipients='["me@x.com"]', cc_recipients="[]",
               received_at="2026-09-17T09:00:00+00:00", is_read=1, is_answered=0,
               is_flagged=0, has_attachments=0, importance="normal", web_link="",
               folder="INBOX", body_preview=body[:120], body_text=body, body_html="",
               synced_at=db.now_iso())
    row.update(over)
    return row


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.set_setting("user_address", "me@x.com")
    with db.connect() as conn:
        conn.execute("INSERT INTO priorities (topic, status, weight, source, created_at) "
                     "VALUES (?, 'active', 20, 'user', ?)", (TOPIC, db.now_iso()))
        conn.commit()
    learning.seed_centroids()
    db.upsert_emails([
        _email("c1", f"Action required: confirm your {TOPIC} placement", "Confirm in writing."),
        _email("c5", "Library opening hours", "The library opens at 10:00.",
               from_address="library@uni.edu", from_name="Library"),
    ])
    return tmp_path


def _persist(matched_for_c1):
    rows = {e["id"]: e for e in db.get_emails(["c1", "c5"])}
    pipeline._persist(
        [Classification(email_id="c1", bucket="action", deadline="2026-09-22",
                        matched=list(matched_for_c1),
                        tasks=[ExtractedTask(title="Confirm the placement", due_date="2026-09-22")]),
         Classification(email_id="c5", bucket="fyi", matched=[])],
        rows, source="llm", model="test")


def _row(eid):
    with db.connect() as conn:
        return dict(conn.execute(
            "SELECT * FROM classifications WHERE email_id=?", (eid,)).fetchone())


def test_a_declared_topic_reaches_relevance_through_the_keyword_path(store):
    """The model contributes nothing here: `matched` is empty on purpose. The
    keyword floor alone has to carry the declared topic, because the default
    provider is `none` and that is the configuration most users start in."""
    _persist([])
    c1 = _row("c1")
    assert c1["relevance"] > BARE_PRIOR + 0.3, c1["relevance"]
    assert c1["reason_code"] == "topic", c1["reason_code"]


def test_relevance_is_not_the_bare_prior_for_every_email(store):
    """Three emails with identical relevance is the signature of a dead
    feature, not of three equally relevant emails. That is how the real bug
    announced itself: 0.4013 across the batch, to four decimal places."""
    _persist(["CO-OP"])
    assert _row("c1")["relevance"] != _row("c5")["relevance"]


def test_deleting_the_priority_lowers_relevance_and_calls_no_model(store, monkeypatch):
    """The case the whole design exists for -- 'a professor matters less once
    the course ends' -- and the one that must never quietly no-op."""
    _persist(["CO-OP"])
    before = _row("c1")["relevance"]

    def explode(*a, **k):                       # noqa: ANN001
        raise AssertionError("rescore_all called the provider; it must be free")
    monkeypatch.setattr("app.llm.registry.get_provider", explode)

    with db.connect() as conn:
        conn.execute("DELETE FROM priorities WHERE topic = ?", (TOPIC,))
        conn.commit()
    pipeline.rescore_all()

    after = _row("c1")["relevance"]
    assert after < before, f"{before} -> {after}: deleting the priority changed nothing"
    assert after == pytest.approx(BARE_PRIOR, abs=1e-3), after


def test_the_stored_model_opinion_cannot_resurrect_a_deleted_topic(store):
    """`matched` is stored on the classification, so a rescore that trusted it
    would keep ranking mail for a topic the user removed -- the keyword side
    stopping while the stored side carried on."""
    _persist(["CO-OP"])
    with db.connect() as conn:
        conn.execute("DELETE FROM priorities WHERE topic = ?", (TOPIC,))
        conn.commit()
    pipeline.rescore_all()
    assert _row("c1")["reason_code"] != "topic"
