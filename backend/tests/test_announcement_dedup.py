"""One announcement, one row in the ranked lists.

Every test here puts the interesting case in front of the collapser *at once*.
The calendar's own recurrence test could not fail because it queried one day at
a time, so the two occurrences were never compared -- four vacuous tests in
three days, all that shape.
"""
from __future__ import annotations

from app import dedup


def mail(id_, subject, *, sender="dept@uni.edu", thread="", deadline=None,
         score=50.0, received="2026-09-10T09:00:00+00:00", verdict=None):
    return {"id": id_, "subject": subject, "from_address": sender,
            "conversation_id": thread, "deadline": deadline, "score": score,
            "received_at": received, "verdict": verdict}


OPEN = dict(eligible=lambda r: (r.get("verdict") or "") in ("", "pinned"),
            drop_groups=lambda r: (r.get("verdict") or "") in ("done", "not_relevant"))


# ------------------------------------------------------- the three shapes

def test_a_thread_is_one_announcement_however_the_subject_was_rewritten():
    """`答复:` is the Chinese `Re:`. Two replies on one thread were sitting at
    16 and 17 in the real priority list, looking like different mail."""
    rows = [
        mail("a", "Re: Sci-Fi Film Website", thread="AQHd123", sender="a@x.com"),
        mail("b", "答复: Sci-Fi Film Website", thread="AQHd123", sender="b@x.com"),
    ]
    assert len(dedup.collapse(rows, **OPEN)) == 1


def test_a_thread_holds_together_when_only_some_copies_carry_its_id():
    """Apple Mail records no Thread-Index for some messages. A single key
    would split the thread in two and the duplicate would survive; the
    union-find merges them through the shared subject."""
    rows = [
        mail("a", "Project update", thread="T1"),
        mail("b", "Re: Project update", thread=""),
        mail("c", "Project update", thread="T1"),
    ]
    assert len(dedup.collapse(rows, **OPEN)) == 1


def test_the_same_notice_sent_again_is_one_row():
    rows = [mail(str(i), "HKUST Daily Event Alert") for i in range(5)]
    out = dedup.collapse(rows, **OPEN)
    assert len(out) == 1 and out[0]["copies"] == 5


def test_a_reminder_and_the_thing_it_reminds_you_of_are_one_row():
    """Fourteen merges of this shape in the real mailbox, all correct:
    `Reminder: X` beside `X`, `[IEI Event of Today] X` beside `X`."""
    rows = [
        mail("a", "Call for Application | UROP Fall 2026-27", deadline="2026-08-26"),
        mail("b", "[Reminder] Call for Application | UROP Fall 2026-27", deadline="2026-08-26"),
    ]
    assert len(dedup.collapse(rows, **OPEN)) == 1


# ------------------------------------------------ what must NOT be collapsed

def test_two_different_notices_from_one_department_stay_two_rows():
    rows = [
        mail("a", "Job Offer Handling and Work Ethics", deadline="2026-09-15"),
        mail("b", "Financial Planning cum MPF Workshop", deadline="2026-09-15"),
    ]
    assert len(dedup.collapse(rows, **OPEN)) == 2


def test_containment_needs_the_same_deadline():
    """Without that clause the test is just "one subject is inside another",
    which two genuinely different notices from one department can satisfy."""
    rows = [
        mail("a", "Call for Application | UROP Fall 2026-27", deadline="2026-08-26"),
        mail("b", "[Reminder] Call for Application | UROP Fall 2026-27", deadline="2026-10-02"),
    ]
    assert len(dedup.collapse(rows, **OPEN)) == 2


def test_containment_needs_the_same_sender():
    rows = [
        mail("a", "Call for Application | UROP Fall", sender="dept@uni.edu", deadline="2026-08-26"),
        mail("b", "[Reminder] Call for Application | UROP Fall", sender="other@uni.edu",
             deadline="2026-08-26"),
    ]
    assert len(dedup.collapse(rows, **OPEN)) == 2


def test_a_short_subject_inside_another_is_a_coincidence_not_a_containment():
    rows = [
        mail("a", "Hi", deadline="2026-08-26"),
        mail("b", "Hi there", deadline="2026-08-26"),
    ]
    assert len(dedup.collapse(rows, **OPEN)) == 2


def test_a_weekly_newsletter_with_different_numbers_is_not_a_duplicate():
    rows = [
        mail("a", "Updates from Career Center (Fall 2026 Week 3)"),
        mail("b", "Updates from Career Center (Fall 2026 Week 4)"),
    ]
    assert len(dedup.collapse(rows, **OPEN)) == 2


# --------------------------------------------------------- which copy shows

