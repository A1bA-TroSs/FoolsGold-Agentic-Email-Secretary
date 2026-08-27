"""Calendar arithmetic and the rules for what appears on it.

The date maths gets its own tests because it is the classic place where code
looks correct all summer and then breaks in February.
"""
from __future__ import annotations

from datetime import date

import pytest

from app import db, planner


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return tmp_path


# --------------------------------------------------------------------------
# grid arithmetic -- no database needed
# --------------------------------------------------------------------------

def test_grid_is_always_six_full_weeks():
    for year, month in [(2026, 2), (2026, 8), (2024, 2), (2026, 12), (2027, 1)]:
        weeks = planner.month_grid(year, month)
        assert len(weeks) == 6
        assert all(len(w) == 7 for w in weeks)


def test_grid_starts_on_the_requested_weekday():
    # Monday
    for week in planner.month_grid(2026, 8, week_starts_on=0):
        assert week[0].weekday() == 0
    # Sunday
    for week in planner.month_grid(2026, 8, week_starts_on=6):
        assert week[0].weekday() == 6


def test_grid_is_contiguous_and_contains_the_whole_month():
    weeks = planner.month_grid(2026, 2)
    flat = [d for week in weeks for d in week]
    for earlier, later in zip(flat, flat[1:]):
        assert (later - earlier).days == 1
    first, last = planner.month_bounds(2026, 2)
    assert first in flat and last in flat


def test_february_lengths_including_leap_years():
    assert planner.month_bounds(2026, 2)[1] == date(2026, 2, 28)
    assert planner.month_bounds(2024, 2)[1] == date(2024, 2, 29)
    assert planner.month_bounds(2000, 2)[1] == date(2000, 2, 29)   # divisible by 400
    assert planner.month_bounds(1900, 2)[1] == date(1900, 2, 28)   # divisible by 100, not 400


def test_month_that_starts_on_the_week_start_still_gets_six_rows():
    """A 28-day February beginning exactly on the week start fits in four rows.
    The grid must still be six, or the layout changes height as you page."""
    year, month = 2021, 2                       # 1 Feb 2021 was a Monday
    assert date(year, month, 1).weekday() == 0
    assert len(planner.month_grid(year, month)) == 6


def test_shift_month_crosses_year_boundaries_in_both_directions():
    assert planner.shift_month(2026, 12, 1) == (2027, 1)
    assert planner.shift_month(2026, 1, -1) == (2025, 12)
    assert planner.shift_month(2026, 8, 12) == (2027, 8)
    assert planner.shift_month(2026, 8, -12) == (2025, 8)
    assert planner.shift_month(2026, 1, -13) == (2024, 12)


def test_month_number_out_of_range_is_rejected():
    with pytest.raises(ValueError):
        planner.month_bounds(2026, 13)
    with pytest.raises(ValueError):
        planner.month_bounds(2026, 0)


def test_parse_day_rejects_dates_that_do_not_exist():
    assert planner.parse_day("2026-02-30") is None
    assert planner.parse_day("2026-13-01") is None
    assert planner.parse_day("next tuesday") is None
    assert planner.parse_day("") is None
    assert planner.parse_day(None) is None
    assert planner.parse_day("2026-02-28") == date(2026, 2, 28)


# --------------------------------------------------------------------------
# what lands on the calendar
# --------------------------------------------------------------------------

def _email(email_id: str, sender: str, subject: str, deadline: str | None, bucket: str = "action"):
    db.upsert_emails([{
        "id": email_id, "conversation_id": "", "subject": subject,
        "from_name": sender.split("@")[0], "from_address": sender,
        "to_recipients": "[]", "cc_recipients": "[]",
        "received_at": "2026-08-20T09:00:00+00:00",
        "is_read": 0, "is_answered": 0, "is_flagged": 0, "has_attachments": 0,
        "importance": "normal", "web_link": "", "folder": "INBOX",
        "body_preview": "", "body_text": "", "body_html": "",
        "synced_at": db.now_iso(),
    }])
    db.save_classification({
        "email_id": email_id, "bucket": bucket, "deadline": deadline,
        "rationale": "", "score": 10.0, "matched": "", "model": "",
        "source": "structural", "created_at": db.now_iso(),
    })


def test_email_deadlines_appear_without_being_asked_for(store):
    _email("e1", "prof@ust.hk", "Chapter 3 comments", "2026-08-27")
    items = planner.entries(date(2026, 8, 1), date(2026, 8, 31))
    assert [i["title"] for i in items] == ["Chapter 3 comments"]
    assert items[0]["kind"] == "email"
    assert items[0]["done"] is False


def test_an_email_without_a_deadline_never_reaches_the_calendar(store):
    _email("e1", "prof@ust.hk", "No date here", None)
    assert planner.entries(date(2026, 1, 1), date(2026, 12, 31)) == []


