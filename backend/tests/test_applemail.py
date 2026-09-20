"""Apple Mail reader tests, run against a synthetic ~/Library/Mail tree.

The .emlx format is fixed and documented, so a fabricated store exercises the
real code paths: byte-count framing, the trailing plist, MIME part selection,
mailbox filtering and stable ids.
"""
from __future__ import annotations

import email.utils
import json
import plistlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.sources import applemail as am

FLAGS_READ_2_ATTACH = am.FLAG_READ | (2 << 10)


def write_emlx(path: Path, raw: str, flags: int = 0, extra: dict | None = None) -> Path:
    """Build a real .emlx: byte count line, message, then the plist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = raw.encode("utf-8")
    meta = {"flags": flags, "subject": "x"}
    if extra:
        meta.update(extra)
    path.write_bytes(f"{len(body)}\n".encode() + body + plistlib.dumps(meta))
    return path


SIMPLE = """From: Prof. Lee <lee@uni.edu>
To: Sam <you@example.com>
Cc: Lab list <lab@uni.edu>
Subject: Action required: final draft by Friday
Date: Tue, 25 Aug 2026 09:00:00 +0000
Message-ID: <abc123@uni.edu>
Content-Type: text/plain; charset="utf-8"

Please submit the final draft by Friday.
"""

MULTIPART = """From: Talent Team <careers@northlight.example>
To: you@example.com
Subject: Internship offer
Date: Mon, 24 Aug 2026 12:00:00 +0000
Message-ID: <offer-1@northlight.io>
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="BOUND"

--BOUND
Content-Type: text/plain; charset="utf-8"

We are delighted to offer you the internship.
--BOUND
Content-Type: text/html; charset="utf-8"

<html><body><p>We are <b>delighted</b> to offer you the internship.</p></body></html>
--BOUND--
"""

HTML_ONLY = """From: Library <library@uni.edu>
To: you@example.com
Subject: New opening hours
Date: Sun, 23 Aug 2026 08:00:00 +0000
Message-ID: <lib-9@uni.edu>
MIME-Version: 1.0
Content-Type: text/html; charset="utf-8"

