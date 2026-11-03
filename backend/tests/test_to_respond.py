"""A1: "To respond", the replies the user owes.

Each condition of the rule is pinned on its own, because a list that lets in
one newsletter a day stops being read, and a list that misses a plain Korean
request fails the user the study is for."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, to_respond

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
ME = "me@x.com"


def mail(**kw):
    base = dict(id="a", subject="Quick question", from_name="P", from_address="prof@uni.edu",
                to_recipients='["me@x.com"]', cc_recipients="[]",
                received_at=(NOW - timedelta(days=1)).isoformat(), is_answered=0,
                body_text="Could you send me the draft?", body_preview="")
    base.update(kw)
    return base


def why(email, **kw):
    return to_respond.reason(email, ME, now=NOW, **kw)


@pytest.mark.parametrize("email, expected", [
    (mail(), "question"),
    (mail(body_text="Please confirm your slot by Friday."), "request"),
    (mail(subject="자료 요청", body_text="내일까지 발표 자료를 보내주시기 바랍니다."), "request"),
    (mail(subject="회의", body_text="다음 주 회의 시간 괜찮으신가요？"), "question"),
])
def test_direct_asks_from_people_are_owed(email, expected):
    assert why(email) == expected


def test_an_action_bucket_counts_when_the_text_has_no_cue():
    email = mail(body_text="The committee meets Tuesday.")
    assert why(email) is None
    assert why(email, bucket="action") == "action"


@pytest.mark.parametrize("case, email, kw", [
    ("answered", mail(is_answered=1), {}),
    ("cc only", mail(to_recipients='["team@uni.edu"]', cc_recipients='["me@x.com"]'), {}),
    ("a list", mail(to_recipients='["all-students@uni.edu"]'), {}),
    ("no-reply", mail(from_address="no-reply@service.com"), {}),
    ("notifications", mail(from_address="notifications@github.com"), {}),
    ("marketing", mail(subject="Last chance: 50% off, reply now?"), {}),
    ("korean ad footer", mail(body_text="이벤트에 참여하시겠어요? 수신거부"), {}),
    ("the user", mail(from_address="ME@x.com"), {}),
    ("muted", mail(), {"muted": True}),
    ("done", mail(), {"verdict": "done"}),
    ("not relevant", mail(), {"verdict": "not_relevant"}),
    ("older than two weeks", mail(received_at=(NOW - timedelta(days=15)).isoformat()), {}),
    ("no date", mail(received_at=""), {}),
    ("a statement", mail(body_text="Here are the notes from today."), {}),
    ("question only in the quote", mail(body_text="Thanks, all set.\n\nOn Mon, X wrote:\n> Can you confirm?"), {}),
])
def test_what_is_never_owed(case, email, kw):
    assert why(email, **kw) is None, case


def test_no_user_address_means_nothing_is_owed():
    assert to_respond.reason(mail(), "", now=NOW) is None


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.set_setting("user_address", ME)
    from app.main import app
    with TestClient(app) as c:
        yield c


def stored(i, days=1, **kw):
    now = datetime.now(timezone.utc)
    row = dict(id=f"m{i}", conversation_id=None, subject="Quick question", from_name="P",
               from_address=f"p{i}@uni.edu", to_recipients='["me@x.com"]', cc_recipients="[]",
               received_at=(now - timedelta(days=days)).isoformat(), is_read=0, is_answered=0,
               is_flagged=0, has_attachments=0, importance="normal", web_link="", folder="INBOX",
               body_preview="", body_text="Can you join on Friday?", body_html="",
               synced_at=db.now_iso())
    row.update(kw)
    return row


def test_the_list_is_newest_first_and_clears_when_answered(client):
    db.upsert_emails([stored(1, days=3), stored(2, days=1), stored(3, days=2, is_answered=1)])
    body = client.get("/api/mail/to-respond").json()
    assert [i["id"] for i in body["items"]] == ["m2", "m1"]
    assert body["count"] == 2 and body["items"][0]["reason"] == "question"
    db.upsert_emails([stored(2, days=1, is_answered=1)])   # replied in Mail; next sync
    assert [i["id"] for i in client.get("/api/mail/to-respond").json()["items"]] == ["m1"]


def test_the_mail_list_view_and_rail_count_agree(client):
    db.upsert_emails([stored(1), stored(2), stored(3, from_address="no-reply@x.com")])
    db.set_feedback("m2", "done")
    data = client.get("/api/mail", params={"to_respond": "true"}).json()
    assert [i["id"] for i in data["items"]] == ["m1"]
    assert data["items"][0]["respond_reason"] == "question"
    assert data["counts"]["to_respond"] == 1
    # The reason chip belongs to this view only; the ranked list is unchanged.
    plain = client.get("/api/mail").json()["items"]
    assert all("respond_reason" not in i for i in plain)
    assert {i["id"] for i in plain} >= {"m1", "m3"}


# Round 1 review: a "?" in a link or a signature asks nothing; a question in the
# subject, a polite Korean ending and "Can you ..." without "?" all ask.
@pytest.mark.parametrize("case, email, expected", [
    ("link", mail(body_text="Here is the doc: https://docs.google.com/d/1?usp=sharing"), None),
    ("signature link", mail(body_text="Notes attached.\n\nBest,\nKim\nhttps://uni.edu/p?id=3"), None),
    ("subject question", mail(subject="Are you free Tuesday?", body_text="See below."), "question"),
    ("reply subject repeats", mail(subject="Re: Are you free Tuesday?", body_text="Noted."), None),
    ("polite korean", mail(subject="일정", body_text="다음 주 화요일 가능하실까요."), "question"),
    ("can you, no mark", mail(body_text="Can you send the slides by Friday."), "request"),
])
def test_round_one_cases(case, email, expected):
    assert why(email) == expected, case


def test_an_active_snooze_hides_the_mail_until_it_ends():
    later = (NOW + timedelta(days=1)).isoformat()
    earlier = (NOW - timedelta(hours=1)).isoformat()
    assert why(mail(), verdict="snoozed", snooze_until=later) is None
    assert why(mail(), verdict="snoozed", snooze_until=earlier) == "question"


# Round 2 review: a sentence that starts with "Thanks" is not a signature;
# handing something over is not an ask; a plain Korean statement is not a
# question; "not important" closes a mail as it does everywhere else.
@pytest.mark.parametrize("case, email, expected", [
    ("thanks opener", mail(body_text="Hi,\nThanks for yesterday.\nCould you send the deck by Friday?"), "question"),
    ("thanks so much", mail(body_text="Hi Alex,\nThanks so much for the update.\nPlease confirm the room booking by Friday."), "request"),
    ("best news", mail(body_text="Hi,\nBest news: we got the grant!\nCan you confirm the budget?"), "question"),
    ("sign-off then link", mail(body_text="See you then.\nThanks,\nKim\nhttps://uni.edu/p?id=3"), None),
    ("korean sign-off", mail(subject="자료", body_text="자료 공유드립니다.\n감사합니다.\nhttps://x.kr/a?b=1"), None),
    ("please find attached", mail(body_text="Please find attached the minutes from today."), None),
    ("please see below", mail(body_text="FYI, please see the notes below.\nNo action needed."), None),
    ("korean statement", mail(subject="일정", body_text="저는 내일 회사에 가요."), None),
    ("korean polite question", mail(subject="일정", body_text="내일 회의 괜찮으신가요."), "question"),
])
def test_round_two_cases(case, email, expected):
    assert why(email) == expected, case


def test_not_important_closes_the_mail():
    assert why(mail(), verdict="not_important") is None
