"""The pure half of adaptive ranking: two axes, a movable boundary, and a
learner that refuses more often than it accepts.

Every test here states a claim from the design and checks it against behaviour
rather than against an implementation detail. Where a test asserts a bound over
a collection it first asserts the collection is non-empty -- a vacuous pass and
a real pass look identical in a green run, and that has already cost this
project once.
"""
from __future__ import annotations

from datetime import date

import pytest

from app import relevance as R
from app.relevance import Axes, Signal, Thresholds


def mail(**over):
    base = dict(
        id="e1", subject="Project update", from_address="lee@uni.edu", from_name="Lee",
        to_recipients="[]", cc_recipients="[]", body_text="Some body text.",
        is_read=0, is_answered=0, is_flagged=0, has_attachments=0,
    )
    base.update(over)
    return base


# --------------------------------------------------------------------------
# the two axes exist and are independent
# --------------------------------------------------------------------------

def test_actionability_ignores_the_user_entirely():
    """A(e) describes the text. The same email must score the same whoever is
    reading it -- that independence is the whole reason it can be frozen while
    relevance moves."""
    email = mail(subject="Action required: sign the form")
    first = R.actionability(email, model_bucket="action")
    second = R.actionability(email, model_bucket="action")
    assert first == second
    assert first > 0.5, "an explicit ask should read as actionable"


def test_actionability_rises_with_each_independent_cue():
    plain = R.actionability(mail(subject="Notes from Tuesday"))
    cued = R.actionability(mail(subject="Action required: submit by Friday"))
    dated = R.actionability(mail(subject="Action required: submit by Friday"), deadline="2026-09-20")
    tasked = R.actionability(mail(subject="Action required: submit by Friday"),
                             deadline="2026-09-20", task_count=2)
    ladder = [plain, cued, dated, tasked]
    assert ladder == sorted(ladder), f"cues must be monotone, got {ladder}"
    assert plain < 0.5 < tasked


def test_relevance_on_day_one_is_the_declared_list_and_nothing_else():
    """Cold start: no learned weights, so R is exactly the priors. A matched
    topic ranks; an unmatched one is merely unremarkable, not noise."""
    matched = R.relevance(R.features_for(mail(), topic_match=1.0), weights={})
    unmatched = R.relevance(R.features_for(mail(), topic_match=0.0), weights={})
    assert matched > 0.8, "a declared priority must dominate on day one"
    assert R.DEFAULT_THETA_REL < unmatched < 0.5, (
        "an email that matches nothing is not noise -- noise needs evidence")


def test_structural_noise_alone_drops_below_the_relevance_floor():
    junk = mail(from_address="noreply@newsletter.com", subject="Weekly round-up: 40% off")
    score = R.relevance(R.features_for(junk, topic_match=0.0), weights={})
    assert score < R.DEFAULT_THETA_REL
    assert R.derive_bucket(R.axes_for(junk), Thresholds()) == "noise"


# --------------------------------------------------------------------------
# the boundary is an object that can be moved
# --------------------------------------------------------------------------

def test_the_same_email_changes_bucket_when_only_the_threshold_moves():
    """The claim the whole redesign rests on: a category has no knob, a
    threshold does. Nothing about the email or the model changes here."""
    email = mail(subject="Please confirm attendance")
    axes = R.axes_for(email, topic_match=1.0, model_bucket="action")
    assert 0.1 < axes.actionability < 0.99, (
        f"need a mid-range A for this test to mean anything, got {axes.actionability}")

    strict = Thresholds(relevance=0.35, action=min(0.95, axes.actionability + 0.05))
    loose = Thresholds(relevance=0.35, action=max(0.05, axes.actionability - 0.05))
    assert axes.bucket(strict) == "fyi"
    assert axes.bucket(loose) == "action"


def test_relevance_gates_before_actionability():
    """A loud ask from someone irrelevant is still noise. Ordering matters: a
    newsletter shouting ACTION REQUIRED must not reach the action list."""
    axes = Axes(actionability=0.99, relevance=0.10, features={})
    assert axes.bucket(Thresholds()) == "noise"


def test_threshold_moves_only_on_a_consistent_run():
    start = Thresholds(action=0.55)
    mixed = [("promote", 0.4), ("demote", 0.7), ("promote", 0.3)]
    unchanged, reason = R.adjust_threshold(start, mixed, today=date(2026, 9, 15))
    assert unchanged.action == start.action
    assert reason == "not-consistent"

    run = [("promote", 0.40), ("promote", 0.38), ("promote", 0.44)]
    moved, reason = R.adjust_threshold(start, run, today=date(2026, 9, 15))
    assert reason == "moved"
    assert moved.action < start.action, "promoting things below the line lowers the line"


