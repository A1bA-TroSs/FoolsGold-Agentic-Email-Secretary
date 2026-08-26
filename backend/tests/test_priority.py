"""Priority scoring is the part most likely to misbehave silently and hardest
to catch by eye, so it gets the tests."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app import priority

ME = "danny@outlook.com"
TODAY = date(2026, 8, 25)


def email(**overrides):
    base = {
        "id": "m1",
        "subject": "Hello",
        "from_name": "Alice Chen",
        "from_address": "alice@uni.edu",
        "to_recipients": json.dumps([{"name": "Danny", "address": ME}]),
        "cc_recipients": json.dumps([]),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "is_read": 1,
        "is_answered": 0,
        "is_flagged": 0,
        "has_attachments": 0,
        "importance": "normal",
        "body_text": "",
        "body_preview": "",
    }
    base.update(overrides)
    return base


def prio(topic, status="active", weight=10):
    return {"topic": topic, "status": status, "weight": weight}


# ----------------------------------------------------------------- deadlines

@pytest.mark.parametrize("text,expected", [
    ("Submit by 2026-09-01 please", "2026-09-01"),
    ("The deadline is 3 September 2026", "2026-09-03"),
    ("Due Sep 3, 2026", "2026-09-03"),
    ("Nothing dated in here at all", None),
])
def test_extract_deadline(text, expected):
    assert priority.extract_deadline(text, today=TODAY) == expected


def test_extract_deadline_weekday_is_always_in_the_future():
    # 2026-08-25 is a Tuesday; "by Friday" must mean the 28th, not last week.
    assert priority.extract_deadline("Please reply by Friday", today=TODAY) == "2026-08-28"
    # "by Tuesday" on a Tuesday means next Tuesday, never today.
    assert priority.extract_deadline("by Tuesday", today=TODAY) == "2026-09-01"


def test_bare_date_far_in_the_past_rolls_to_next_year():
    # In late August, "January 10" means next January.
    assert priority.extract_deadline("due January 10", today=TODAY) == "2027-01-10"


def test_invalid_date_does_not_raise():
    assert priority.extract_deadline("due 2026-02-30", today=TODAY) is None


# -------------------------------------------------------------- deadline pts

def test_deadline_points_decay_with_distance():
    d = lambda n: (TODAY + timedelta(days=n)).isoformat()  # noqa: E731
    assert priority.deadline_points(d(0), TODAY) > priority.deadline_points(d(1), TODAY)
    assert priority.deadline_points(d(1), TODAY) > priority.deadline_points(d(5), TODAY)
    assert priority.deadline_points(d(5), TODAY) > priority.deadline_points(d(30), TODAY)
    assert priority.deadline_points(None, TODAY) == 0.0


def test_today_outranks_overdue():
    """A deadline you can still hit matters more than one already missed."""
    overdue = priority.deadline_points((TODAY - timedelta(days=3)).isoformat(), TODAY)
    due_now = priority.deadline_points(TODAY.isoformat(), TODAY)
    assert due_now > overdue > 0


# --------------------------------------------------------------- classification

def test_noreply_sender_is_noise():
    assert priority.structural_bucket(email(from_address="no-reply@shop.com"), ME) == "noise"


def test_action_language_beats_neutral_subject():
    assert priority.structural_bucket(email(subject="Action required: sign the form"), ME) == "action"
    assert priority.structural_bucket(email(subject="Notes from Tuesday"), ME) == "fyi"


def test_deadline_in_body_still_reads_as_action():
    assert priority.structural_bucket(
        email(subject="Coursework", body_text="Please submit the final draft by Friday.")
    ) == "action"


# --------------------------------------------------------------------- scoring

def test_action_outranks_fyi_all_else_equal():
    e = email()
    action, _ = priority.score_email(e, "action", None, [], user_address=ME, today=TODAY)
    fyi, _ = priority.score_email(e, "fyi", None, [], user_address=ME, today=TODAY)
    assert action > fyi


def test_noise_is_capped_even_when_every_other_signal_fires():
    loud = email(importance="high", has_attachments=1, is_read=0)
    score, _ = priority.score_email(
        loud, "noise", TODAY.isoformat(), [prio("Thesis")],
        llm_matched=["Thesis"], user_address=ME, today=TODAY,
    )
    assert score <= priority.NOISE_CEILING


def test_active_priority_lifts_an_otherwise_ordinary_email():
    e = email(subject="Thesis chapter three feedback")
    with_prio, matched = priority.score_email(
        e, "fyi", None, [prio("thesis")], user_address=ME, today=TODAY
    )
    without, _ = priority.score_email(e, "fyi", None, [], user_address=ME, today=TODAY)
    assert with_prio > without
    assert matched == ["thesis"]


def test_dismissed_topic_pushes_an_email_down():
    e = email(subject="Weekly society social")
    demoted, _ = priority.score_email(
        e, "fyi", None, [prio("society", status="dismissed")], user_address=ME, today=TODAY
    )
    neutral, _ = priority.score_email(e, "fyi", None, [], user_address=ME, today=TODAY)
    assert demoted < neutral


def test_direct_recipient_outranks_cc_only():
    direct = email()
    cc = email(
        to_recipients=json.dumps([{"name": "List", "address": "all@uni.edu"}]),
        cc_recipients=json.dumps([{"name": "Danny", "address": ME}]),
    )
    direct_score, _ = priority.score_email(direct, "fyi", None, [], user_address=ME, today=TODAY)
    cc_score, _ = priority.score_email(cc, "fyi", None, [], user_address=ME, today=TODAY)
    assert direct_score > cc_score


def test_llm_match_only_counts_for_topics_the_user_actually_has():
    """A hallucinated topic must not silently inflate the score."""
    e = email(subject="Unrelated")
    score, matched = priority.score_email(
        e, "fyi", None, [prio("thesis")], llm_matched=["invented topic"],
        user_address=ME, today=TODAY,
    )
    baseline, _ = priority.score_email(e, "fyi", None, [prio("thesis")], user_address=ME, today=TODAY)
    assert matched == []
    assert score == baseline


def test_score_never_goes_negative():
    e = email(importance="low", is_read=1, received_at="2020-01-01T00:00:00Z")
    score, _ = priority.score_email(
        e, "noise", None, [prio("x", status="dismissed", weight=100)],
        llm_matched=[], user_address=ME, today=TODAY,
    )
    assert score >= 0


# ------------------------------------------------ engagement & staleness

def test_overdue_deadlines_decay_instead_of_pinning_forever():
    """The bug Danny saw: a print notice whose collection window closed in May
    outranked this morning's mail, because any overdue date scored a flat +26
    while the best recency bonus was +8."""
    d = lambda n: (TODAY + timedelta(days=n)).isoformat()  # noqa: E731
    just_missed = priority.deadline_points(d(-1), TODAY)
    last_week = priority.deadline_points(d(-8), TODAY)
    ancient = priority.deadline_points(d(-95), TODAY)

    assert just_missed > last_week > 0
    assert ancient == 0.0, "a deadline three months gone is history, not a priority"
    assert ancient < priority._recency_points(datetime.now(timezone.utc).isoformat())


def test_a_stale_overdue_notice_loses_to_todays_mail():
    """End-to-end version of the same thing, at the score level."""
    old_notice = email(
        subject="ITSO NetPrint Service Notification",
        from_address="no-reply@netprint.ust.hk",
        is_read=1,
        received_at=(datetime.now(timezone.utc) - timedelta(days=100)).isoformat(),
    )
    todays_mail = email(subject="Can you confirm Thursday?", is_read=0)

    stale, _ = priority.score_email(
        old_notice, "action", (TODAY - timedelta(days=95)).isoformat(), [],
        user_address=ME, today=TODAY,
    )
    fresh, _ = priority.score_email(todays_mail, "action", None, [], user_address=ME, today=TODAY)
    assert fresh > stale


def test_answering_an_email_demotes_it():
    """You replied. Whatever it asked for, you did it."""
    base = email(subject="Please confirm your slot")
    answered = email(subject="Please confirm your slot", is_answered=1)
    unanswered_score, _ = priority.score_email(base, "action", None, [], user_address=ME, today=TODAY)
    answered_score, _ = priority.score_email(answered, "action", None, [], user_address=ME, today=TODAY)
    assert answered_score < unanswered_score


def test_flagging_an_email_promotes_it():
    plain, _ = priority.score_email(email(), "fyi", None, [], user_address=ME, today=TODAY)
    starred, _ = priority.score_email(email(is_flagged=1), "fyi", None, [], user_address=ME, today=TODAY)
    assert starred > plain


def test_mail_from_people_you_write_back_to_ranks_higher():
    stranger = email(from_address="random@vendor.com")
    supervisor = email(from_address="lee@uni.edu")
    correspondents = {"lee@uni.edu": 30}
    a, _ = priority.score_email(stranger, "fyi", None, [], user_address=ME, today=TODAY,
                                correspondents=correspondents)
    b, _ = priority.score_email(supervisor, "fyi", None, [], user_address=ME, today=TODAY,
                                correspondents=correspondents)
    assert b > a


def test_affinity_is_capped_so_one_chatty_thread_cannot_dominate():
    assert priority.affinity_points("x@y.com", {"x@y.com": 5000}) == priority.AFFINITY_CAP
    assert priority.affinity_points("x@y.com", {"x@y.com": 1}) < priority.AFFINITY_CAP
    assert priority.affinity_points("x@y.com", {}) == 0.0
    assert priority.affinity_points("", {"x@y.com": 10}) == 0.0


def test_old_read_mail_that_was_never_acted_on_decays():
    old = email(is_read=1, received_at=(datetime.now(timezone.utc) - timedelta(days=90)).isoformat())
    recent = email(is_read=1)
    old_score, _ = priority.score_email(old, "fyi", None, [], user_address=ME, today=TODAY)
    new_score, _ = priority.score_email(recent, "fyi", None, [], user_address=ME, today=TODAY)
    assert old_score < new_score


def test_staleness_does_not_punish_starred_or_answered_mail():
    """Explicit engagement means it is not forgotten mail, however old."""
    old = dict(is_read=1, received_at=(datetime.now(timezone.utc) - timedelta(days=90)).isoformat())
    assert priority._stale_read_points(email(**old)) < 0
    assert priority._stale_read_points(email(is_flagged=1, **old)) == 0.0
    assert priority._stale_read_points(email(is_answered=1, **old)) == 0.0


def test_unread_mail_is_never_treated_as_stale():
    old_unread = email(is_read=0, received_at=(datetime.now(timezone.utc) - timedelta(days=200)).isoformat())
    assert priority._stale_read_points(old_unread) == 0.0


# ------------------------------------------------------------- feedback

def test_pinning_beats_every_inferred_signal():
    dull = email(subject="Weekly newsletter", from_address="news@x.com", is_read=1)
    plain, _ = priority.score_email(dull, "noise", None, [], user_address=ME, today=TODAY)
    pinned, _ = priority.score_email(dull, "noise", None, [], user_address=ME, today=TODAY,
                                     verdict="pinned")
    assert pinned > plain
    assert pinned > priority.NOISE_CEILING, "a pin must escape the noise ceiling"


def test_marking_not_important_or_done_sinks_an_email():
    urgent = email(subject="Action required: submit by Friday", is_read=0, importance="high")
    normal, _ = priority.score_email(urgent, "action", TODAY.isoformat(), [],
                                     user_address=ME, today=TODAY)
    dismissed, _ = priority.score_email(urgent, "action", TODAY.isoformat(), [],
                                        user_address=ME, today=TODAY, verdict="not_important")
    done, _ = priority.score_email(urgent, "action", TODAY.isoformat(), [],
                                   user_address=ME, today=TODAY, verdict="done")
    assert dismissed < normal and done < normal


def test_an_unknown_verdict_is_ignored_rather_than_crashing():
    a, _ = priority.score_email(email(), "fyi", None, [], user_address=ME, today=TODAY)
    b, _ = priority.score_email(email(), "fyi", None, [], user_address=ME, today=TODAY,
                                verdict="nonsense")
    assert a == b


def test_pin_is_a_large_boost_but_ordering_is_what_puts_it_on_top():
    """The score bump alone left a pinned item mid-list, because a pin cannot
    outrun a genuinely urgent email on points. The list query sorts pinned mail
    as its own group; the bump only orders items *within* that group."""
    quiet = email(subject="Re: draft outline", is_answered=1, is_read=1)
    plain, _ = priority.score_email(quiet, "fyi", None, [], user_address=ME, today=TODAY)
    pinned, _ = priority.score_email(quiet, "fyi", None, [], user_address=ME, today=TODAY,
                                     verdict="pinned")
    assert pinned - plain == priority.FEEDBACK_POINTS["pinned"]

    urgent = email(subject="Due today", is_read=0, importance="high")
    urgent_score, _ = priority.score_email(urgent, "action", TODAY.isoformat(), [],
                                           user_address=ME, today=TODAY)
    # Points alone are not enough -- which is exactly why ordering handles it.
    assert pinned < urgent_score
