"""The stateful half: what actually gets written, and what refuses to be.

These are the tests the pure module cannot carry -- that a refusal reaches the
audit trail, that thresholds survive a restart, and that the pipeline stores
both axes rather than only the bucket they produced.
"""
from __future__ import annotations

from datetime import date

import pytest

from app import db, learning, priority, relevance
from app.relevance import Signal, Thresholds


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    rows = [
        dict(id="junk1", conversation_id=None, subject="Weekly round-up: 40% off",
             from_name="ShopCo", from_address="noreply@shop.com", to_recipients="[]",
             cc_recipients="[]", received_at="2026-09-10T09:00:00+00:00", is_read=1,
             is_answered=0, is_flagged=0, has_attachments=0, importance="normal",
             web_link="", folder="INBOX", body_preview="", body_text="Sale ends soon",
             body_html="", synced_at=db.now_iso()),
        dict(id="real1", conversation_id=None, subject="Thesis defence: confirm the date",
             from_name="Lee", from_address="lee@uni.edu", to_recipients='["me@x.com"]',
             cc_recipients="[]", received_at="2026-09-14T09:00:00+00:00", is_read=0,
             is_answered=0, is_flagged=0, has_attachments=0, importance="normal",
             web_link="", folder="INBOX", body_preview="",
             body_text="Please confirm by 2026-09-20.", body_html="", synced_at=db.now_iso()),
    ]
    db.upsert_emails(rows)
    db.set_setting("user_address", "me@x.com")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO priorities (topic, status, weight, source, created_at) "
            "VALUES ('thesis', 'active', 20, 'user', ?)", (db.now_iso(),))
        conn.commit()
    return tmp_path


def emails(store, ids):
    return {e["id"]: e for e in db.get_emails(ids)}


# --------------------------------------------------------------------------
# the epoch
# --------------------------------------------------------------------------

def test_learning_is_off_out_of_the_box(store):
    assert learning.epoch_start() == "", "a fresh install must not learn from QA rows"


def test_every_signal_before_the_epoch_is_refused_and_recorded(store):
    learning.begin_epoch("2026-09-12T00:00:00+00:00")
    feats = {"real1": relevance.features_for(emails(store, ["real1"])["real1"], topic_match=1.0)}
    signals = [Signal("real1", "explicit_correction", 1.0, "2026-09-01T09:00:00+00:00")]

    result = learning.learn(signals, feats)
    assert result["applied"] == 0
    assert result["refused"] == {"pre-epoch": 1}
    assert db.ranking_weights() == {}

    events = db.learning_events()
    assert len(events) == 1, "a refusal that leaves no trace is indistinguishable from a bug"
    assert events[0]["applied"] == 0 and events[0]["reason"] == "pre-epoch"


def test_after_the_epoch_the_same_signal_teaches(store):
    learning.begin_epoch("2026-09-01T00:00:00+00:00")
    junk = emails(store, ["junk1"])["junk1"]
    feats = {"junk1": relevance.features_for(junk, topic_match=0.0)}
    before = relevance.relevance(feats["junk1"], db.ranking_weights())

    result = learning.learn(
        [Signal("junk1", "explicit_correction", 1.0, "2026-09-10T09:00:00+00:00")], feats)
    assert result["applied"] == 1, result["refused"]
    after = relevance.relevance(feats["junk1"], db.ranking_weights())
    assert after > before


def test_the_epoch_is_the_only_difference_between_those_two_runs(store):
    """Stated as its own test because it is the claim Danny's correction turns
    on: the same rows are worthless before the line and useful after it."""
    junk = emails(store, ["junk1"])["junk1"]
    feats = {"junk1": relevance.features_for(junk, topic_match=0.0)}
    signal = [Signal("junk1", "explicit_correction", 1.0, "2026-09-10T09:00:00+00:00")]

    learning.begin_epoch("2026-09-20T00:00:00+00:00")
    assert learning.learn(signal, feats)["applied"] == 0
    weights_after_refusal = db.ranking_weights()

    learning.begin_epoch("2026-09-01T00:00:00+00:00")
    assert learning.learn(signal, feats)["applied"] == 1
    assert db.ranking_weights() != weights_after_refusal


