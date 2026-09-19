"""The completed box: reviewing what you ticked off, and undoing a mistake.

Muting has a box you can open and reverse. Completing did not: the six-second
undo toast was the only way back, so an accidental tick was permanent the
moment you looked away. These tests pin the two properties that make the box
worth having -- that it finds the row, and that muting the sender afterwards
does not make it disappear."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


def _email(eid, address, subject, received):
    return dict(id=eid, conversation_id=None, subject=subject, from_name="Someone",
                from_address=address, to_recipients="[]", cc_recipients="[]",
                received_at=received, is_read=1, is_answered=0, is_flagged=0,
                has_attachments=0, importance="normal", web_link="", folder="INBOX",
                body_preview="", body_text="", body_html="", synced_at=db.now_iso())


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.upsert_emails([
        _email("a", "prof@uni.edu", "Grade appeal", "2026-08-20T09:00:00+00:00"),
        _email("b", "news@shop.com", "Half price", "2026-08-21T09:00:00+00:00"),
        _email("c", "friend@mail.com", "Lunch?", "2026-08-22T09:00:00+00:00"),
    ])
    with TestClient(app) as c:
        yield c


def test_the_box_lists_only_what_was_completed(client):
    db.set_feedback("a", "done")
    db.set_feedback("b", "pinned")
    ids = [i["id"] for i in client.get("/api/mail", params={"verdict": "done"}).json()["items"]]
    assert ids == ["a"], "a pinned row is not a completed row"


def test_an_unknown_verdict_is_refused_rather_than_returning_everything(client):
    """A filter that silently matches nothing -- or worse, everything -- is how
    an empty box gets read as 'you have completed nothing'."""
    assert client.get("/api/mail", params={"verdict": "finished"}).status_code == 400


def test_muting_the_sender_does_not_erase_the_record(client):
    """The whole point of the box is that it remembers. Every other list hides
    muted senders; this one must not, or the row you came to undo is gone."""
    db.set_feedback("b", "done")
    db.mute_sender("news@shop.com")
    ids = [i["id"] for i in client.get("/api/mail", params={"verdict": "done"}).json()["items"]]
    assert ids == ["b"]
    # and it is still hidden from the ordinary list, which is the other half
    ordinary = [i["id"] for i in client.get("/api/mail").json()["items"]]
    assert "b" not in ordinary


def test_rows_carry_when_they_were_decided_not_when_they_arrived(client):
    """The box is sorted by the decision, so the decision has to be on the row
    -- and the most recent one has to come first, because that is the one the
    user is here to take back."""
    db.set_feedback("a", "done")
    db.set_feedback("c", "done")
    items = client.get("/api/mail", params={"verdict": "done", "sort": "decided"}).json()["items"]
    assert [i["id"] for i in items] == ["c", "a"], "newest decision first"
    assert all(i["decided_at"] for i in items)


def test_clearing_the_feedback_empties_the_box(client):
    db.set_feedback("a", "done")
    client.delete("/api/mail/a/feedback")
    assert client.get("/api/mail", params={"verdict": "done"}).json()["items"] == []
