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


# ------------------------------------------------- the briefing refills itself

def _db(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()
    return db


def _seed(db, n, *, start=0, deadline_from=1):
    """n candidates, each with a deadline further out than the last."""
    rows, classes = [], []
    for i in range(start, start + n):
        eid = f"e{i}"
        rows.append(dict(
            id=eid, conversation_id=None, subject=f"Subject {i}", from_name="Dept",
            from_address=f"a{i}@uni.edu", to_recipients="[]", cc_recipients="[]",
            received_at="2026-09-20T09:00:00+00:00", is_read=0, is_answered=0,
            is_flagged=0, has_attachments=0, importance="normal", web_link="",
            folder="INBOX", body_preview="", body_text="", body_html="",
            source_path="", synced_at=db.now_iso()))
        classes.append(dict(
            email_id=eid, bucket="action",
            deadline=(TODAY + timedelta(days=deadline_from + i)).isoformat(),
            rationale="[]", score=100 - i, matched="", model="structural",
            source="structural", created_at=db.now_iso()))
    db.upsert_emails(rows)
    for rec in classes:
        db.save_classification(rec)
    return [r["id"] for r in rows]


def test_finishing_an_item_pulls_the_next_one_in(tmp_path, monkeypatch):
    """The defect: a briefing frozen at seven can only ever shrink.

    Clearing four left three until tomorrow, and since the seven nearest
    deadlines are all seven slots can hold, nothing further out than a day or
    two could ever appear no matter how much of the list you cleared.
    """
    db = _db(tmp_path, monkeypatch)
    ids = _seed(db, 12)

    _, rows = pipeline._fallback_agenda(pipeline._digest_candidates())
    assert len(rows) == pipeline.DIGEST_TARGET_ITEMS
    first_seven = [r["email_id"] for r in rows]

    digest = pipeline._with_current_verdicts({"items": list(rows), "headline": ""})
    assert [r["email_id"] for r in digest["items"]] == first_seven, "nothing decided yet"

    db.set_feedback(first_seven[0], "done")
    db.set_feedback(first_seven[1], "not_relevant")
    served = pipeline._with_current_verdicts({"items": list(rows), "headline": ""})

    got = [r["email_id"] for r in served["items"]]
    assert len(got) == pipeline.DIGEST_TARGET_ITEMS, "the gap should be refilled"
    assert first_seven[0] not in got and first_seven[1] not in got
    # And the replacements come from the queue, in its order -- not at random.
    assert got[-2:] == ids[7:9]


def test_pinning_is_not_finishing(tmp_path, monkeypatch):
    """`pinned` says "this matters", which is the opposite of "I am done"."""
    db = _db(tmp_path, monkeypatch)
    _seed(db, 9)
    _, rows = pipeline._fallback_agenda(pipeline._digest_candidates())
    target = rows[0]["email_id"]
    db.set_feedback(target, "pinned")
    served = pipeline._with_current_verdicts({"items": list(rows), "headline": ""})
    assert target in [r["email_id"] for r in served["items"]]


def test_a_refill_never_duplicates_a_row_already_on_the_list(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _seed(db, 20)
    _, rows = pipeline._fallback_agenda(pipeline._digest_candidates())
    db.set_feedback(rows[0]["email_id"], "done")
    served = pipeline._with_current_verdicts({"items": list(rows), "headline": ""})
    got = [r["email_id"] for r in served["items"]]
    assert len(got) == len(set(got))


def test_a_short_queue_shrinks_rather_than_inventing_rows(tmp_path, monkeypatch):
    """Nothing left to promote is a real answer. Padding would not be."""
    db = _db(tmp_path, monkeypatch)
    _seed(db, 3)
    _, rows = pipeline._fallback_agenda(pipeline._digest_candidates())
    assert len(rows) == 3
    db.set_feedback(rows[0]["email_id"], "done")
    served = pipeline._with_current_verdicts({"items": list(rows), "headline": ""})
    assert len(served["items"]) == 2


def test_the_headline_count_follows_the_rows(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _seed(db, 3)
    headline, rows = pipeline._fallback_agenda(pipeline._digest_candidates())
    db.set_feedback(rows[0]["email_id"], "done")
    served = pipeline._with_current_verdicts({"items": list(rows), "headline": headline})
    assert served["headline"]["vars"]["n"] == len(served["items"])


def test_a_model_written_headline_is_left_alone(tmp_path, monkeypatch):
    """It is prose about the day. Half-correcting it is worse than leaving it."""
    db = _db(tmp_path, monkeypatch)
    _seed(db, 9)
    _, rows = pipeline._fallback_agenda(pipeline._digest_candidates())
    db.set_feedback(rows[0]["email_id"], "done")
    served = pipeline._with_current_verdicts(
        {"items": list(rows), "headline": "오늘 마감 1건, 이번 주 2건."})
    assert served["headline"] == "오늘 마감 1건, 이번 주 2건."


def test_an_empty_briefing_topped_up_stops_saying_nothing_is_waiting(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _seed(db, 4)
    served = pipeline._with_current_verdicts(
        {"items": [], "headline": {"key": "nothingWaiting"}})
    assert len(served["items"]) == 4
    assert served["headline"]["key"] == "agendaHeadline"