# --------------------------------------------------------------------------
# the audit trail
# --------------------------------------------------------------------------

def test_the_summary_says_why_nothing_is_being_learned(store):
    """The diagnosis the user needs when the app 'is not adapting'. Silence
    cannot produce it; a reason code with a big number next to it can."""
    learning.begin_epoch("2026-09-20T00:00:00+00:00")
    feats = {"real1": relevance.features_for(emails(store, ["real1"])["real1"], topic_match=1.0)}
    learning.learn(
        [Signal("real1", "explicit_correction", 1.0, f"2026-09-1{i}T09:00:00+00:00")
         for i in range(5)], feats)

    summary = db.learning_summary()
    assert summary["applied"] == 0
    assert summary["refused"] > 0
    assert summary["refused_by_reason"], "a refusal count with no reasons is not a diagnosis"
    assert "pre-epoch" in summary["refused_by_reason"]


def test_applied_events_carry_the_deltas_that_were_written(store):
    learning.begin_epoch("2026-09-01T00:00:00+00:00")
    junk = emails(store, ["junk1"])["junk1"]
    feats = {"junk1": relevance.features_for(junk, topic_match=0.0)}
    learning.learn([Signal("junk1", "explicit_correction", 1.0, "2026-09-10T09:00:00+00:00")], feats)

    applied = db.learning_events(applied_only=True)
    assert applied, "nothing applied -- the rest of this test would pass vacuously"
    assert applied[0]["deltas"], "an applied event with no deltas is a lie"
    assert set(applied[0]["deltas"]) <= set(relevance.FEATURE_NAMES)


def test_resetting_weights_returns_to_the_declared_priors(store):
    """The undo. It is what makes switching learning on a reversible decision
    rather than a one-way door."""
    learning.begin_epoch("2026-09-01T00:00:00+00:00")
    junk = emails(store, ["junk1"])["junk1"]
    feats = {"junk1": relevance.features_for(junk, topic_match=0.0)}
    baseline = relevance.relevance(feats["junk1"], {})
    learning.learn([Signal("junk1", "explicit_correction", 1.0, "2026-09-10T09:00:00+00:00")], feats)
    assert db.ranking_weights() != {}

    db.reset_ranking_weights()
    assert db.ranking_weights() == {}
    assert relevance.relevance(feats["junk1"], db.ranking_weights()) == baseline


# --------------------------------------------------------------------------
# thresholds
# --------------------------------------------------------------------------

def test_thresholds_survive_a_restart(store):
    learning.save_thresholds(Thresholds(relevance=0.42, action=0.66))
    loaded = learning.load_thresholds()
    assert loaded.relevance == pytest.approx(0.42)
    assert loaded.action == pytest.approx(0.66)


def test_the_volume_control_sets_the_boundary(store):
    scores = [0.95, 0.90, 0.80, 0.60, 0.40, 0.20, 0.10]
    updated = learning.set_action_volume(2, scores)
    kept = [s for s in scores if s >= updated.action]
    assert len(kept) == 2
    assert db.get_setting("target_action_volume") == "2"


def test_nudging_records_its_reason_even_when_it_declines(store):
    _, reason = learning.nudge_threshold([("promote", 0.4)], today=date(2026, 9, 15))
    assert reason == "too-few"
    before = learning.load_thresholds().action
    moved, reason = learning.nudge_threshold(
        [("promote", 0.40), ("promote", 0.38), ("promote", 0.44)], today=date(2026, 9, 15))
    assert reason == "moved" and moved.action < before


# --------------------------------------------------------------------------
# centroids
# --------------------------------------------------------------------------

def test_a_topic_is_seeded_from_the_words_the_user_typed(store):
    assert learning.seed_centroids() == 1
    centroids = db.priority_centroids(learning.EMBEDDER_NAME)
    assert "thesis" in centroids
    assert learning.seed_centroids() == 0, "seeding must be idempotent"


