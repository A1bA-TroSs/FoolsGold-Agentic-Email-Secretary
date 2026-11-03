"""Real-mail fixes (2026-11-03): what Danny's own Mac showed.

His Mail folders are Korean (받은 편지함, 정크 메일, 보낸 편지함), often NFD on
disk. With English-only folder lists the app cached junk and his sent mail as
inbox mail, missed his Sent folder, and To respond listed seven emails none of
which he owed. Each case below is one of those seven, or the rule that keeps a
real ask from being lost by the fix."""
from __future__ import annotations

import json
import unicodedata
from datetime import date, datetime, timedelta, timezone

import pytest

from app import db, pipeline, priority, to_respond
from app.compose import identity
from app.sources import applemail

ME = "me@uni.edu"
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
nfd = lambda s: unicodedata.normalize("NFD", s)


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.set_setting("user_address", ME)
    return tmp_path


def mail(**kw):
    base = dict(id="a", subject="Draft", from_name="P", from_address="prof@uni.edu",
                to_recipients=f'["{ME}"]', cc_recipients="[]",
                received_at=(NOW - timedelta(days=1)).isoformat(), is_answered=0,
                body_text="Could you send me the draft?", body_preview="")
    base.update(kw)
    return base


def why(email, **kw):
    return to_respond.reason(email, ME, now=NOW, **kw)


# --------------------------------------------------------------- folder kinds

@pytest.mark.parametrize("name, kind", [
    ("정크 메일", "junk"), (nfd("정크 메일"), "junk"), ("보낸 편지함", "sent"), (nfd("보낸 편지함"), "sent"),
    ("보낸편지함", "sent"), ("임시 보관함", "drafts"), ("휴지통", "trash"), ("지운 편지함", "trash"),
    ("迷惑メール", "junk"), ("送信済み", "sent"), ("ゴミ箱", "trash"),
    ("垃圾邮件", "junk"), ("已发送", "sent"), ("草稿", "drafts"),
    ("Junk", "junk"), ("Sent Messages", "sent"), ("  sent   items ", "sent"), ("Deleted Messages", "trash"),
    ("받은 편지함", None), ("INBOX", None), ("Projects", None), ("Sentences", None),
])
def test_mailbox_kind(name, kind):
    assert applemail.mailbox_kind(name) == kind


def test_a_korean_inbox_counts_as_the_inbox_and_junk_never_does():
    assert applemail._wanted_mailbox([nfd("받은 편지함")], inbox_only=True)
    assert not applemail._wanted_mailbox([nfd("정크 메일")], inbox_only=False)
    assert not applemail._wanted_mailbox(["Projects", "보낸 편지함"], inbox_only=False)


def test_the_sent_walk_finds_a_korean_sent_folder_and_fills_the_sent_log(fresh):
    root = fresh / "Mail" / "V10"
    msgs = root / "acct" / f"{nfd('보낸 편지함')}.mbox" / "Messages"
    msgs.mkdir(parents=True)
    (msgs / "7.emlx").write_bytes(
        b"200\nFrom: Danny Park <me@uni.edu>\nTo: Cindy <cindy@uni.edu>\n"
        b"Subject: Re: [CO-OP] SID 20792960 approval\nDate: Wed, 29 Oct 2026 15:27:00 +0800\n\nThanks\n")
    assert applemail.build_correspondent_affinity(root, force=True) == {"cindy@uni.edu": 1}
    after = db.sent_after()
    assert after == {to_respond.subject_key("[CO-OP] SID 20792960 approval"):
                     [("2026-10-29T07:27:00+00:00", {"cindy@uni.edu"}, False)]}


# ------------------------------------------------------------- purge and log

def stored(i, folder, frm="x@uni.edu", subject="Hello there everyone", name="S"):
    return dict(id=i, conversation_id=None, subject=subject, from_name=name, from_address=frm,
                to_recipients='["cindy@uni.edu"]', cc_recipients="[]",
                received_at=NOW.isoformat(), is_read=0, is_answered=0, is_flagged=0,
                has_attachments=0, importance="normal", web_link="", folder=folder,
                body_preview="", body_text="SECRETBODY", body_html="", synced_at=db.now_iso())