def test_a_nonsense_deadline_drops_one_entry_not_the_month(store):
    _email("good", "a@b.com", "Real", "2026-08-27")
    _email("bad", "a@b.com", "Hallucinated", "2026-02-31")
    titles = [i["title"] for i in planner.entries(date(2026, 1, 1), date(2026, 12, 31))]
    assert titles == ["Real"]


def test_ticking_an_email_keeps_it_on_the_calendar_struck_through(store):
    _email("e1", "prof@ust.hk", "Chapter 3 comments", "2026-08-27")
    planner.set_done("email", "e1", True)
    items = planner.entries(date(2026, 8, 27), date(2026, 8, 27))
    assert len(items) == 1, "a completed item must stay on the day it was due"
    assert items[0]["done"] is True
    assert items[0]["completed_at"]


def test_unticking_an_email_does_not_discard_an_unrelated_pin(store):
    _email("e1", "prof@ust.hk", "Chapter 3", "2026-08-27")
    db.set_feedback("e1", "pinned")
    planner.set_done("email", "e1", False)
    assert db.all_feedback()["e1"]["verdict"] == "pinned"


def test_unticking_an_email_that_was_done_clears_the_verdict(store):
    _email("e1", "prof@ust.hk", "Chapter 3", "2026-08-27")
    planner.set_done("email", "e1", True)
    planner.set_done("email", "e1", False)
    assert "e1" not in db.all_feedback()


def test_muting_the_sender_takes_its_deadlines_off_the_calendar(store):
    _email("e1", "noreply@spam.io", "Sale ends Friday", "2026-08-28")
    assert len(planner.entries(date(2026, 8, 1), date(2026, 8, 31))) == 1
    db.mute_sender("noreply@spam.io")
    assert planner.entries(date(2026, 8, 1), date(2026, 8, 31)) == []


def test_unmuting_puts_them_back(store):
    _email("e1", "noreply@spam.io", "Sale ends Friday", "2026-08-28")
    db.mute_sender("noreply@spam.io")
    db.unmute_sender("noreply@spam.io")
    assert len(planner.entries(date(2026, 8, 1), date(2026, 8, 31))) == 1


def test_deleting_an_email_entry_hides_it_without_deleting_the_mail(store):
    _email("e1", "prof@ust.hk", "Chapter 3", "2026-08-27")
    planner.remove("email", "e1")
    assert planner.entries(date(2026, 8, 1), date(2026, 8, 31)) == []
    assert db.get_emails(["e1"]), "the message itself must survive"
    db.unhide_calendar_email("e1")
    assert len(planner.entries(date(2026, 8, 1), date(2026, 8, 31))) == 1


# --------------------------------------------------------------------------
# hand-written tasks
# --------------------------------------------------------------------------

def test_a_task_can_be_added_ticked_and_stays_put(store):
    task = db.add_task("Book the microscope room", "2026-08-27")
    items = planner.entries(date(2026, 8, 27), date(2026, 8, 27))
    assert [i["title"] for i in items] == ["Book the microscope room"]
    planner.set_done("task", task["id"], True)
    after = planner.entries(date(2026, 8, 27), date(2026, 8, 27))
    assert len(after) == 1 and after[0]["done"] is True


def test_reopening_a_task_clears_its_completion_time(store):
    task = db.add_task("Thing", "2026-08-27")
    planner.set_done("task", task["id"], True)
    assert db.get_task(task["id"])["completed_at"]
    planner.set_done("task", task["id"], False)
    assert db.get_task(task["id"])["completed_at"] is None


def test_deleting_a_task_really_deletes_it(store):
    task = db.add_task("Thing", "2026-08-27")
    planner.remove("task", task["id"])
    assert db.get_task(task["id"]) is None


def test_missing_entries_raise_rather_than_pass_silently(store):
    with pytest.raises(KeyError):
        planner.set_done("task", 4242, True)
    with pytest.raises(KeyError):
        planner.remove("task", 4242)
    with pytest.raises(ValueError):
        planner.set_done("meeting", "1", True)


# --------------------------------------------------------------------------
# ordering and the agenda a reminder reads from
# --------------------------------------------------------------------------

def test_open_items_sort_above_completed_ones_within_a_day(store):
    _email("e1", "a@b.com", "Aaa email", "2026-08-27")
    done_task = db.add_task("Bbb task", "2026-08-27")
    db.add_task("Ccc task", "2026-08-27")
    planner.set_done("task", done_task["id"], True)
    items = planner.entries(date(2026, 8, 27), date(2026, 8, 27))
    assert [i["done"] for i in items] == [False, False, True]