<html><head><style>p{color:red}</style></head><body><p>Opening an hour earlier.</p>
<script>alert(1)</script></body></html>
"""


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    """A miniature ~/Library/Mail/V10 with two accounts and several mailboxes."""
    root = tmp_path / "V10"
    acct = root / "AAAA-1111"
    write_emlx(acct / "INBOX.mbox" / "data" / "Messages" / "1.emlx", SIMPLE, FLAGS_READ_2_ATTACH)
    write_emlx(acct / "INBOX.mbox" / "data" / "Messages" / "2.emlx", MULTIPART, 0)
    write_emlx(acct / "INBOX.mbox" / "data" / "Messages" / "3.emlx", HTML_ONLY, am.FLAG_READ)
    # These must never show up in a priority inbox.
    write_emlx(acct / "Trash.mbox" / "data" / "Messages" / "4.emlx", SIMPLE, 0)
    write_emlx(acct / "Junk.mbox" / "data" / "Messages" / "5.emlx", SIMPLE, 0)
    write_emlx(acct / "Sent Messages.mbox" / "data" / "Messages" / "6.emlx", SIMPLE, 0)
    # A nested subfolder under INBOX should still count as inbox mail.
    write_emlx(acct / "INBOX.mbox" / "Coursework.mbox" / "data" / "Messages" / "7.emlx", SIMPLE, 0)
    return root


# ------------------------------------------------------------------ format

def test_parses_bytecount_message_and_plist(tmp_path: Path):
    path = write_emlx(tmp_path / "m.emlx", SIMPLE, FLAGS_READ_2_ATTACH)
    parsed = am.parse_emlx(path)
    assert parsed.message["Subject"] == "Action required: final draft by Friday"
    assert parsed.is_read is True
    assert parsed.attachment_count == 2


def test_unread_flag_is_read_correctly(tmp_path: Path):
    parsed = am.parse_emlx(write_emlx(tmp_path / "m.emlx", SIMPLE, 0))
    assert parsed.is_read is False


def test_bytecount_frames_the_message_even_with_a_trailing_plist(tmp_path: Path):
    """The plist must never leak into the message body."""
    parsed = am.parse_emlx(write_emlx(tmp_path / "m.emlx", SIMPLE, am.FLAG_READ))
    text, _ = am.extract_bodies(parsed.message)
    assert "plist" not in text and "<?xml" not in text
    assert "final draft by Friday" in text


def test_file_without_a_bytecount_is_treated_as_plain_eml(tmp_path: Path):
    path = tmp_path / "plain.emlx"
    path.write_bytes(SIMPLE.encode())
    parsed = am.parse_emlx(path)
    assert parsed.message["Message-ID"] == "<abc123@uni.edu>"
    assert parsed.is_read is False


def test_corrupt_plist_does_not_break_the_message(tmp_path: Path):
    path = tmp_path / "bad.emlx"
    body = SIMPLE.encode()
    path.write_bytes(f"{len(body)}\n".encode() + body + b"<?xml version not actually valid")
    parsed = am.parse_emlx(path)
    assert parsed.message["Subject"].startswith("Action required")
    assert parsed.flags == 0


# ------------------------------------------------------------------ bodies

def test_prefers_the_plain_text_part_over_html(tmp_path: Path):
    parsed = am.parse_emlx(write_emlx(tmp_path / "m.emlx", MULTIPART))
    text, html = am.extract_bodies(parsed.message)
    assert text.strip() == "We are delighted to offer you the internship."
    assert "<b>delighted</b>" in html


def test_html_only_mail_still_yields_readable_text(tmp_path: Path):
    parsed = am.parse_emlx(write_emlx(tmp_path / "m.emlx", HTML_ONLY))
    text, html = am.extract_bodies(parsed.message)
    assert "Opening an hour earlier." in text
    assert "alert(1)" not in text          # script contents must not reach the model
    assert "color:red" not in text          # nor must style rules
    assert html                             # the raw html is still kept for display


# ------------------------------------------------------------------ rows

def test_row_carries_recipients_sender_and_date(tmp_path: Path):
    path = write_emlx(tmp_path / "m.emlx", SIMPLE, FLAGS_READ_2_ATTACH)
    row = am.to_row(path, path.stat().st_mtime, ["INBOX"])
    assert row["from_address"] == "lee@uni.edu"
    assert row["from_name"] == "Prof. Lee"
    assert json.loads(row["to_recipients"]) == [{"name": "Sam", "address": "you@example.com"}]
    assert json.loads(row["cc_recipients"])[0]["address"] == "lab@uni.edu"
    assert row["received_at"].startswith("2026-08-25T09:00")
    assert row["is_read"] == 1
    assert row["has_attachments"] == 1
    assert row["folder"] == "INBOX"


def test_id_is_stable_across_syncs_and_paths(tmp_path: Path):
    """Re-importing the same email must not re-classify it -- that costs money
    on a metered provider."""
    a = write_emlx(tmp_path / "a" / "m.emlx", SIMPLE)
    b = write_emlx(tmp_path / "b" / "different-name.emlx", SIMPLE)
    assert am.to_row(a, a.stat().st_mtime, ["INBOX"])["id"] == \
           am.to_row(b, b.stat().st_mtime, ["INBOX"])["id"]


def test_messages_without_a_message_id_still_get_distinct_ids(tmp_path: Path):
    raw = SIMPLE.replace("Message-ID: <abc123@uni.edu>\n", "")
    a = write_emlx(tmp_path / "a.emlx", raw)
    b = write_emlx(tmp_path / "b.emlx", raw)
    assert am.to_row(a, 0, ["INBOX"])["id"] != am.to_row(b, 0, ["INBOX"])["id"]


def test_missing_date_header_falls_back_to_file_mtime(tmp_path: Path):
    raw = SIMPLE.replace("Date: Tue, 25 Aug 2026 09:00:00 +0000\n", "")
    path = write_emlx(tmp_path / "m.emlx", raw)
    row = am.to_row(path, 1_700_000_000.0, ["INBOX"])
    assert row["received_at"].startswith("2023-11-14")


def test_importance_from_x_priority(tmp_path: Path):
    raw = SIMPLE.replace("Subject:", "X-Priority: 1\nSubject:")
    path = write_emlx(tmp_path / "m.emlx", raw)
    assert am.to_row(path, 0, ["INBOX"])["importance"] == "high"


# ------------------------------------------------------------- walking

def test_inbox_only_walk_excludes_trash_junk_and_sent(store: Path):
    chains = [chain for _, _, chain in am.iter_message_files(store, inbox_only=True)]
    assert len(chains) == 4                      # 3 in INBOX + 1 in INBOX/Coursework
    flat = {name for chain in chains for name in chain}
    assert flat == {"INBOX", "Coursework"}


def test_full_walk_still_excludes_trash_and_junk(store: Path):
    chains = [chain for _, _, chain in am.iter_message_files(store, inbox_only=False)]
    names = {name for chain in chains for name in chain}
    assert "Trash" not in names and "Junk" not in names and "Sent Messages" not in names


def test_find_mail_root_picks_the_highest_version(tmp_path: Path):
    for name in ("V7", "V9", "V10", "MailData", "NotAVersion"):
        (tmp_path / name).mkdir()
    assert am.find_mail_root(str(tmp_path / "V10")).name == "V10"


def test_find_mail_root_returns_none_for_a_missing_path():
    assert am.find_mail_root("/nonexistent/path/for/sure") is None


# ------------------------------------------------- source status ordering

def test_explicit_root_setting_wins_over_the_default_location(store, monkeypatch, tmp_path):
    """A store outside ~/Library/Mail must still work. Regression: status()
    used to bail on a missing ~/Library/Mail before ever reading the override."""
    from app import db as appdb

    values = {"applemail_root": str(store), "user_address": "you@example.com"}
    monkeypatch.setattr(appdb, "get_setting", lambda k, d="": values.get(k, d))
    monkeypatch.setattr(am, "MAIL_HOME", tmp_path / "definitely-not-here")

    state = am.AppleMailSource().status()
    assert state.ready is True
    assert state.extra["mail_root"] == str(store)


def test_bad_explicit_root_says_so_instead_of_blaming_apple_mail(monkeypatch, tmp_path):
    from app import db as appdb

    values = {"applemail_root": "/nope/not/here"}
    monkeypatch.setattr(appdb, "get_setting", lambda k, d="": values.get(k, d))
    state = am.AppleMailSource().status()
    assert state.ready is False
    assert "does not exist" in state.detail


# ------------------------------------------------- permission diagnosis

def test_blocked_store_raises_instead_of_looking_empty(store, monkeypatch):
    """os.walk hides directory errors by default, so a store blocked by Full
    Disk Access would otherwise report 'no mail downloaded yet' -- sending the
    user off to debug Apple Mail when the real fix is a macOS permission."""
    import os as real_os

    def blocked(path, **kwargs):
        onerror = kwargs.get("onerror")
        if onerror:
            onerror(PermissionError(13, "Operation not permitted", str(path)))
        return iter(())

    monkeypatch.setattr(am.os, "walk", blocked)
    with pytest.raises(PermissionError):
        list(am.iter_message_files(store))


def test_blocked_store_surfaces_as_an_actionable_status(store, monkeypatch):
    from app import db as appdb

    values = {"applemail_root": str(store)}
    monkeypatch.setattr(appdb, "get_setting", lambda k, d="": values.get(k, d))
    monkeypatch.setattr(am.os, "walk", lambda path, **kw: (
        kw["onerror"](PermissionError(13, "Operation not permitted")) or iter(())
    ))

    state = am.AppleMailSource().status()
    assert state.ready is False
    assert "Full Disk Access" in state.detail


def test_one_unreadable_folder_does_not_kill_a_working_store(store, monkeypatch):
    """The opposite guard: partial failure inside a store that otherwise works
    must be skipped, not fatal."""
    real_walk = am.os.walk

    def flaky(path, **kwargs):
        onerror = kwargs.get("onerror")
        if onerror:
            onerror(PermissionError(13, "one bad folder"))
        yield from real_walk(path, **{k: v for k, v in kwargs.items() if k != "onerror"})

    monkeypatch.setattr(am.os, "walk", flaky)
    found = list(am.iter_message_files(store, inbox_only=True))
    assert len(found) == 4          # still returns everything it could read


# ------------------------------------------- status() must never raise

def test_status_never_raises_when_the_mail_root_is_blocked(monkeypatch, tmp_path):
    """Regression: find_mail_root() called iterdir() unguarded, so a TCC block
    propagated out of status() and 500'd /api/health -- the endpoint the shell
    polls to decide the app is alive. A fixable permission looked like a dead app."""
    from app import db as appdb

    monkeypatch.setattr(appdb, "get_setting", lambda k, d="": "")

    def blocked(self):
        raise PermissionError(1, "Operation not permitted", str(am.MAIL_HOME))

    monkeypatch.setattr(am.Path, "iterdir", blocked)
    monkeypatch.setattr(am, "MAIL_HOME", tmp_path)   # exists, but iterdir blows up

    state = am.AppleMailSource().status()
    assert state.ready is False
    assert state.needs_setup is True
    assert "Full Disk Access" in state.detail


def test_status_never_raises_on_any_os_error(monkeypatch, tmp_path):
    from app import db as appdb

    monkeypatch.setattr(appdb, "get_setting", lambda k, d="": "")
    monkeypatch.setattr(am, "MAIL_HOME", tmp_path)
    monkeypatch.setattr(am.Path, "iterdir", lambda self: (_ for _ in ()).throw(OSError("disk on fire")))

    state = am.AppleMailSource().status()
    assert state.ready is False
    assert "disk on fire" in state.detail


def test_health_endpoint_survives_a_broken_source(monkeypatch):
    """/api/health must answer 200 even when the selected source explodes."""
    from fastapi.testclient import TestClient
    from app import main as app_main

    class Exploding:
        name = "boom"
        label = "Boom"
        def status(self):
            raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(app_main, "get_source", lambda: Exploding())
    with TestClient(app_main.app) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["source"]["ready"] is False
    assert "Could not read the mail source" in body["source"]["detail"]


def test_sync_reports_the_permission_fix_rather_than_crashing(monkeypatch, tmp_path):
    import asyncio
    from app import db as appdb
    from app.sources.base import SourceError

    monkeypatch.setattr(appdb, "get_setting", lambda k, d="": "")
    monkeypatch.setattr(am, "MAIL_HOME", tmp_path)
    monkeypatch.setattr(am.Path, "iterdir",
                        lambda self: (_ for _ in ()).throw(PermissionError(1, "nope")))

    with pytest.raises(SourceError, match="Full Disk Access"):
        asyncio.run(am.AppleMailSource().sync())


# --------------------------------------------- real dates, not file mtimes

def test_header_date_wins_over_file_mtime(tmp_path):
    """Apple Mail stamps every file with today's date on a first sync, so a
    2026-05 email and this morning's look identical by mtime. Sorting on that
    produced an arbitrary 'newest 300' full of months-old mail."""
    import os
    raw = SIMPLE.replace("Tue, 25 Aug 2026 09:00:00 +0000", "Fri, 08 May 2026 09:00:00 +0000")
    path = write_emlx(tmp_path / "old.emlx", raw)
    fresh_mtime = 1_800_000_000.0
    os.utime(path, (fresh_mtime, fresh_mtime))

    got = am.read_header_date(path, fresh_mtime)
    from datetime import datetime, timezone
    assert datetime.fromtimestamp(got, timezone.utc).strftime("%Y-%m") == "2026-05"
    assert got != fresh_mtime


def test_header_date_falls_back_to_mtime_when_there_is_no_date_header(tmp_path):
    raw = SIMPLE.replace("Date: Tue, 25 Aug 2026 09:00:00 +0000\n", "")
    path = write_emlx(tmp_path / "nodate.emlx", raw)
    assert am.read_header_date(path, 1234.0) == 1234.0


def test_header_date_survives_a_garbage_date_header(tmp_path):
    raw = SIMPLE.replace("Tue, 25 Aug 2026 09:00:00 +0000", "not a date at all")
    path = write_emlx(tmp_path / "bad.emlx", raw)
    assert am.read_header_date(path, 999.0) == 999.0


def test_answered_and_flagged_reach_the_row(tmp_path):
    path = write_emlx(tmp_path / "m.emlx", SIMPLE, am.FLAG_READ | am.FLAG_ANSWERED | am.FLAG_FLAGGED)
    row = am.to_row(path, path.stat().st_mtime, ["INBOX"])
    assert row["is_answered"] == 1 and row["is_flagged"] == 1


def test_plain_unread_message_has_no_engagement_flags(tmp_path):
    path = write_emlx(tmp_path / "m.emlx", SIMPLE, 0)
    row = am.to_row(path, path.stat().st_mtime, ["INBOX"])
    assert row["is_answered"] == 0 and row["is_flagged"] == 0


def test_correspondent_affinity_counts_who_you_write_to(tmp_path):
    root = tmp_path / "V10" / "ACC"
    sent = ("From: Sam <you@example.com>\nTo: Prof. Lee <lee@uni.edu>\n"
            "Subject: Re: draft\nDate: Tue, 25 Aug 2026 09:00:00 +0000\n\nOn its way.\n")
    for n in range(3):
        write_emlx(root / "Sent Messages.mbox" / "d" / "Messages" / f"{n}.emlx", sent)
    write_emlx(root / "INBOX.mbox" / "d" / "Messages" / "9.emlx", SIMPLE)

    counts = am.build_correspondent_affinity(tmp_path / "V10", force=True)
    assert counts.get("lee@uni.edu") == 3
    assert "you@example.com" not in counts, "your own address is a From, not a To"


# ------------------------------------------- not re-reading an unchanged store

def _store(tmp_path, count=3):
    root = tmp_path / "Mail" / "V10"
    box = root / "INBOX.mbox" / "Messages"
    box.mkdir(parents=True)
    for i in range(count):
        raw = (b"From: a@b.c\r\nTo: me@x.com\r\nSubject: s%d\r\n"
               b"Date: Sat, 19 Sep 2026 10:00:00 +0000\r\n\r\nbody" % i)
        (box / f"{i}.emlx").write_bytes(str(len(raw)).encode() + b"\n" + raw)
    return root


@pytest.mark.asyncio
async def test_an_untouched_store_is_not_read_twice(tmp_path, monkeypatch):
    """The poller runs every five minutes, forever.

    On a Mac with Mail.app closed it had scanned 14,135 files two hundred
    times in twenty-four hours, nine seconds a go, to find nothing each time --
    which is the number the "no new mail" banner was counting. The walk is
    cheap; the header read on every candidate is not.
    """
    from app import db
    from app.sources import applemail

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()
    root = _store(tmp_path)
    db.set_setting("applemail_root", str(root))

    source = applemail.AppleMailSource()
    first = await source.sync(days=3650, max_messages=50)
    assert first["fetched"] == 3 and not first.get("unchanged")

    reads: list = []
    real = applemail.read_header_date
    monkeypatch.setattr(applemail, "read_header_date",
                        lambda p, m: (reads.append(p), real(p, m))[1])

    second = await source.sync(days=3650, max_messages=50)
    assert second["unchanged"] is True
    assert second["fetched"] == 0
    assert reads == [], "an unchanged store must not be header-read again"


@pytest.mark.asyncio
async def test_a_new_message_ends_the_skip(tmp_path, monkeypatch):
    """The guard must not be a way to stop noticing mail."""
    from app import db
    from app.sources import applemail

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()
    root = _store(tmp_path)
    db.set_setting("applemail_root", str(root))

    source = applemail.AppleMailSource()
    await source.sync(days=3650, max_messages=50)
    assert (await source.sync(days=3650, max_messages=50))["unchanged"] is True

    raw = (b"From: a@b.c\r\nTo: me@x.com\r\nSubject: brand new\r\n"
           b"Date: Sat, 19 Sep 2026 11:00:00 +0000\r\n\r\nbody")
    (root / "INBOX.mbox" / "Messages" / "99.emlx").write_bytes(
        str(len(raw)).encode() + b"\n" + raw)

    after = await source.sync(days=3650, max_messages=50)
    assert not after.get("unchanged")
    assert after["fetched"] == 4


@pytest.mark.asyncio
async def test_an_empty_database_is_never_skipped(tmp_path, monkeypatch):
    """A fingerprint left by a run that never wrote anything must not convince
    the next one there is nothing to do."""
    from app import db
    from app.sources import applemail

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()
    root = _store(tmp_path)
    db.set_setting("applemail_root", str(root))
    source = applemail.AppleMailSource()
    await source.sync(days=3650, max_messages=50)

    with db.connect() as conn:
        conn.execute("DELETE FROM emails")
        conn.commit()
    again = await source.sync(days=3650, max_messages=50)
    assert not again.get("unchanged")
    assert again["fetched"] == 3
