"""Learning that a KIND of mail matters, not just a sender or a keyword.

The gap this closes, in the user's own words: exam notices matter to him and
hackathon invitations do not — and both arrive from the same departmental
address, with none of the same words. Neither the sender feature nor the topic
feature can express that; only a category can.
"""
from __future__ import annotations

import pytest

from app import db, learning, relevance
from app.llm.base import Classification, parse_classifications


def test_a_category_becomes_one_more_feature_and_nothing_else_changes():
    """Gmail's Priority Inbox scores sum(f*g) + sum(f*w) — a global prior plus
    a per-user deviation. A per-category preference is just one more w, so this
    composes with PA-II with no architectural change at all."""
    plain = relevance.features_for({}, 0.5)
    tagged = relevance.features_for({}, 0.5, category="exam")
    assert set(tagged) - set(plain) == {"cat_exam"}
    assert tagged["cat_exam"] == 1.0
    for key in plain:
        assert tagged[key] == plain[key], key


def test_an_unknown_category_contributes_nothing_rather_than_a_default():
    """A wrong category learned confidently is worse than no category: the
    user's corrections would attach to the wrong drawer, invisibly."""
    assert relevance.category_features("quidditch") == {}
    assert relevance.category_features(None) == {}
    assert relevance.category_features("") == {}
    assert set(relevance.features_for({}, 0.0, category="nonsense")) == \
           set(relevance.features_for({}, 0.0))


def test_the_parser_refuses_a_category_outside_the_taxonomy():
    raw = ('[{"id": "a", "bucket": "fyi", "matched": [], "category": "exam"},'
           ' {"id": "b", "bucket": "fyi", "matched": [], "category": "astrology"}]')
    out = {c.email_id: c.category for c in parse_classifications(raw, ["a", "b"])}
    assert out == {"a": "exam", "b": ""}


def test_two_categories_from_one_sender_can_diverge():
    """The whole point. Same address, same features except the category, and
    the model must be able to hold opposite opinions about them."""
    email = {"subject": "x", "from_address": "dept@uni.edu", "is_read": 1}
    weights = {"cat_exam": 0.9, "cat_competition": -1.4}
    exam = relevance.relevance(relevance.features_for(email, 0.0, category="exam"), weights)
    comp = relevance.relevance(relevance.features_for(email, 0.0, category="competition"), weights)
    assert exam > comp + 0.25, (exam, comp)


# ---------------------------------------------------------------- learning rate

def test_a_brand_new_feature_moves_at_full_speed():
    """The cold-start mechanism, and the answer to 'a handful of clicks should
    visibly change things'. Google's FTRL paper makes the case with a coin
    analogy: a single global step size decreases for a coin even when it is not
    being flipped, which is wrong — a feature you have barely observed should
    move further per observation."""
    assert relevance.learning_rate(0) == 1.0
    assert relevance.learning_rate(1) < 1.0
    assert relevance.learning_rate(5) < relevance.learning_rate(1)
    assert relevance.learning_rate(500) == relevance.learning_rate(45) == pytest.approx(0.35)


def test_the_step_is_never_larger_than_pa_ii_sanctioned():
    """This can only damp a step, never amplify one, so PA-II's relative-loss
    bound stays an upper bound. The interactive-ML literature is explicit that
    effective updates are immediate and small, not large — cranking the step up
    to feel responsive is the thing the evidence says not to do."""
    assert all(relevance.learning_rate(n) <= 1.0 for n in range(0, 200))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()
    learning.begin_epoch()
    return tmp_path


def _signal(email_id, target, kind="explicit_correction"):
    return relevance.Signal(email_id=email_id, kind=kind, target=target,
                            at=db.now_iso())


def test_the_first_correction_on_a_new_category_moves_it_further(store):
    """Same gesture, same features, different history: the category nobody has
    corrected before must move further than one corrected twenty times."""
    feats = relevance.features_for({"is_read": 1}, 0.0, category="competition")

    fresh = db.ranking_weights()
    learning.learn([_signal("e1", 0.0)], {"e1": dict(feats)})
    moved_when_new = abs(db.ranking_weights().get("cat_competition", 0.0)
                         - fresh.get("cat_competition", 0.0))

    with db.connect() as conn:
        conn.execute("UPDATE ranking_weights SET count = 40 WHERE name = 'cat_competition'")
        conn.commit()
    before = db.ranking_weights().get("cat_competition", 0.0)
    learning.learn([_signal("e2", 0.0)], {"e2": dict(feats)})
    moved_when_mature = abs(db.ranking_weights().get("cat_competition", 0.0) - before)

    assert moved_when_new > moved_when_mature * 1.5, (moved_when_new, moved_when_mature)


def test_the_counter_advances_only_for_features_that_actually_moved(store):
    feats = relevance.features_for({"is_read": 1}, 0.0, category="exam")
    learning.learn([_signal("e1", 1.0)], {"e1": dict(feats)})
    counts = db.weight_counts()
    assert counts.get("cat_exam", 0) == 1
    # A feature that was zero in the vector contributes no gradient, so it must
    # not be charged an observation it never had.
    assert counts.get("cat_career", 0) == 0


def test_the_evidence_is_reportable_not_just_learnable(store):
    """'You have marked six of eight competition emails not relevant' is a
    sentence the user can argue with. A weight of -1.4023 is not."""
    with db.connect() as conn:
        for i in range(8):
            conn.execute("INSERT INTO classifications (email_id, bucket, category, source) "
                         "VALUES (?, 'fyi', 'competition', 'llm')", (f"c{i}",))
        for i in range(6):
            conn.execute("INSERT INTO feedback (email_id, verdict, created_at) "
                         "VALUES (?, 'not_relevant', ?)", (f"c{i}", db.now_iso()))
        conn.commit()
    rows = {r["category"]: r for r in db.category_evidence()}
    assert rows["competition"]["total"] == 8
    assert rows["competition"]["dismissed"] == 6
    assert "signals" in rows["competition"]


def test_not_relevant_is_a_verdict_the_app_accepts_and_learns_from(store):
    assert "not_relevant" in db.VALID_VERDICTS
    assert "not_relevant" in learning.VERDICT_SIGNALS
    kind, target = learning.VERDICT_SIGNALS["not_relevant"]
    assert kind == "explicit_correction", "an explicit gesture must outrank an inferred one"
    assert target == 0.0
