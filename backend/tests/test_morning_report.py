"""F6: the morning report, one card where the briefing and the recap were two.

Pinned here: replies owed are exactly the To respond list; deadlines keep to
their window at both edges; "missed" is important mail still unopened after a
day; no email shows twice; nothing calls a model; and reading the report moves
no window and marks nothing read."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, recap, report, to_respond

ME = "me@x.com"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.set_setting("user_address", ME)
    db.set_setting("llm_provider", "none")
    from app.main import app
    with TestClient(app) as c:
        yield c


NOW = datetime.now(timezone.utc)
TODAY = date.today()


def put(i, *, bucket="fyi", deadline=None, hours=5, read=0, frm=None, to='["list@uni.edu"]',
        subject=None, body="A notice.", conversation=None):
    db.upsert_emails([dict(
        id=i, conversation_id=conversation, subject=subject or f"Subject {i}", from_name="S",
        from_address=frm or f"{i}@uni.edu", to_recipients=to, cc_recipients="[]",
        received_at=(NOW - timedelta(hours=hours)).isoformat(), is_read=read, is_answered=0,
        is_flagged=0, has_attachments=0, importance="normal", web_link="", folder="INBOX",
        body_preview=body[:40], body_text=body, body_html="", synced_at=db.now_iso())])
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO classifications (email_id, bucket, deadline, score, source) "
                     "VALUES (?, ?, ?, 1, 'structural')", (i, bucket, deadline))
        conn.commit()


def due(days):
    return (TODAY + timedelta(days=days)).isoformat()


def ids(section):
    return [r["email_id"] for r in section["items"]]


def all_ids(rep):
    out = ids(rep["replies"]) + ids(rep["deadlines"]) + ids(rep["missed"])
    for s in rep["pile"]["sections"]:
        out += [l["email_id"] for l in s["lines"]]
    for s in (rep["pile"].get("bulk") or {}).get("senders", []):
        out += [l["email_id"] for l in s["lines"]]
    return out


def test_replies_are_the_to_respond_list(client):
    put("q1", to=f'["{ME}"]', body="Could you send the draft?", hours=3)
    put("q2", to=f'["{ME}"]', body="Please confirm the room by Friday.", hours=1)
    rep = client.get("/api/mail/report").json()
    owed = to_respond.owed()
    assert ids(rep["replies"]) == [o["id"] for o in owed] == ["q2", "q1"]
    assert [r["reason"] for r in rep["replies"]["items"]] == ["request", "question"]


@pytest.mark.parametrize("days, shown, when", [
    (-3, False, None), (-2, True, "overdue"), (0, True, "today"),
    (1, True, "tomorrow"), (7, True, "this_week"), (8, False, None),
])
def test_deadline_window_edges(client, days, shown, when):
    put("d", deadline=due(days))
    rows = client.get("/api/mail/report").json()["deadlines"]["items"]
    assert [r["email_id"] for r in rows] == (["d"] if shown else [])
    if shown:
        assert rows[0]["when"] == when


def test_deadlines_soonest_first_and_closed_mail_out(client):
    put("late", deadline=due(5))
    put("soon", deadline=due(1))
    put("over", deadline=due(-1))
    put("noise", bucket="noise", deadline=due(1))
    put("done", deadline=due(2))
    put("muted", deadline=due(2), frm="muted@uni.edu")
    put("snoozed", deadline=due(2))
    put("woke", deadline=due(3))
    db.set_feedback("done", "done")
    db.set_feedback("snoozed", "snoozed", (NOW + timedelta(days=1)).isoformat())
    db.set_feedback("woke", "snoozed", (NOW - timedelta(hours=1)).isoformat())
    with db.connect() as conn:
        conn.execute("INSERT INTO muted_senders (address) VALUES ('muted@uni.edu')")
        conn.commit()
    rows = client.get("/api/mail/report").json()["deadlines"]["items"]
    assert [r["email_id"] for r in rows] == ["over", "soon", "woke", "late"]


@pytest.mark.parametrize("case, kw, missed", [
    ("action, two days unopened", dict(bucket="action", hours=48), True),
    ("must-see kind, not action", dict(bucket="fyi", hours=48, subject="Final exam schedule"), True),
    ("under a day", dict(bucket="action", hours=20), False),
    ("over a week", dict(bucket="action", hours=24 * 8), False),
    ("already read", dict(bucket="action", hours=48, read=1), False),
    ("plain fyi", dict(bucket="fyi", hours=48), False),
])
def test_missed(client, case, kw, missed):
    put("m", **kw)
    rows = client.get("/api/mail/report").json()["missed"]["items"]
    assert ([r["email_id"] for r in rows] == ["m"]) is missed, case


def test_no_email_twice_and_the_pile_drops_shown_groups(client):
    # An owed reply with a deadline: in replies only.
    put("r", to=f'["{ME}"]', body="Can you review this?", deadline=due(1), bucket="action")
    # A dated notice sent twice: one deadline row, and its copy is not in the pile.
    put("n1", deadline=due(2), subject="Visa documents due", conversation="c1", hours=2)
    put("n2", deadline=due(2), subject="Visa documents due", conversation="c1", hours=1)
    put("plain", subject="Campus newsletter", hours=2)
    rep = client.get("/api/mail/report").json()
    every = all_ids(rep)
    assert len(every) == len(set(every))
    assert "r" in ids(rep["replies"]) and "r" not in ids(rep["deadlines"])
    assert "plain" in every
    shown = set(ids(rep["deadlines"]))
    assert shown & {"n1", "n2"}
    pile = [l for s in rep["pile"]["sections"] for l in s["lines"]]
    assert not any(set(l["email_ids"]) & {"n1", "n2"} for l in pile)
    assert rep["pile"]["lines"] == len(pile) + (rep["pile"]["bulk"] or {}).get("count", 0)


def test_headline_counts_and_all_clear(client):
    rep = client.get("/api/mail/report").json()
    assert rep["headline"] == {"key": "reportAllClear",
                               "vars": {"replies": 0, "due": 0, "missed": 0, "new": 0}}
    put("d", deadline=due(0))
    put("m", bucket="action", hours=30)
    head = client.get("/api/mail/report").json()["headline"]
    assert head["key"] == "reportHeadline"
    assert head["vars"]["due"] == 1 and head["vars"]["missed"] == 1


def test_no_model_is_called(client, monkeypatch):
    from app import pipeline
    from app.llm import registry

    def boom(*a, **k):
        raise AssertionError("the report must not call a model")
    monkeypatch.setattr(pipeline, "get_provider", boom)
    monkeypatch.setattr(registry, "get_provider", boom)
    db.set_setting("llm_provider", "ollama")
    put("d", deadline=due(1))
    r = client.get("/api/mail/report")
    assert r.status_code == 200 and ids(r.json()["deadlines"]) == ["d"]


def test_reading_the_report_moves_no_window_and_marks_nothing_read(client):
    put("p", hours=2)
    client.get("/api/mail/report")
    client.get("/api/mail/report")
    assert db.get_setting(recap.SEEN_KEY, "") in ("", None)
    with db.connect() as conn:
        assert conn.execute("SELECT is_read FROM emails WHERE id='p'").fetchone()[0] == 0
    client.post("/api/mail/recap/seen")
    assert "p" not in all_ids(client.get("/api/mail/report").json())


def test_open_rate_counts_days_not_clicks(client):
    client.get("/api/mail/report")
    assert report.open_rate() == {"opened_days": 0, "active_days": 1}
    for _ in range(3):
        client.post("/api/mail/report/opened")
    assert report.open_rate() == {"opened_days": 1, "active_days": 1}
    report.mark_opened(TODAY + timedelta(days=1))
    assert report.open_rate()["opened_days"] == 2
    card = client.get("/api/quality/export").json()
    assert card["report"] == {"opened_days": 2, "active_days": 1}