def test_agenda_carries_overdue_work_forward(store):
    db.add_task("Yesterday, unfinished", "2026-08-26")
    db.add_task("Today", "2026-08-27")
    finished = db.add_task("Yesterday, finished", "2026-08-26")
    planner.set_done("task", finished["id"], True)

    day = planner.agenda(date(2026, 8, 27))
    assert [i["title"] for i in day["open"]] == ["Today"]
    assert [i["title"] for i in day["overdue"]] == ["Yesterday, unfinished"]
    assert day["weekday"] == date(2026, 8, 27).weekday()


def test_agenda_of_an_empty_day_is_empty_not_an_error(store):
    day = planner.agenda(date(2026, 8, 27))
    assert day["open"] == [] and day["done"] == [] and day["overdue"] == []


def test_month_payload_covers_every_day_it_claims_to(store):
    _email("e1", "a@b.com", "Due next month", "2026-09-01")
    payload = planner.month(2026, 8)
    flat = [d for week in payload["weeks"] for d in week]
    assert payload["start"] == flat[0] and payload["end"] == flat[-1]
    # 1 Sep 2026 falls in August's trailing row, so it must be reachable.
    assert "2026-09-01" in flat
    assert "2026-09-01" in payload["days"]


# --------------------------------------------------------------------------
# muting from the calendar
# --------------------------------------------------------------------------

def test_email_entries_carry_the_address_needed_to_mute(store):
    _email("e1", "promo@shop.io", "Sale ends Friday", "2026-08-28")
    entry = planner.entries(date(2026, 8, 28), date(2026, 8, 28))[0]
    assert entry["sender_address"] == "promo@shop.io"


def test_own_tasks_have_no_sender_to_mute(store):
    db.add_task("Write it up", "2026-08-28")
    entry = planner.entries(date(2026, 8, 28), date(2026, 8, 28))[0]
    assert entry["sender_address"] is None


def test_muting_from_one_day_clears_that_sender_across_the_whole_month(store):
    for i, day in enumerate(("2026-08-05", "2026-08-14", "2026-08-28")):
        _email(f"ad{i}", "promo@shop.io", f"Offer {i}", day)
    _email("real", "prof@ust.hk", "Chapter 3", "2026-08-27")

    assert len(planner.entries(date(2026, 8, 1), date(2026, 8, 31))) == 4
    db.mute_sender("promo@shop.io")
    left = planner.entries(date(2026, 8, 1), date(2026, 8, 31))
    assert [e["title"] for e in left] == ["Chapter 3"]


def test_muting_also_clears_future_and_past_months(store):
    _email("past", "promo@shop.io", "July offer", "2026-07-04")
    _email("future", "promo@shop.io", "October offer", "2026-10-31")
    db.mute_sender("promo@shop.io")
    assert planner.entries(date(2026, 1, 1), date(2026, 12, 31)) == []


def test_a_completed_entry_from_a_muted_sender_goes_too(store):
    """Muting is about the correspondent, so it does not spare the rows you
    happened to tick off first."""
    _email("ad", "promo@shop.io", "Offer", "2026-08-28")
    planner.set_done("email", "ad", True)
    db.mute_sender("promo@shop.io")
    assert planner.entries(date(2026, 8, 1), date(2026, 8, 31)) == []


# --------------------------------------------------------------------------
# the removed box
# --------------------------------------------------------------------------

def test_deleting_a_task_keeps_it_recoverable(store):
    task = db.add_task("Book the room", "2026-08-27")
    planner.remove("task", task["id"])

    assert planner.entries(date(2026, 8, 27), date(2026, 8, 27)) == []
    assert db.get_task(task["id"]) is None, "gone from the working view"
    assert db.get_task(task["id"], include_deleted=True) is not None, "but not destroyed"

    removed = planner.removed()
    assert [r["title"] for r in removed] == ["Book the room"]
    assert removed[0]["kind"] == "task"
    assert removed[0]["can_purge"] is True


def test_restoring_a_task_puts_it_back_on_its_original_day(store):
    task = db.add_task("Book the room", "2026-08-27")
    planner.remove("task", task["id"])
    planner.restore("task", task["id"])

    items = planner.entries(date(2026, 8, 27), date(2026, 8, 27))
    assert [i["title"] for i in items] == ["Book the room"]
    assert planner.removed() == []


def test_a_completed_task_comes_back_still_completed(store):
    task = db.add_task("Book the room", "2026-08-27")
    planner.set_done("task", task["id"], True)
    planner.remove("task", task["id"])
    planner.restore("task", task["id"])
    assert planner.entries(date(2026, 8, 27), date(2026, 8, 27))[0]["done"] is True