def test_purge_drops_junk_and_sent_rows_and_keeps_inbox_and_user_folders(fresh):
    db.upsert_emails([stored("inbox", "받은 편지함"), stored("proj", "Projects/2026"),
                      stored("mine", "Projects/보관"),
                      stored("junk", nfd("정크 메일")), stored("trash", "휴지통"),
                      stored("sent", nfd("보낸 편지함"), frm=ME, name="Danny Park",
                             subject="Re: Visa documents for spring")])
    with db.connect() as conn:
        conn.execute("INSERT INTO classifications (email_id, bucket) VALUES ('junk', 'action')")
        conn.commit()
    assert db.purge_excluded_folders() == 3
    with db.connect() as conn:
        assert {r[0] for r in conn.execute("SELECT id FROM emails")} == {"inbox", "proj", "mine"}
        assert conn.execute("SELECT COUNT(*) FROM classifications WHERE email_id='junk'").fetchone()[0] == 0
        log = [dict(r) for r in conn.execute("SELECT * FROM sent_log")]
    assert len(log) == 1
    text = json.dumps(log, ensure_ascii=False)
    for leak in ("Visa", "visa", "SECRETBODY", "Danny"):
        assert leak not in text
    assert json.loads(log[0]["recipients"]) == ["cindy@uni.edu"]
    assert db.purge_excluded_folders() == 0           # idempotent


def test_purge_leaves_what_the_user_made(fresh):
    db.upsert_emails([stored("junk", "정크 메일")])
    with db.connect() as conn:
        conn.execute("INSERT INTO tasks (title, due_date, status, email_id, origin) "
                     "VALUES ('Renamed by me', '2026-11-05', 'open', 'junk', 'email')")
        conn.execute("INSERT INTO tasks (title, due_date, status, email_id, origin) "
                     "VALUES ('Done one', '2026-11-01', 'done', 'junk', 'email')")
        conn.execute("INSERT INTO eval_labels (email_id, bucket) VALUES ('junk', 'noise')")
        conn.commit()
    db.purge_excluded_folders()
    with db.connect() as conn:
        tasks = {r[0]: r[1] for r in conn.execute("SELECT title, deleted_at FROM tasks")}
        labels = conn.execute("SELECT COUNT(*) FROM eval_labels").fetchone()[0]
    assert set(tasks) == {"Renamed by me", "Done one"}        # nothing deleted
    assert tasks["Renamed by me"] and not tasks["Done one"]   # open one in the removed box
    assert labels == 1


def test_reply_identities_survive_the_purge(fresh):
    db.upsert_emails([stored("sent", "보낸 편지함", frm="alias@alumni.org")])
    db.purge_excluded_folders()
    assert identity.known_identities() == [ME, "alias@alumni.org"]


# ---------------------------------------------------- answered in the thread

def sent(subject, hours_ago, to=("cindy@uni.edu",)):
    return {to_respond.subject_key(subject): [((NOW - timedelta(hours=hours_ago)).isoformat(), set(to),
                                               to_respond.is_forward(subject))]}


def test_a_reply_in_another_branch_answers_the_thread():
    email = mail(subject="RE: [CO-OP] SID 20792960 approval", from_address="dion@uni.edu",
                 body_text="Could you confirm the form?")
    assert why(email) == "question"
    assert why(email, sent_after=sent("Re: [CO-OP] SID 20792960 approval", 2)) is None
    # A reply sent before the mail arrived answers nothing.
    assert why(email, sent_after=sent("Re: [CO-OP] SID 20792960 approval", 72)) == "question"


def test_a_generic_subject_joins_only_when_the_reply_went_to_someone_on_it():
    email = mail(subject="Meeting request", from_address="other@x.com", body_text="Can we meet Friday?")
    assert why(email, sent_after=sent("Re: Meeting request", 1, to=("someone@else.com",))) == "question"
    assert why(email, sent_after=sent("Re: Meeting request", 1, to=("other@x.com",))) is None


def test_a_forward_is_not_a_reply():
    ask = mail(subject="Draft paper for NeurIPS submission review", from_address="prof@uni.edu",
               body_text="Could you review the draft?")
    assert why(ask, sent_after=sent("Fwd: Draft paper for NeurIPS submission review", 1,
                                    to=("friend@gmail.com",))) == "question"
    assert why(ask, sent_after=sent("Fwd: Draft paper for NeurIPS submission review", 1,
                                    to=("prof@uni.edu",))) is None