def test_promotions_already_above_the_line_teach_nothing_about_it():
    start = Thresholds(action=0.55)
    above = [("promote", 0.80), ("promote", 0.91), ("promote", 0.77)]
    result, reason = R.adjust_threshold(start, above, today=date(2026, 9, 15))
    assert reason == "already-above"
    assert result.action == start.action


def test_one_bad_morning_cannot_walk_the_threshold_anywhere():
    """The daily cap. Without it the consistent-direction rule is a lever with
    no stop, and a single session of clicking resets the system."""
    thresholds = Thresholds(action=0.55)
    run = [("promote", 0.10), ("promote", 0.10), ("promote", 0.10)]
    reasons = []
    for _ in range(40):
        thresholds, reason = R.adjust_threshold(thresholds, run, today=date(2026, 9, 15))
        reasons.append(reason)
    assert "daily-cap" in reasons, "the cap must actually fire"
    assert 0.55 - thresholds.action <= R.THRESHOLD_DAILY_CAP + 1e-9


def test_threshold_solves_from_a_number_the_user_actually_knows():
    scores = [0.95, 0.90, 0.80, 0.60, 0.40, 0.20, 0.10]
    theta = R.threshold_for_volume(scores, target_per_day=3)
    assert theta is not None
    kept = [s for s in scores if s >= theta]
    assert len(kept) == 3, f"asked for 3, theta={theta} kept {kept}"


def test_threshold_for_volume_refuses_an_empty_history():
    assert R.threshold_for_volume([], target_per_day=5) is None
    assert R.threshold_for_volume([0.4], target_per_day=0) is None


# --------------------------------------------------------------------------
# learning, and everything that stops it
# --------------------------------------------------------------------------

EPOCH = "2026-09-01T00:00:00+00:00"


def sig(**over):
    base = dict(email_id="e1", kind="explicit_correction", target=1.0,
                at="2026-09-10T09:00:00+00:00", sender="lee@uni.edu")
    base.update(over)
    return Signal(**base)


def test_nothing_is_learned_before_the_epoch():
    """The contamination answer. Weeks of QA clicking sit before this line and
    must move nothing, and the refusal names itself rather than passing
    silently."""
    weights = {}
    feats = R.features_for(mail(), topic_match=1.0)
    outcome = R.apply_signal(sig(at="2026-08-01T09:00:00+00:00"), feats, weights,
                             epoch_start=EPOCH)
    assert outcome.applied is False
    assert outcome.reason == "pre-epoch"
    assert weights == {}


def test_learning_is_off_until_an_epoch_is_set_by_hand():
    """Opt-in, not opt-out. A default of 'learn from whatever is already in the
    tables' would have baked the test phase in permanently on first run."""
    weights = {}
    outcome = R.apply_signal(sig(), R.features_for(mail(), topic_match=1.0), weights,
                             epoch_start="")
    assert outcome.applied is False and outcome.reason == "pre-epoch"
    assert weights == {}


def test_a_real_correction_moves_weights_in_the_right_direction():
    weights = {}
    junk = mail(from_address="noreply@news.com", subject="Weekly round-up")
    feats = R.features_for(junk, topic_match=0.0)
    before = R.relevance(feats, weights)
    outcome = R.apply_signal(sig(target=1.0), feats, weights, epoch_start=EPOCH)
    assert outcome.applied, outcome.reason
    assert outcome.deltas, "an applied update with no deltas is not an update"
    after = R.relevance(feats, weights)
    assert after > before, "pinning something suppressed must raise its relevance"


def test_a_model_that_is_already_right_stops_moving():
    """The hinge. Without it ordinary agreement erodes a working ranking one
    tiny update at a time."""
    weights = {}
    feats = R.features_for(mail(), topic_match=1.0)
    predicted = R.relevance(feats, weights)
    outcome = R.apply_signal(sig(target=round(predicted, 4)), feats, weights, epoch_start=EPOCH)
    assert outcome.applied is False
    assert outcome.reason == "within-hinge"
    assert weights == {}


def test_a_burst_teaches_nothing():
    """Twelve actions in forty seconds across twelve senders is someone
    checking the buttons work."""
    signals = [sig(email_id=f"e{i}", at=f"2026-09-10T09:00:{i * 3:02d}+00:00",
                   sender=f"p{i}@x.com") for i in range(12)]
    marked = R.detect_bursts(signals)
    assert marked, "detect_bursts must not swallow its input"
    assert all(s.burst_index for s in marked), "all twelve are one burst"

    weights = {}
    feats = R.features_for(mail(), topic_match=0.0)
    for s in marked:
        outcome = R.apply_signal(s, feats, weights, epoch_start=EPOCH)
        assert outcome.applied is False and outcome.reason == "burst"
    assert weights == {}


