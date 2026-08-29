"""One commitment, one row -- whichever mail carried it.

The reported bug: "Submit your unofficial transcript" appeared twice on the
same day, once tagged with the department and once with the user's own name.
Both were real. The department sent the notice and the user forwarded it to
themselves, each message was classified on its own, and the uniqueness rule at
the time was scoped per email -- (email_id, title, due_date) -- so two rows was
exactly what it was written to allow.

A deadline is a fact about the world, not a property of the message that
mentioned it. These tests pin that down, and pin down the things that made the
old scoping tempting: re-reading a message must not duplicate, and a message
falling silent must not delete a commitment another message still names.
"""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from app import db, planner


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return tmp_path


DAY = "2026-08-28"
TRANSCRIPT = {"title": "Submit your unofficial transcript", "due_date": DAY}
CURRICULUM = {"title": "Submit your curriculum document", "due_date": DAY}


def _emails(*ids):
    db.upsert_emails([{
        "id": eid, "conversation_id": "", "subject": f"Notice {eid}",
        "from_name": eid, "from_address": f"{eid}@example.edu",
        "to_recipients": '["you@example.com"]', "cc_recipients": "[]",
        "received_at": "2026-08-20T09:00:00+00:00",
        "is_read": 0, "is_answered": 0, "is_flagged": 0, "has_attachments": 0,
        "importance": "normal", "web_link": "", "folder": "INBOX",
        "body_preview": "", "body_text": "", "body_html": "",
        "synced_at": db.now_iso(),
    } for eid in ids])


def _titles_on(day=DAY):
    return sorted(t["title"] for t in db.tasks_between(day, day))


# --------------------------------------------------------------------------
# the reported bug
# --------------------------------------------------------------------------

def test_two_emails_naming_one_deadline_make_one_row(store):
    _emails("admin", "forwarded")
    db.sync_email_tasks("admin", [TRANSCRIPT, CURRICULUM])
    db.sync_email_tasks("forwarded", [TRANSCRIPT, CURRICULUM])

    assert _titles_on() == [
        "Submit your curriculum document",
        "Submit your unofficial transcript",
    ]


def test_the_second_email_is_recorded_as_a_source_not_dropped(store):
    """It contributed nothing new, but it did name the commitment -- and a
    message that named a commitment has been read."""
    _emails("admin", "forwarded")
    db.sync_email_tasks("admin", [TRANSCRIPT])
    db.sync_email_tasks("forwarded", [TRANSCRIPT])

    assert db.email_task_ids() == {"admin", "forwarded"}


def test_the_deduplicated_email_does_not_come_back_as_a_subject_chip(store):
    """The failure this guards against is the dedup appearing to do nothing.

    Collapse the two rows but leave the second email looking unread, and the
    calendar puts its subject line on the same day instead -- a duplicate again,
    just wearing the subject rather than the title."""
    _emails("admin", "forwarded")
    db.sync_email_tasks("admin", [TRANSCRIPT])
    db.sync_email_tasks("forwarded", [TRANSCRIPT])
    db.save_classification({
        "email_id": "forwarded", "bucket": "action", "deadline": DAY,
        "rationale": "", "score": 80, "matched": "", "model": "stub",
        "source": "llm", "created_at": db.now_iso(),
    })

    labels = [e["title"] for e in planner.entries(date(2026, 8, 28), date(2026, 8, 28))]
    assert labels.count("Submit your unofficial transcript") == 1
    assert "Notice forwarded" not in labels


def test_near_identical_wording_still_counts_as_one(store):
    """Two offices describing one deadline will not type it identically."""
    _emails("admin", "forwarded")
    db.sync_email_tasks("admin", [{"title": "Submit your unofficial transcript", "due_date": DAY}])
    db.sync_email_tasks(
        "forwarded", [{"title": "Submit your  unofficial Transcript.", "due_date": DAY}]
    )
    assert len(_titles_on()) == 1