def test_a_year_is_not_an_id():
    ask = mail(subject="Graduation 2026", from_address="office@uni.edu", body_text="Can you confirm?")
    assert why(ask, sent_after=sent("Re: Graduation 2026", 1, to=("me.personal@gmail.com",))) == "question"


@pytest.mark.parametrize("subject", ["Question", "Re: hi", "Meeting", "회의", "Fwd: Hello", "[EXTERNAL] RE: Hi"])
def test_short_subjects_never_join_threads(subject):
    assert to_respond.subject_key(subject) is None


def test_reply_prefixes_in_any_language_share_a_key():
    keys = {to_respond.subject_key(s) for s in (
        "Visa documents for spring", "Re: Visa documents for spring", "RE: Re: visa  documents for spring",
        "회신: Visa documents for spring", "답장: Fwd: Visa documents for spring",
        "[EXTERNAL] RE: Visa documents for spring", "[외부] 회신: Visa documents for spring")}
    assert len(keys) == 1 and None not in keys


# ----------------------------------------------------------- not an ask

@pytest.mark.parametrize("case, email", [
    ("do not reply", mail(subject="Official Document Ready for Collection", from_address="arcollect@ust.hk",
                          body_text="Please do not reply to this email as this mailbox is unmonitored. "
                                    "Please confirm your pickup time.")),
    ("korean do not reply", mail(subject="성적 안내", body_text="본 메일은 발신 전용입니다. 확인 부탁드립니다.")),
    ("korean notice", mail(subject="성적 안내", body_text="이 메일에는 회신하지 마십시오. 확인 바랍니다.")),
    ("dear user", mail(subject="HKUST Daily Event Alert", from_address="calendar@ust.hk",
                       body_text="HKUST Ucalendar\nDear USER, Don't miss out. Register now and reply to RSVP.")),
    ("dear students", mail(subject="Exchange info session", body_text="Dear students,\nPlease register by Friday.")),
    ("dear user, title line first", mail(subject="Weekly Event Summary",
                                         body_text="HKUST Ucalendar\nDear USER,\nPlease reply to RSVP.")),
    ("newsletter", mail(subject="Boundless Oct newsletter", from_address="ustboundless@ust.hk",
                        body_text="Read the stories. Can you spare a minute for our survey?")),
    ("html placeholder", mail(subject="Career Center Registration - Confirmation",
                              body_text="To view the message, please use an HTML compatible email viewer!")),
])
def test_broadcasts_wearing_a_direct_address(case, email):
    assert why(email) is None, case


@pytest.mark.parametrize("body", [
    "Hi Dion,\n\nThanks for the update. Danny, can you send me the numbers by Friday?",
    "Dion, Danny, Kenneth,\nPlease each send me your section by Friday.",
    "Dion: please book the room.\nDanny: please prepare the slides.",
    "Dion, Danny,\n\nCould you both send me your slides by Friday?",
    "Dion, please follow up with the vendor.\nDanny, could you send the data to me by Friday?",
])
def test_a_mail_that_also_asks_the_user_is_kept(body):
    email = mail(subject="Workshop plan", body_text=body, to_recipients=f'["{ME}", "dion.lee@uni.edu"]')
    assert why(email) is not None, body


def test_an_ask_named_to_another_recipient_is_not_on_the_user():
    email = mail(subject="RE: [CO-OP] SID 20792960", body_text="Dion, Please follow-up : )\nCould you check?",
                 to_recipients=f'["{ME}", "dion.liu@uni.edu"]')
    assert why(email) is None
    alone = mail(subject="RE: [CO-OP] SID 20792960", body_text="Dion, Please follow-up : )\nCould you check?")
    assert why(alone) == "question"           # no Dion on the mail: kept


@pytest.mark.parametrize("body", [
    "Sir,\nCould you sign the form?", "Professor,\nCould you sign the form?",
    "Yoonsung,\nCould you send the draft?", "Danny,\nCould you send the draft?",
    "Team,\nCould you send your slides?", "Reminder: could you send the form?",
    "Hi Kenneth and Danny,\nCould you both review?",
    "선배님,\n이 부분 설명해주실 수 있을까요?", "팀장님,\n보고서 검토 부탁드립니다.",
    "윤성님,\n내일까지 자료를 보내주시기 바랍니다.",
])
def test_titles_and_names_never_drop_an_ask(body):
    email = mail(subject="요청 Request", body_text=body, to_recipients=f'["{ME}", "kenneth.wong@uni.edu"]')
    assert why(email) is not None, body