def test_drift_refuses_when_there_is_nothing_to_learn_from(store):
    learning.seed_centroids()
    assert learning.drift_centroid("thesis") is False
    assert learning.drift_centroid("thesis", engaged_texts=["defence committee date"]) is True


def test_centroids_from_a_different_embedder_are_not_compared(store):
    """A cosine between vectors from two models is a number with no meaning.
    Filtering by embedder is what stops a model swap producing confident
    nonsense instead of an obvious failure."""
    db.save_priority_centroid("thesis", [0.0, 1.0], embedder="some-future-model")
    assert db.priority_centroids(learning.EMBEDDER_NAME) == {}
    assert "thesis" in db.priority_centroids("some-future-model")


# --------------------------------------------------------------------------
# exploration
# --------------------------------------------------------------------------

def test_only_suppressed_mail_is_ever_explored(store):
    assert learning.should_explore("junk1", "action") is False
    assert learning.should_explore("junk1", "fyi") is False


def test_exploration_honours_the_setting(store):
    db.set_setting("explore_one_in", "1")
    assert learning.should_explore("junk1", "noise") is True
    db.set_setting("explore_one_in", "0")
    assert learning.should_explore("junk1", "noise") is False


# --------------------------------------------------------------------------
# the pipeline stores both axes
# --------------------------------------------------------------------------

def test_classification_records_the_axes_not_just_the_bucket(store):
    from app import pipeline
    from app.llm.base import Classification

    rows = emails(store, ["junk1", "real1"])
    pipeline._persist(
        [Classification(email_id="junk1", bucket="noise", deadline=None, rationale="promo"),
         Classification(email_id="real1", bucket="action", deadline="2026-09-20",
                        rationale="asks for confirmation")],
        rows, source="structural", model="test",
    )
    with db.connect() as conn:
        stored = {r["email_id"]: dict(r) for r in
                  conn.execute("SELECT * FROM classifications").fetchall()}

    assert set(stored) == {"junk1", "real1"}, "nothing stored -- the asserts below would be vacuous"
    for rec in stored.values():
        assert rec["actionability"] is not None
        assert rec["relevance"] is not None
    assert stored["junk1"]["relevance"] < stored["real1"]["relevance"]
    assert stored["junk1"]["actionability"] < stored["real1"]["actionability"]
    assert stored["junk1"]["bucket"] == "noise"


def test_a_muted_sender_is_never_surfaced_by_exploration(store):
    """Mute is a promise, not a probability. Exploration opens the one-way door
    on the system's own guesses -- never on the user's standing decision."""
    from app import pipeline
    from app.llm.base import Classification

    db.set_setting("explore_one_in", "1")      # explore everything suppressed
    db.mute_sender("noreply@shop.com")
    rows = emails(store, ["junk1"])
    pipeline._persist(
        [Classification(email_id="junk1", bucket="noise", deadline=None, rationale="promo")],
        rows, source="structural", model="test",
    )
    with db.connect() as conn:
        rec = dict(conn.execute("SELECT * FROM classifications WHERE email_id='junk1'").fetchone())
    assert rec["explored"] == 0


def test_unmuted_noise_does_get_explored(store):
    """The other half of the previous test. Without this one, 'explored == 0'
    would pass for the wrong reason and nobody would know."""
    from app import pipeline
    from app.llm.base import Classification

    db.set_setting("explore_one_in", "1")
    rows = emails(store, ["junk1"])
    pipeline._persist(
        [Classification(email_id="junk1", bucket="noise", deadline=None, rationale="promo")],
        rows, source="structural", model="test",
    )
    with db.connect() as conn:
        rec = dict(conn.execute("SELECT * FROM classifications WHERE email_id='junk1'").fetchone())
    assert rec["explored"] == 1


def test_explain_returns_none_rather_than_inventing_an_explanation(store):
    assert learning.explain_email("no-such-email") is None


def test_explain_names_the_topics_and_the_thresholds(store):
    from app import pipeline
    from app.llm.base import Classification

    rows = emails(store, ["real1"])
    pipeline._persist(
        [Classification(email_id="real1", bucket="action", deadline="2026-09-20",
                        rationale="asks for confirmation")],
        rows, source="structural", model="test",
    )
    out = learning.explain_email("real1")
    assert out is not None
    assert out["contributions"], "an explanation with no contributions explains nothing"
    assert out["theta_action"] == pytest.approx(learning.load_thresholds().action)
    assert out["matched_topics"] == ["thesis"]


