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


def test_a_cached_briefing_is_rebuilt_when_the_provider_changes(tmp_path, monkeypatch):
    """The footer names what produced the briefing, and the briefing is cached
    for a day. Switching provider at lunchtime left the morning's stamp naming
    a provider that was no longer in use -- a footer reporting a fact that had
    stopped being true."""
    from app import db, pipeline

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    day = __import__("datetime").date.today().isoformat()
    import json
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO digests (day, body, model, created_at) VALUES (?, ?, ?, ?)",
            (day, json.dumps({"headline": "hi", "items": [],
                              "logic_version": pipeline.DIGEST_LOGIC_VERSION}),
             "copilot:auto", db.now_iso()))
        conn.commit()

    db.set_setting("llm_provider", "copilot")
    assert pipeline.cached_digest(day) is not None, "same provider -- the cache is fine"

    db.set_setting("llm_provider", "anthropic")
    assert pipeline.cached_digest(day) is None, "provider changed -- do not serve a stale stamp"