@pytest.mark.parametrize("email, expected", [
    (mail(body_text="Thanks,\nCould you send the deck by Friday?"), "question"),
    (mail(body_text="Hi all and Danny,\nCan you join?"), "question"),
    (mail(body_text="Please do not reply all. Could you send me your slot privately?"), "question"),
    (mail(body_text="Don't respond to the vendor yet. Can you check the quote with me first?"), "question"),
    (mail(body_text="The attached report was automatically generated. Can you review it before Monday?"), "question"),
    (mail(body_text="Hi team,\nCould each of you send me your slides?"), "question"),
    (mail(body_text="Danny, can you reply to this one?\n\nDear all,\nThe event is on."), "question"),
    (mail(body_text="Dear all,\nCould you send me your slides?",
          to_recipients=f'["{ME}", "b@uni.edu", "c@uni.edu"]'), "question"),
    (mail(subject="자료", body_text="금요일까지 회신이 불가하면 알려주세요."), "request"),
    (mail(body_text="Team, please send your slides by Friday.",
          to_recipients=f'["{ME}", "team@uni.edu"]'), "request"),
    (mail(body_text="Dear colleagues,\nCould you send me your availability?",
          to_recipients=f'["{ME}", "b@uni.edu"]'), "question"),
    (mail(body_text="Our system sends this is an automated message notice weekly; can you check why?"), "question"),
    (mail(subject="자료 요청", body_text="안녕하세요.\n내일까지 발표 자료를 보내주시기 바랍니다."), "request"),
    (mail(body_text="Please reply by Friday with your slot."), "request"),
])
def test_real_asks_still_show(email, expected):
    assert why(email) == expected


def test_dear_all_to_the_user_alone_is_a_broadcast():
    assert why(mail(body_text="Dear all,\nPlease register by Friday.")) is None


# ------------------------------------------------------- deadlines by mail date

def test_a_yearless_date_is_read_against_the_day_the_mail_came():
    old = mail(subject="Switch to Zoom: UG seminar", body_text="The seminar on Wed Nov 2 moves to Zoom.",
               received_at="2022-10-30T09:00:00+00:00")
    assert pipeline._structural_classifications([old], ME)[0].deadline == "2022-11-02"
    assert priority.mail_day({"received_at": "2022-10-30T09:00:00+00:00"}) == date(2022, 10, 30)
    assert priority.mail_day({"received_at": ""}) is None


def test_a_korean_mail_tree_end_to_end(fresh):
    """Sync a fake Apple Mail store with NFD Korean folders, then read To respond."""
    import asyncio
    root = fresh / "Mail" / "V10"
    acct = root / "acct"
    when = "Mon, 02 Nov 2026 09:00:00 +0000"

    def put(folder, n, head):
        d = acct / f"{nfd(folder)}.mbox" / "Messages"
        d.mkdir(parents=True, exist_ok=True)
        raw = (head + f"Date: {when}\nMessage-ID: <{folder}{n}@x>\n\n").encode()
        body = raw + b"Could you send me the draft by Friday?\n"
        (d / f"{n}.emlx").write_bytes(str(len(body)).encode() + b"\n" + body)

    put("받은 편지함", 1, f"From: Prof <prof@uni.edu>\nTo: {ME}\nSubject: Draft for the workshop paper\n")
    put("받은 편지함", 2, f"From: Lab <lab@uni.edu>\nTo: {ME}\nSubject: Re: [LAB] 20260 budget sheet\n")
    put("정크 메일", 3, f"From: Ads <ads@shop.com>\nTo: {ME}\nSubject: Could you review our offer\n")
    put("보낸 편지함", 4, f"From: Me <{ME}>\nTo: lab@uni.edu\nSubject: Re: [LAB] 20260 budget sheet\n"
                       "Date: Tue, 03 Nov 2026 08:00:00 +0000\n")
    db.set_setting("applemail_root", str(root))
    from app.sources.applemail import AppleMailSource
    asyncio.run(AppleMailSource().sync(days=3650))
    with db.connect() as conn:
        folders = {r[0] for r in conn.execute("SELECT folder FROM emails")}
    assert folders == {"받은 편지함"}
    assert to_respond.subject_key("[LAB] 20260 budget sheet") in db.sent_after()
