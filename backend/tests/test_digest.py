"""The briefing is a checklist bound to real emails, so its rows must always
point at something the user can open."""
from __future__ import annotations

from datetime import date, timedelta

from app import pipeline

TODAY = date.today()


def mail(id_, subject, bucket="action", deadline=None, sender="Prof. Lee"):
    return {
        "id": id_, "subject": subject, "from_name": sender, "from_address": "lee@uni.edu",
        "bucket": bucket, "deadline": deadline, "score": 50, "received_at": "2026-08-26T09:00:00+00:00",
        "body_text": "", "body_preview": "",
    }


def test_fallback_agenda_puts_deadlines_first():
    emails = [
        mail("a", "No deadline here"),
        mail("b", "Due today", deadline=TODAY.isoformat()),
        mail("c", "Due next week", deadline=(TODAY + timedelta(days=6)).isoformat()),
    ]
    headline, rows = pipeline._fallback_agenda(emails)
    assert [r["email_id"] for r in rows][:2] == ["b", "c"]
    assert rows[0]["note_key"] == "dueToday"
    assert rows[1]["note_key"] == "dueOn" and rows[1]["note_vars"]["date"]
    # The backend emits a key, not a sentence -- otherwise the briefing stays
    # English while the rest of the UI is translated.
    assert headline["key"] == "agendaHeadline"
    assert headline["vars"] == {"n": len(rows), "d": 2}


def test_every_agenda_row_references_a_real_email():
    emails = [mail(str(i), f"Subject {i}") for i in range(5)]
    _, rows = pipeline._fallback_agenda(emails)
    ids = {e["id"] for e in emails}
    assert rows and all(r["email_id"] in ids for r in rows)
    assert all(r["subject"] and "sender" in r for r in rows)


def test_agenda_is_capped_so_the_briefing_stays_a_briefing():
    emails = [mail(str(i), f"Subject {i}") for i in range(30)]
    _, rows = pipeline._fallback_agenda(emails)
    assert len(rows) <= 7


def test_nothing_urgent_reads_plainly():
    headline, rows = pipeline._fallback_agenda([mail("a", "FYI thing", bucket="fyi")])
    assert rows == []
    assert headline == {"key": "nothingUrgent"}


def test_structural_rows_never_carry_english_prose():
    """Regression: the fallback used to hard-code 'due today' / 'needs a reply',
    so a Korean UI showed an English briefing."""
    emails = [mail("a", "One", deadline=TODAY.isoformat()), mail("b", "Two")]
    _, rows = pipeline._fallback_agenda(emails)
    assert all(r["note_key"] for r in rows)
    assert all(r["note"] == "" for r in rows)


def test_stored_digest_json_round_trips():
    row = {"body": '{"headline":"Two things.","items":[{"email_id":"a","subject":"S"}]}'}
    out = pipeline._unpack(row)
    assert out["headline"] == "Two things."
    assert out["items"][0]["email_id"] == "a"


def test_a_legacy_markdown_digest_still_renders_as_a_headline():
    """Old rows hold plain markdown; they must not crash the card."""
    out = pipeline._unpack({"body": "- Due today - something"})
    assert out["items"] == []
    assert out["headline"].startswith("- Due today")
