"""The recap card: what piled up since you last looked.

Three things are worth a test more than the assembly is:

- the window moves ONLY when the card is dismissed, and reading it changes no
  `is_read` flag -- a summary that consumes its own input is worse than none;
- every line carries an openable id, because the whole promise of the card is
  that each line links to its mail;
- `category` and `summary` actually reach the database. `category` did not, for
  1,315 rows, and nothing failed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import recap


def _db(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()
    return db


def _email(db, eid, *, subject="Subject", sender="dept@uni.edu", hours_ago=1,
           read=0, thread=None):
    when = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO emails (id, conversation_id, subject, from_name, from_address, "
            "received_at, is_read, folder, body_preview) VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, thread, subject, sender.split("@")[0], sender, when, read, "INBOX", ""),
        )
        conn.commit()


def _classify(db, eid, **kw):
    rec = {"email_id": eid, "bucket": "fyi", "deadline": None, "rationale": "",
           "score": 1.0, "matched": "", "model": "m", "source": "llm",
           "created_at": datetime.now(timezone.utc).isoformat()}
    rec.update(kw)
    db.save_classification(rec)


# ---------------------------------------------------------------- persistence

def test_category_and_summary_survive_a_round_trip(tmp_path, monkeypatch):
    """The regression this feature was built on top of.

    `category` was passed to `save_classification` in a dict whose extra keys
    sqlite3 silently ignores, so it was never written and never raised."""
    db = _db(tmp_path, monkeypatch)
    _email(db, "e1")
    _classify(db, "e1", category="career", summary="Careers office opens co-op applications on Monday.")
    with db.connect() as conn:
        row = conn.execute("SELECT category, summary FROM classifications WHERE email_id='e1'").fetchone()
    assert row["category"] == "career"
    assert row["summary"].startswith("Careers office")


def test_an_update_does_not_blank_the_new_columns(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _email(db, "e1")
    _classify(db, "e1", category="exam", summary="Midterm moved to 10 October.")
    _classify(db, "e1", category="exam", summary="Midterm moved to 10 October.", score=9.0)
    with db.connect() as conn:
        row = conn.execute("SELECT category, summary, score FROM classifications WHERE email_id='e1'").fetchone()
    assert (row["category"], row["score"]) == ("exam", 9.0)
    assert row["summary"]


# --------------------------------------------------------------- the window

def test_first_run_looks_back_a_day_not_to_the_beginning(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _email(db, "old", hours_ago=40)
    _email(db, "new", hours_ago=2)
    _classify(db, "old")
    _classify(db, "new")
    card = recap.build()
    assert card["first_run"] is True
    ids = {l["email_id"] for s in card["sections"] for l in s["lines"]}
    assert ids == {"new"}


def test_dismissing_is_the_only_thing_that_moves_the_window(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _email(db, "e1", hours_ago=2)
    _classify(db, "e1")

    before = db.get_setting(recap.SEEN_KEY, "")
    recap.build()
    recap.build()
    assert db.get_setting(recap.SEEN_KEY, "") == before == ""

    recap.mark_seen()
    assert db.get_setting(recap.SEEN_KEY, "") != ""


def test_building_the_card_never_marks_mail_read(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _email(db, "e1", hours_ago=2)
    _classify(db, "e1")
    recap.build()
    recap.mark_seen()
    with db.connect() as conn:
        assert conn.execute("SELECT is_read FROM emails WHERE id='e1'").fetchone()["is_read"] == 0


def test_the_card_is_suppressed_right_after_a_dismissal(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _email(db, "e1", hours_ago=0)
    _classify(db, "e1")
    recap.mark_seen()
    assert recap.build()["too_soon"] is True


def test_a_long_absence_is_clamped_rather_than_enumerated(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    db.set_setting(recap.SEEN_KEY, long_ago.isoformat())
    card = recap.build()
    assert card["clamped"] is True
    since = datetime.fromisoformat(card["since"])
    assert (datetime.now(timezone.utc) - since).days == recap.WINDOW_CEILING_DAYS


# ------------------------------------------------------------------ the pile

def test_read_settled_and_muted_mail_are_not_the_pile(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _email(db, "read", subject="Already opened", read=1)
    _email(db, "done", subject="Finished with")
    _email(db, "muted", subject="From a muted sender", sender="spam@ads.com")
    _email(db, "keep", subject="Still waiting")
    for eid in ("read", "done", "muted", "keep"):
        _classify(db, eid)
    db.set_feedback("done", "done")
    db.mute_sender("spam@ads.com")

    ids = {l["email_id"] for s in recap.build()["sections"] for l in s["lines"]}
    assert ids == {"keep"}


def test_one_announcement_sent_three_times_is_one_line(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    for i, subject in enumerate(("Seminar on Friday", "Reminder: Seminar on Friday",
                                 "Seminar on Friday")):
        _email(db, f"e{i}", subject=subject, thread="t1", hours_ago=i + 1)
        _classify(db, f"e{i}")
    card = recap.build()
    lines = [l for s in card["sections"] for l in s["lines"]]
    assert len(lines) == 1
    assert lines[0]["copies"] == 3
    assert len(lines[0]["email_ids"]) == 3


def test_every_line_can_be_opened(tmp_path, monkeypatch):
    """The card's whole promise. A line with no id is a line that lies."""
    db = _db(tmp_path, monkeypatch)
    for i in range(5):
        _email(db, f"e{i}", subject=f"Thing {i}", sender=f"s{i}@uni.edu")
        _classify(db, f"e{i}", bucket=("action" if i % 2 else "fyi"),
                  category="event", summary=f"Summary {i}")
    card = recap.build()
    lines = [l for s in card["sections"] for l in s["lines"]]
    assert lines
    for line in lines:
        assert line["email_id"]
        assert line["email_ids"] and line["email_id"] in line["email_ids"]