def test_a_deleted_task_cannot_be_edited_or_ticked(store):
    """It is not on the calendar, so nothing should be able to reach it except
    restore and purge."""
    task = db.add_task("Book the room", "2026-08-27")
    planner.remove("task", task["id"])
    assert db.update_task(task["id"], title="Changed") is None
    with pytest.raises(KeyError):
        planner.set_done("task", task["id"], True)


def test_deleting_twice_does_not_duplicate_the_entry(store):
    task = db.add_task("Book the room", "2026-08-27")
    planner.remove("task", task["id"])
    with pytest.raises(KeyError):
        planner.remove("task", task["id"])
    assert len(planner.removed()) == 1


def test_purging_a_task_is_final(store):
    task = db.add_task("Book the room", "2026-08-27")
    planner.remove("task", task["id"])
    planner.purge("task", task["id"])
    assert db.get_task(task["id"], include_deleted=True) is None
    assert planner.removed() == []


def test_an_email_due_date_can_be_removed_and_restored(store):
    _email("e1", "prof@ust.hk", "Chapter 3", "2026-08-27")
    planner.remove("email", "e1")

    removed = planner.removed()
    assert [r["title"] for r in removed] == ["Chapter 3"]
    assert removed[0]["due"] == "2026-08-27", "the box has to say when it was due"
    assert removed[0]["can_purge"] is False

    planner.restore("email", "e1")
    assert len(planner.entries(date(2026, 8, 27), date(2026, 8, 27))) == 1
    assert planner.removed() == []


def test_an_email_due_date_cannot_be_purged(store):
    """Purging an email entry would delete our *tombstone*, putting the date
    back -- the opposite of what the button would promise."""
    _email("e1", "prof@ust.hk", "Chapter 3", "2026-08-27")
    planner.remove("email", "e1")
    with pytest.raises(ValueError):
        planner.purge("email", "e1")
    assert len(planner.removed()) == 1, "and it stays removed"


def test_restoring_something_that_was_never_removed_is_an_error(store):
    _email("e1", "prof@ust.hk", "Chapter 3", "2026-08-27")
    task = db.add_task("Live task", "2026-08-27")
    with pytest.raises(KeyError):
        planner.restore("email", "e1")
    with pytest.raises(KeyError):
        planner.restore("task", task["id"])
    with pytest.raises(ValueError):
        planner.restore("meeting", "1")


def test_muted_senders_are_not_listed_in_the_removed_box(store):
    """Muting is undone by address in the muted box. Offering a per-date
    restore here would appear to work and change nothing."""
    _email("ad", "promo@shop.io", "Offer", "2026-08-28")
    db.mute_sender("promo@shop.io")
    assert planner.removed() == []


def test_the_removed_box_is_newest_first(store):
    a = db.add_task("First removed", "2026-08-27")
    b = db.add_task("Second removed", "2026-08-27")
    planner.remove("task", a["id"])
    planner.remove("task", b["id"])
    titles = [r["title"] for r in planner.removed()]
    assert titles[0] == "Second removed"


def test_removed_entries_survive_a_reopen_of_the_database(store):
    task = db.add_task("Book the room", "2026-08-27")
    planner.remove("task", task["id"])
    db.init_db()                      # what a restart does
    assert [r["title"] for r in planner.removed()] == ["Book the room"]


def test_the_deleted_at_column_is_added_to_an_existing_database(tmp_path, monkeypatch):
    """Upgraders have a tasks table with no deleted_at. SQLite has no
    ADD COLUMN IF NOT EXISTS, so this is exactly the shape of bug that ships."""
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "old.db")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
            "due_date TEXT NOT NULL, note TEXT, status TEXT NOT NULL DEFAULT 'open', "
            "created_at TEXT, completed_at TEXT)"
        )
        conn.execute("INSERT INTO tasks (title, due_date, status) VALUES ('Old row', '2026-08-27', 'open')")
        conn.commit()

    db.init_db()

    columns = {r["name"] for r in db.connect().execute("PRAGMA table_info(tasks)")}
    assert "deleted_at" in columns
    assert [t["title"] for t in db.tasks_between("2026-08-01", "2026-08-31")] == ["Old row"]
    assert planner.removed() == []


def test_a_removed_entry_from_a_muted_sender_is_not_offered_for_restore(store):
    """Otherwise the box shows a Restore button that appears to work and
    changes nothing: the mute filter hides the entry again immediately."""
    _email("ad", "promo@shop.io", "Offer", "2026-08-28")
    planner.remove("email", "ad")
    assert len(planner.removed()) == 1

    db.mute_sender("promo@shop.io")
    assert planner.removed() == [], "muting is undone in the muted box, by address"

    db.unmute_sender("promo@shop.io")
    assert len(planner.removed()) == 1, "and unmuting brings the row back here"
    assert planner.entries(date(2026, 8, 28), date(2026, 8, 28)) == [], "still removed, though"