# --------------------------------------------------------------------------
# re-scoring must move the bucket, or learning is invisible
# --------------------------------------------------------------------------

def test_editing_the_priority_list_moves_an_email_between_buckets(store):
    """The claim that makes the whole layer visible: relevance is volatile, so
    a bucket assigned on arrival must not survive a change to what the user
    cares about. Before this, only the score was recomputed and an email kept
    its original bucket until the model was called again."""
    from app import pipeline
    from app.llm.base import Classification

    rows = emails(store, ["real1"])
    pipeline._persist(
        [Classification(email_id="real1", bucket="action", deadline="2026-09-20",
                        rationale="asks for confirmation")],
        rows, source="llm", model="test",
    )

    def stored():
        with db.connect() as conn:
            return dict(conn.execute(
                "SELECT * FROM classifications WHERE email_id='real1'").fetchone())

    first = stored()
    assert first["bucket"] in {"action", "fyi"}, first["bucket"]

    # Make it irrelevant: drop the only topic it matched, and raise the bar.
    with db.connect() as conn:
        conn.execute("DELETE FROM priorities WHERE topic = 'thesis'")
        conn.commit()
    learning.save_thresholds(Thresholds(relevance=0.9, action=0.55))
    pipeline.rescore_all()

    after = stored()
    assert after["bucket"] == "noise", (
        f"relevance {after['relevance']} should be below theta 0.9")
    assert after["relevance"] < first["relevance"] or first["relevance"] < 0.9


def test_rescoring_uses_what_the_model_said_not_its_own_last_answer(store):
    """A loop whose input is its previous output drifts wherever the first
    error pointed. `model_bucket` is the unrewritten input."""
    from app import pipeline
    from app.llm.base import Classification

    rows = emails(store, ["real1"])
    pipeline._persist(
        [Classification(email_id="real1", bucket="action", deadline="2026-09-20",
                        rationale="asks")],
        rows, source="llm", model="test",
    )
    with db.connect() as conn:
        rec = dict(conn.execute(
            "SELECT * FROM classifications WHERE email_id='real1'").fetchone())
    assert rec["model_bucket"] == "action", "the provider's own answer must be kept"

    # Ten round trips must not drift the actionability, because the hint it is
    # derived from never changes.
    first_a = rec["actionability"]
    for _ in range(10):
        pipeline.rescore_all()
    with db.connect() as conn:
        final = dict(conn.execute(
            "SELECT * FROM classifications WHERE email_id='real1'").fetchone())
    assert final["model_bucket"] == "action"
    assert final["actionability"] == pytest.approx(first_a)


def test_rescoring_is_cheap_enough_to_run_on_every_priority_edit(store):
    """It is called from the settings screen on every change, so it must not do
    per-email queries for per-user state."""
    import time
    from app import pipeline
    from app.llm.base import Classification

    rows = [dict(id=f"bulk{i}", conversation_id=None, subject=f"Message {i}",
                 from_name="X", from_address="x@y.com", to_recipients="[]",
                 cc_recipients="[]", received_at="2026-09-10T09:00:00+00:00",
                 is_read=0, is_answered=0, is_flagged=0, has_attachments=0,
                 importance="normal", web_link="", folder="INBOX", body_preview="",
                 body_text="body", body_html="", synced_at=db.now_iso())
            for i in range(200)]
    db.upsert_emails(rows)
    by_id = {e["id"]: e for e in db.get_emails([r["id"] for r in rows])}
    pipeline._persist(
        [Classification(email_id=i, bucket="fyi", deadline=None, rationale="")
         for i in by_id], by_id, source="structural", model="t")

    started = time.monotonic()
    updated = pipeline.rescore_all()
    elapsed = time.monotonic() - started
    assert updated >= 200
    assert elapsed < 5.0, f"200 emails took {elapsed:.2f}s"