def test_action_mail_leads_and_categories_section_the_rest(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _email(db, "a", subject="Submit the form", sender="coop@uni.edu")
    _classify(db, "a", bucket="action", deadline="2026-10-01", category="career")
    _email(db, "b", subject="Seminar next week", sender="lab@uni.edu")
    _classify(db, "b", bucket="fyi", category="event")
    card = recap.build()
    keys = [s["key"] for s in card["sections"]]
    assert keys[0] == "needs"
    assert "cat:event" in keys


def test_a_task_title_becomes_the_line_when_there_is_one(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _email(db, "a", subject="Co-op programme update")
    _classify(db, "a", bucket="action")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (title, due_date, status, created_at, email_id, origin) "
            "VALUES (?,?,?,?,?,?)",
            ("Submit the Co-op progress report", "2026-10-02", "open",
             datetime.now(timezone.utc).isoformat(), "a", "email"),
        )
        conn.commit()
    line = recap.build()["sections"][0]["lines"][0]
    assert line["task"] == "Submit the Co-op progress report"


# ------------------------------------------------------- display categories

def test_an_unlabelled_mail_is_grouped_on_screen_but_not_in_the_database(tmp_path, monkeypatch):
    """The display guess must not become training data.

    `classifications.category` feeds the learned per-category weights. A
    keyword guess written there would attach the user's corrections to a drawer
    a regex invented."""
    db = _db(tmp_path, monkeypatch)
    _email(db, "a", subject="[공지] 기말고사 일정 안내")
    _classify(db, "a", category="")
    card = recap.build()
    assert any(s["key"] == "cat:exam" for s in card["sections"])
    with db.connect() as conn:
        assert conn.execute("SELECT category FROM classifications WHERE email_id='a'").fetchone()["category"] == ""


def test_the_model_label_wins_over_the_keyword_guess(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _email(db, "a", subject="Hackathon registration now open")
    _classify(db, "a", category="career")
    assert any(s["key"] == "cat:career" for s in recap.build()["sections"])