def test_deliberate_actions_spread_over_time_are_not_a_burst():
    """The guard has to be falsifiable in the other direction too, or it is
    just a switch that turns learning off."""
    signals = [sig(email_id=f"e{i}", at=f"2026-09-10T09:{i * 5:02d}:00+00:00",
                   sender=f"p{i}@x.com") for i in range(6)]
    marked = R.detect_bursts(signals)
    assert len(marked) == 6
    assert not any(s.burst_index for s in marked)


def test_the_same_action_on_one_correspondent_is_not_a_burst():
    """Working through one noisy sender is triage, not testing."""
    signals = [sig(email_id=f"e{i}", at=f"2026-09-10T09:00:{i * 3:02d}+00:00",
                   sender="news@shop.com") for i in range(12)]
    assert not any(s.burst_index for s in R.detect_bursts(signals))


def test_a_glance_teaches_less_than_a_correction_and_a_flick_teaches_nothing():
    corr, _ = R.confidence_of(sig(kind="explicit_correction"))
    read, _ = R.confidence_of(sig(kind="opened_dwelled", dwell_ms=9000))
    flick, reason = R.confidence_of(sig(kind="opened_dwelled", dwell_ms=200))
    assert corr > read > 0
    assert flick == 0.0 and reason == "dwell-below-floor"


def test_confidence_scales_the_size_of_the_update_not_just_its_admission():
    """`C` is the mechanism that makes contamination survivable rather than
    fatal: a weak signal is allowed in and moves almost nothing."""
    feats = R.features_for(mail(from_address="noreply@news.com"), topic_match=0.0)
    strong, weak = {}, {}
    R.apply_signal(sig(kind="explicit_correction", target=1.0), feats, strong, epoch_start=EPOCH)
    R.apply_signal(sig(kind="opened_dwelled", target=1.0, dwell_ms=9000), feats, weak,
                   epoch_start=EPOCH)
    assert strong and weak, "both should apply; only the magnitudes differ"
    assert sum(abs(v) for v in strong.values()) > sum(abs(v) for v in weak.values())


def test_an_unknown_gesture_is_refused_rather_than_guessed():
    weights = {}
    outcome = R.apply_signal(sig(kind="invented_by_a_future_feature"),
                             R.features_for(mail(), topic_match=0.0), weights,
                             epoch_start=EPOCH)
    assert outcome.applied is False and outcome.reason == "unknown-kind"
    assert weights == {}


def test_no_single_email_may_swing_a_weight_arbitrarily_far():
    weights = {}
    feats = R.features_for(mail(), topic_match=1.0)
    R.apply_signal(sig(target=1.0), feats, weights, epoch_start=EPOCH)
    assert weights
    assert all(abs(v) <= R.PA_MAX_STEP + 1e-9 for v in weights.values())


def test_repeated_corrections_converge_rather_than_diverge():
    """Online learning that oscillates is worse than none. Twenty consistent
    corrections should approach the target, not overshoot past it."""
    weights = {}
    junk = mail(from_address="noreply@news.com", subject="Weekly round-up")
    feats = R.features_for(junk, topic_match=0.0)
    trail = []
    for i in range(20):
        R.apply_signal(sig(email_id=f"e{i}", target=1.0), feats, weights, epoch_start=EPOCH)
        trail.append(R.relevance(feats, weights))
    assert len(trail) == 20
    assert trail[-1] > trail[0], "it has to actually learn"
    assert trail[-1] <= 1.0
    assert all(b >= a - 1e-6 for a, b in zip(trail, trail[1:])), f"non-monotone: {trail}"


# --------------------------------------------------------------------------
# exploration
# --------------------------------------------------------------------------

def test_exploration_is_deterministic_so_a_row_does_not_flicker():
    ids = [f"m{i}" for i in range(200)]
    first = [R.is_explored(i) for i in ids]
    second = [R.is_explored(i) for i in ids]
    assert first == second
    assert any(first), "a rate of zero would make the mechanism a no-op"


def test_exploration_rate_is_close_to_the_configured_fraction():
    ids = [f"msg-{i}" for i in range(4000)]
    rate = R.exploration_rate(ids, one_in=20)
    assert 0.03 < rate < 0.07, f"expected ~5%, got {rate:.3f}"


def test_exploration_can_be_switched_off():
    assert R.is_explored("m1", one_in=0) is False


# --------------------------------------------------------------------------
# centroids and drift
# --------------------------------------------------------------------------

def test_embedding_is_deterministic_and_unit_length():
    a, b = R.embed("quarterly planning review"), R.embed("quarterly planning review")
    assert a == b
    assert abs(sum(v * v for v in a) ** 0.5 - 1.0) < 1e-6


def test_related_text_is_nearer_than_unrelated_text():
    topic = R.embed("thesis defence committee")
    near = R.embed("scheduling the thesis defence with the committee")
    far = R.embed("40% off winter boots this weekend only")
    assert R.cosine(topic, near) > R.cosine(topic, far)