def test_the_same_job_on_a_different_day_is_a_different_job(store):
    _emails("admin")
    db.sync_email_tasks("admin", [
        {"title": "Submit the monthly report", "due_date": "2026-08-28"},
        {"title": "Submit the monthly report", "due_date": "2026-09-28"},
    ])
    assert len(db.tasks_between("2026-08-01", "2026-09-30")) == 2


# --------------------------------------------------------------------------
# what the per-email scoping was protecting, which must still hold
# --------------------------------------------------------------------------

def test_re_reading_one_email_changes_nothing(store):
    _emails("admin")
    db.sync_email_tasks("admin", [TRANSCRIPT])
    first = db.tasks_between(DAY, DAY)[0]["id"]

    result = db.sync_email_tasks("admin", [TRANSCRIPT])
    rows = db.tasks_between(DAY, DAY)
    assert len(rows) == 1 and rows[0]["id"] == first
    assert result == {"added": 0, "dropped": 0}


def test_a_commitment_another_email_still_names_survives(store):
    """The reconcile has to ask "does anything else still say this?" before it
    deletes. Asking only about the email in hand deletes a live obligation the
    moment one of two senders stops repeating it."""
    _emails("admin", "forwarded")
    db.sync_email_tasks("admin", [TRANSCRIPT])
    db.sync_email_tasks("forwarded", [TRANSCRIPT])

    db.sync_email_tasks("admin", [])          # admin's mail no longer mentions it

    assert _titles_on() == ["Submit your unofficial transcript"]
    assert db.email_task_ids() == {"forwarded"}


def test_the_last_email_falling_silent_does_drop_it(store):
    _emails("admin")
    db.sync_email_tasks("admin", [TRANSCRIPT])
    assert db.sync_email_tasks("admin", []) == {"added": 0, "dropped": 1}
    assert _titles_on() == []


def test_a_ticked_commitment_is_never_dropped(store):
    _emails("admin")
    db.sync_email_tasks("admin", [TRANSCRIPT])
    db.update_task(db.tasks_between(DAY, DAY)[0]["id"], status="done")

    db.sync_email_tasks("admin", [])
    assert _titles_on() == ["Submit your unofficial transcript"]


def test_a_hand_written_item_is_adopted_and_never_deleted(store):
    """If the user wrote it down before the mail arrived, the mail joins their
    item rather than filing a second one beside it -- and losing the mail must
    not take their own note with it."""
    _emails("admin")
    mine = db.add_task("Submit your unofficial transcript", DAY)
    db.sync_email_tasks("admin", [TRANSCRIPT])

    rows = db.tasks_between(DAY, DAY)
    assert len(rows) == 1 and rows[0]["id"] == mine["id"]
    assert rows[0]["origin"] == "user"

    db.sync_email_tasks("admin", [])
    assert _titles_on() == ["Submit your unofficial transcript"]


def test_two_hand_written_items_may_read_alike(store):
    """The uniqueness rule is about what we inferred, not about what the user
    chose to type. Refusing their second note would be the app arguing."""
    db.add_task("Call the office", DAY)
    db.add_task("Call the office", DAY)
    assert len(db.tasks_between(DAY, DAY)) == 2


# --------------------------------------------------------------------------
# editing
# --------------------------------------------------------------------------

def test_a_renamed_item_survives_the_mail_being_read_again(store):
    """The link remembers the commitment as the *email* worded it, so the user's
    wording is free to differ. Matching on the row's current title instead read
    the rename as a commitment we no longer held: it deleted their edit and
    filed the model's original phrasing back in its place."""
    _emails("admin")
    db.sync_email_tasks("admin", [TRANSCRIPT])
    task_id = db.tasks_between(DAY, DAY)[0]["id"]

    db.update_task(task_id, title="Send the transcript to Dion")
    db.sync_email_tasks("admin", [TRANSCRIPT])

    rows = db.tasks_between(DAY, DAY)
    assert [r["title"] for r in rows] == ["Send the transcript to Dion"]
    assert rows[0]["id"] == task_id