def test_the_best_ranked_copy_survives_not_the_newest():
    """The difference from the calendar, and it is load-bearing.

    The ICAC seminar is ranked second in the real list and its newest copy
    scores sixteen points lower. Keeping the newest -- which is what the
    calendar does, for good reasons of its own -- would have quietly demoted it
    out of the top twenty. Collapsing removes rows; it does not reorder.
    """
    rows = [
        mail("old", "ICAC Seminar", score=82.0, received="2026-09-11T09:00:00+00:00"),
        mail("new", "ICAC Seminar", score=66.0, received="2026-09-18T09:00:00+00:00"),
    ]
    out = dedup.collapse(rows, **OPEN)
    assert [r["id"] for r in out] == ["old"]
    assert out[0]["copies"] == 2


def test_equal_copies_go_to_the_most_recent():
    rows = [
        mail("old", "Seminar", score=70.0, received="2026-09-11T09:00:00+00:00"),
        mail("new", "Seminar", score=70.0, received="2026-09-18T09:00:00+00:00"),
    ]
    assert [r["id"] for r in dedup.collapse(rows, **OPEN)] == ["new"]


def test_the_caller_s_order_survives_collapsing():
    rows = [mail("a", "A", score=90), mail("b", "B", score=80),
            mail("c", "A", score=10), mail("d", "C", score=70)]
    assert [r["id"] for r in dedup.collapse(rows, **OPEN)] == ["a", "b", "d"]


# ------------------------------------------------- finishing an announcement

def test_finishing_the_row_does_not_promote_a_sibling():
    """The trap this whole mechanism creates for itself.

    The user sees one row standing for three messages and ticks it. The two
    copies they never saw are still open, so without dropping the group the
    announcement returns immediately wearing a different id -- and it looks
    exactly like the tick did nothing.
    """
    rows = [
        mail("a", "ICAC Seminar", score=82, verdict="done"),
        mail("b", "ICAC Seminar", score=70),
        mail("c", "ICAC Seminar", score=60),
    ]
    assert dedup.collapse(rows, **OPEN) == []


def test_dismissing_one_copy_takes_the_announcement_with_it():
    rows = [mail("a", "Promo", verdict="not_relevant"), mail("b", "Promo")]
    assert dedup.collapse(rows, **OPEN) == []


def test_a_snoozed_copy_counts_but_cannot_be_the_row():
    """It is still a copy -- it says something about how often this arrives --
    but the user put it away, so it must not be what the list shows."""
    rows = [
        mail("a", "Seminar", score=90, verdict="snoozed"),
        mail("b", "Seminar", score=50),
    ]
    out = dedup.collapse(rows, **OPEN)
    assert [r["id"] for r in out] == ["b"]
    assert out[0]["copies"] == 2


def test_a_group_with_nothing_showable_disappears_rather_than_leaking_one():
    rows = [mail("a", "Seminar", verdict="snoozed"), mail("b", "Seminar", verdict="snoozed")]
    assert dedup.collapse(rows, **OPEN) == []


def test_pinning_is_not_finishing():
    rows = [mail("a", "Seminar", score=10, verdict="pinned"), mail("b", "Seminar", score=90)]
    assert len(dedup.collapse(rows, **OPEN)) == 1


# ------------------------------------------------------------- bookkeeping

def test_every_surviving_row_says_how_many_it_stands_for():
    rows = [mail("a", "A"), mail("b", "A"), mail("c", "B")]
    out = {r["subject"]: r["copies"] for r in dedup.collapse(rows, **OPEN)}
    assert out == {"A": 2, "B": 1}


def test_an_empty_list_collapses_to_an_empty_list():
    assert dedup.collapse([], **OPEN) == []


def test_the_normaliser_strips_reply_markers_in_every_language_the_repo_knows():
    base = dedup.normalise_subject("Sci-Fi Film Website")
    for prefix in ("Re: ", "RE: ", "답장: ", "答复: ", "回复: ", "Fwd: ", "转发: "):
        assert dedup.normalise_subject(prefix + "Sci-Fi Film Website") == base, prefix


def test_the_calendar_keeps_the_day_in_its_key():
    """The two surfaces disagree on purpose, and that has to stay true.

    A calendar exists to put each occurrence on its own square, so a weekly
    seminar must not collapse there. A ranked list has no day to disambiguate
    with, so it must.
    """
    from app import planner
    monday = planner._announcement_key("Weekly seminar", "2026-09-07")
    next_monday = planner._announcement_key("Weekly seminar", "2026-09-14")
    assert monday != next_monday

    rows = [mail("a", "Weekly seminar", deadline="2026-09-07"),
            mail("b", "Weekly seminar", deadline="2026-09-14")]
    assert len(dedup.collapse(rows, **OPEN)) == 1, "the list collapses what the calendar keeps"