def test_rocchio_pulls_toward_engagement_and_pushes_from_rejection():
    centroid = R.embed("course assignments")
    engaged = [R.embed("assignment three feedback"), R.embed("assignment four rubric")]
    rejected = [R.embed("campus gym membership sale")]
    moved = R.rocchio(centroid, engaged, rejected)
    assert R.cosine(moved, engaged[0]) > R.cosine(centroid, engaged[0])
    assert R.cosine(moved, rejected[0]) <= R.cosine(centroid, rejected[0]) + 1e-9


def test_a_topic_that_stops_being_fed_drifts_away_from_its_old_mail():
    """'The professor matters less once the course ends' -- resolved by the
    centroid moving, with the user editing nothing."""
    centroid = R.embed("stats course")
    old_mail = R.embed("stats course problem set due friday")
    before = R.cosine(centroid, old_mail)
    for _ in range(6):
        centroid = R.rocchio(centroid, [R.embed("internship interview scheduling")], [])
    assert R.cosine(centroid, old_mail) < before


def test_topic_match_keeps_working_with_no_centroids_at_all():
    """If the embedder is unavailable the list must still sort by what the user
    declared, rather than collapsing every relevance to zero."""
    score, matched = R.topic_match(mail(), centroids={}, keyword_hits=["thesis"])
    assert score > 0.8 and matched == ["thesis"]


def test_topic_match_is_zero_when_nothing_matches():
    score, matched = R.topic_match(mail(subject="lunch?"), centroids={}, keyword_hits=[])
    assert score == 0.0 and matched == []


# --------------------------------------------------------------------------
# explainability
# --------------------------------------------------------------------------

def test_a_score_can_name_what_produced_it():
    """Provenance as precondition: a ranking nobody can interrogate is the same
    failure as a claim with no sources, wearing a number."""
    axes = R.axes_for(mail(), topic_match=1.0, model_bucket="action")
    out = R.explain(axes, Thresholds(), weights={"direct_address": 0.4})
    assert out["bucket"] in {"action", "fyi", "noise"}
    assert out["contributions"], "an explanation with no contributions explains nothing"
    assert "topic_match" in out["contributions"]
    assert out["contributions"]["topic_match"] > 0


# --------------------------------------------------------------------------
# exploration as a quota (regression: the audit found a seed giving zero)
# --------------------------------------------------------------------------

def test_a_small_suppressed_population_is_never_left_unexplored():
    """Per-email sampling gave zero explored messages over a 74-message
    suppressed set on a real seed, while the bulk rate looked perfect. The
    promise is a proportion of that set, so it is a quota."""
    ids = [f"m{i:04d}" for i in range(74)]
    chosen = R.select_explored(ids, one_in=20)
    assert 0 < len(chosen) <= 8, f"expected ~4, got {len(chosen)}"
    assert chosen <= set(ids)


def test_the_quota_is_stable_between_refreshes():
    ids = [f"m{i}" for i in range(200)]
    assert R.select_explored(ids, 20) == R.select_explored(ids, 20)


def test_the_quota_scales_with_the_population():
    small = R.select_explored([f"a{i}" for i in range(20)], 20)
    large = R.select_explored([f"a{i}" for i in range(400)], 20)
    assert len(small) == 1
    assert 15 <= len(large) <= 25


def test_an_empty_population_explores_nothing():
    assert R.select_explored([], 20) == set()
    assert R.select_explored(["a"], 0) == set()


def test_demotion_is_learned_more_slowly_than_promotion():
    """Suppression is self-concealing -- a hidden email is never shown, so it is
    never corrected. The asymmetry is the same reasoning as Rocchio's."""
    feats = R.features_for(mail(), topic_match=0.5)
    predicted = R.relevance(feats, {})
    # Equal error magnitude in both directions, or this compares |error| rather
    # than the asymmetry it claims to be about.
    delta = 0.3
    assert delta < predicted < 1 - delta, f"need headroom both ways, got {predicted}"

    up, down = {}, {}
    R.apply_signal(sig(target=predicted + delta), feats, up, epoch_start=EPOCH)
    R.apply_signal(sig(target=predicted - delta), feats, down, epoch_start=EPOCH)
    assert up and down, "both directions must apply, or the comparison is empty"

    moved_up = sum(abs(v) for v in up.values())
    moved_down = sum(abs(v) for v in down.values())
    # Directional, and a scaling rather than a shutoff. The exact ratio is left
    # unasserted on purpose: it also passes through the per-email step clamp,
    # and pinning it here would make PA_MAX_STEP untunable without a red test
    # that is not about what this test claims.
    assert moved_down < moved_up
    assert moved_down > moved_up * 0.5