def test_moving_an_item_to_another_day_survives_too(store):
    _emails("admin")
    db.sync_email_tasks("admin", [TRANSCRIPT])
    task_id = db.tasks_between(DAY, DAY)[0]["id"]

    db.update_task(task_id, due_date="2026-09-01")
    db.sync_email_tasks("admin", [TRANSCRIPT])

    assert db.tasks_between(DAY, DAY) == []
    assert [r["id"] for r in db.tasks_between("2026-09-01", "2026-09-01")] == [task_id]


def test_editing_one_item_into_another_is_refused_not_crashed(store):
    _emails("admin")
    db.sync_email_tasks("admin", [TRANSCRIPT, CURRICULUM])
    first, second = sorted(r["id"] for r in db.tasks_between(DAY, DAY))

    with pytest.raises(db.DuplicateTask):
        db.update_task(second, title=db.get_task(first)["title"])


# --------------------------------------------------------------------------
# upgrading a database that already holds the duplicates
# --------------------------------------------------------------------------

def _legacy_rows(path, rows):
    """Write task rows the way the old code did: no dedup_key, no link table."""
    conn = sqlite3.connect(path / "t.db")
    for title, due, status, deleted, email_id in rows:
        conn.execute(
            "INSERT INTO tasks (title, due_date, status, created_at, completed_at, "
            "deleted_at, email_id, origin) VALUES (?, ?, ?, ?, ?, ?, ?, 'email')",
            (title, due, status, "2026-08-01T00:00:00Z",
             "2026-08-02T00:00:00Z" if status == "done" else None, deleted, email_id),
        )
    conn.execute("DELETE FROM task_sources")
    conn.execute("UPDATE tasks SET dedup_key = NULL")
    conn.commit()
    conn.close()


def test_an_upgrade_collapses_duplicates_already_on_disk(store):
    """Danny's database held four rows for two commitments before this existed.
    Fixing the write path does nothing for them; the migration has to."""
    _legacy_rows(store, [
        ("Submit your unofficial transcript", DAY, "open", None, "admin"),
        ("Submit your curriculum document", DAY, "open", None, "admin"),
        ("Submit your unofficial transcript", DAY, "open", None, "forwarded"),
        ("Submit your curriculum document", DAY, "open", None, "forwarded"),
    ])
    db.init_db()

    assert _titles_on() == [
        "Submit your curriculum document",
        "Submit your unofficial transcript",
    ]
    assert db.email_task_ids() == {"admin", "forwarded"}


def test_the_upgrade_keeps_a_tick_from_either_copy(store):
    """Which of two identical chips they ticked is an accident of which one
    they happened to look at."""
    _legacy_rows(store, [
        ("Submit your unofficial transcript", DAY, "open", None, "admin"),
        ("Submit your unofficial transcript", DAY, "done", None, "forwarded"),
    ])
    db.init_db()

    rows = db.tasks_between(DAY, DAY)
    assert len(rows) == 1
    assert rows[0]["status"] == "done"
    assert rows[0]["completed_at"] == "2026-08-02T00:00:00Z"


def test_the_upgrade_keeps_the_visible_copy_when_one_was_removed(store):
    """Recovering something that came back is a click. Noticing that something
    never came back is not."""
    _legacy_rows(store, [
        ("Submit your unofficial transcript", DAY, "open", "2026-08-05T00:00:00Z", "admin"),
        ("Submit your unofficial transcript", DAY, "open", None, "forwarded"),
    ])
    db.init_db()

    assert _titles_on() == ["Submit your unofficial transcript"]
    assert db.removed_tasks() == []


def test_the_upgrade_is_idempotent(store):
    _emails("admin")
    db.sync_email_tasks("admin", [TRANSCRIPT, CURRICULUM])
    before = [dict(r) for r in db.tasks_between(DAY, DAY)]

    db.init_db()
    db.init_db()

    assert [dict(r) for r in db.tasks_between(DAY, DAY)] == before
