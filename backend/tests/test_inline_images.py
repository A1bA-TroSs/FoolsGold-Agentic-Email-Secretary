"""Inline images in the reading pane.

543 of 1,274 messages in the real mailbox carry `cid:` images -- 1,405
references in all -- and every one of them rendered as a broken icon, because
nothing ever extracted the inline parts. This is the biggest single thing wrong
with the reading pane and it had no test at all.
"""
from __future__ import annotations

import email.message
import email.policy
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.sources import applemail


def _message_with_inline(cid: str = "logo@fools.gold", partial: bool = False) -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = "dept@uni.edu"
    msg["To"] = "me@x.com"
    msg["Subject"] = "Newsletter"
    msg.set_content("plain text")
    msg.add_alternative(f'<html><body><img src="cid:{cid}"></body></html>', subtype="html")
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    msg.add_attachment(png, maintype="image", subtype="png", cid=f"<{cid}>",
                       disposition="inline")
    raw = msg.as_bytes()
    if partial:
        # What Apple Mail writes when the attachment was left on the server:
        # headers intact, payload replaced, X-Apple-Content-Length added.
        raw = raw.replace(b"Content-Type: image/png",
                          b"X-Apple-Content-Length: 48\r\nContent-Type: image/png")
    # .emlx = a byte count, then the message, then a plist.
    return str(len(raw)).encode() + b"\n" + raw + b"<?xml version='1.0'?><plist/>"


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def _partial_message(cid: str = "logo@fools.gold", filename: str | None = None) -> bytes:
    """What Apple Mail actually writes when the attachment stayed on the server.

    The earlier fixture only *added* the header and left the base64 payload in
    place, so the part still decoded to a valid PNG -- it tested a file that
    cannot exist. A real `.partial.emlx` has the payload replaced.
    """
    raw = _message_with_inline(cid)
    body = raw.split(b"\n", 1)[1].rsplit(b"<?xml", 1)[0]
    msg = email.message_from_bytes(body, policy=email.policy.default)
    for part in msg.walk():
        if part.get_content_type() == "image/png":
            part.set_payload("")
            part["X-Apple-Content-Length"] = str(len(PNG))
            if filename:
                del part["Content-Disposition"]
                part["Content-Disposition"] = f'inline; filename="{filename}"'
    out = msg.as_bytes()
    return str(len(out)).encode() + b"\n" + out + b"<?xml version='1.0'?><plist/>"


def _apple_store(tmp_path: Path, name: str, raw: bytes) -> Path:
    """`.../INBOX.mbox/Messages/<name>` -- the sibling `Attachments` directory
    the reassembly looks in only exists relative to this layout."""
    messages = tmp_path / "INBOX.mbox" / "Messages"
    messages.mkdir(parents=True, exist_ok=True)
    path = messages / name
    path.write_bytes(raw)
    return path


# ------------------------------------------------------------------ cid parsing

@pytest.mark.parametrize("raw,want", [
    ("<logo@example.com>", "logo@example.com"),      # header form, brackets on
    ("logo@example.com", "logo@example.com"),        # url form, brackets off
    ("  <Logo@Example.COM> ", "logo@example.com"),   # case and space
    ("a%40b", "a@b"),                                # percent-decoded, RFC 2392
])
def test_a_content_id_and_a_cid_url_are_compared_in_the_same_shape(raw, want):
    """The header keeps its angle brackets and the URL does not. Comparing them
    as they arrive never matches, and every inline image in the mailbox breaks
    -- which looks exactly like the images simply not being there."""
    assert applemail._normalise_cid(raw) == want


# ------------------------------------------------------------------ extraction

def test_an_inline_image_is_found_in_a_real_message(tmp_path):
    path = tmp_path / "1.emlx"
    path.write_bytes(_message_with_inline())
    parts = applemail.inline_parts(path)
    assert set(parts) == {"logo@fools.gold"}
    ctype, payload = parts["logo@fools.gold"]
    assert ctype == "image/png"
    assert payload.startswith(b"\x89PNG")


def test_a_detached_attachment_is_reassembled_from_the_sidecar_directory(tmp_path):
    """The single most likely remaining cause of a broken inline image.

    Apple Mail leaves attachments on the server and writes `.partial.emlx`:
    Content-ID present, payload gone. The bytes are not missing -- they are in
    `Attachments/<id>/<part-nr>/`, one directory per part, named after a dotted
    path of 1-based child indices. Skipping those parts (what this did before)
    means every image in such a message stays broken forever.
    """
    path = _apple_store(tmp_path, "7.partial.emlx", _partial_message())
    side = tmp_path / "INBOX.mbox" / "Attachments" / "7" / "2"
    side.mkdir(parents=True)
    (side / "Mail-Anhang.png").write_bytes(PNG)   # locale-specific default name
    parts = applemail.inline_parts(path)
    assert set(parts) == {"logo@fools.gold"}
    assert parts["logo@fools.gold"][1] == PNG


def test_the_part_number_is_the_child_index_not_a_leaf_counter(tmp_path):
    """The image here is child 2 of the top-level message, even though it is
    the *third* leaf: the text/plain and text/html bodies are 1.1 and 1.2.
    Numbering leaves instead of children puts the bytes one directory over,
    which silently serves the wrong picture or none at all."""
    path = _apple_store(tmp_path, "8.partial.emlx", _partial_message())
    wrong = tmp_path / "INBOX.mbox" / "Attachments" / "8" / "3"
    wrong.mkdir(parents=True)
    (wrong / "x.png").write_bytes(PNG)
    assert applemail.inline_parts(path) == {}


def test_the_id_is_the_name_up_to_the_first_dot(tmp_path):
    """`7.partial.emlx` files under `7`, not `7.partial`. Splitting on the last
    dot is the obvious way to write this and it is wrong."""
    assert applemail._attachment_dir(Path("/m/Messages/7.partial.emlx"), (2,)) == \
        Path("/m/Attachments/7/2")


def test_a_detached_part_with_no_sidecar_reports_nothing(tmp_path):
    """Still the honest answer when the bytes really are only on the server:
    a zero-byte image would make that indistinguishable from a lookup bug."""
    path = _apple_store(tmp_path, "9.partial.emlx", _partial_message())
    assert applemail.inline_parts(path) == {}


def test_a_sidecar_that_is_not_an_image_is_refused(tmp_path):
    """The declared type comes from the message and the bytes come from a
    directory found by index. If they disagree, the index was wrong -- serving
    it anyway would hand the renderer arbitrary file content under an
    `image/png` label."""
    path = _apple_store(tmp_path, "10.partial.emlx", _partial_message())
    side = tmp_path / "INBOX.mbox" / "Attachments" / "10" / "2"
    side.mkdir(parents=True)
    (side / "notes.txt").write_bytes(b"this is not a picture")
    assert applemail.inline_parts(path) == {}


def test_a_declared_filename_cannot_escape_the_attachment_directory(tmp_path):
    """`Content-Disposition: filename=` is attacker-controlled text from the
    message. It is used to name a file to read."""
    outside = tmp_path / "INBOX.mbox" / "Attachments" / "11"
    (outside / "2").mkdir(parents=True)
    (outside / "stolen.png").write_bytes(PNG)
    path = _apple_store(tmp_path, "11.partial.emlx",
                        _partial_message(filename="../stolen.png"))
    assert applemail.inline_parts(path) == {}


def test_a_whole_message_with_no_detached_parts_still_works_from_the_store(tmp_path):
    path = _apple_store(tmp_path, "12.emlx", _message_with_inline())
    assert set(applemail.inline_parts(path)) == {"logo@fools.gold"}


def test_a_non_image_part_is_never_served(tmp_path):
    msg = email.message.EmailMessage()
    msg["From"] = "x@y.z"
    msg.set_content("hi")
    msg.add_attachment(b"<script>alert(1)</script>", maintype="text", subtype="html",
                       cid="<evil@x>", disposition="inline")
    raw = msg.as_bytes()
    path = tmp_path / "3.emlx"
    path.write_bytes(str(len(raw)).encode() + b"\n" + raw + b"<plist/>")
    assert applemail.inline_parts(path) == {}, "only image/* may reach the renderer"


def test_an_unreadable_file_is_not_an_exception(tmp_path):
    assert applemail.inline_parts(tmp_path / "does-not-exist.emlx") == {}


# ------------------------------------------------------------------ the endpoint

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()
    root = tmp_path / "Mail"
    (root / "INBOX.mbox" / "Messages").mkdir(parents=True)
    src = root / "INBOX.mbox" / "Messages" / "1.emlx"
    src.write_bytes(_message_with_inline())
    db.set_setting("applemail_root", str(root))
    db.upsert_emails([dict(
        id="e1", conversation_id=None, subject="Newsletter", from_name="Dept",
        from_address="dept@uni.edu", to_recipients="[]", cc_recipients="[]",
        received_at="2026-09-20T09:00:00+00:00", is_read=1, is_answered=0,
        is_flagged=0, has_attachments=1, importance="normal", web_link="",
        folder="INBOX", body_preview="", body_text="", body_html="",
        source_path=str(src), synced_at=db.now_iso())])
    with TestClient(app) as c:
        yield c, tmp_path


def test_the_image_is_served_with_the_right_type(client):
    c, _ = client
    r = c.get("/api/mail/e1/part/logo@fools.gold")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content.startswith(b"\x89PNG")
    # Even a mislabelled payload must not be re-interpreted as something
    # executable by the renderer.
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in r.headers["content-security-policy"]


def test_a_missing_image_is_a_404_not_a_placeholder(client):
    c, _ = client
    assert c.get("/api/mail/e1/part/nothing@here").status_code == 404


def test_a_path_outside_the_mail_store_is_refused(client):
    """A stored path is data. The first time anything else writes to that
    column, this endpoint becomes a file read with an attacker-chosen path.

    Answered 404 rather than 403 since the check moved into the source: a 403
    confirms the path exists and is merely off-limits, and there is nothing to
    gain by telling a message body that. The assertion is on the *bytes*, which
    is the part that actually matters."""
    c, tmp_path = client
    secret = tmp_path / "secret.emlx"
    secret.write_bytes(_message_with_inline())
    with db.connect() as conn:
        conn.execute("UPDATE emails SET source_path = ? WHERE id = 'e1'", (str(secret),))
        conn.commit()
    reply = c.get("/api/mail/e1/part/logo@fools.gold")
    assert reply.status_code != 200
    assert b"\x89PNG" not in reply.content


def test_an_unknown_message_is_a_404(client):
    c, _ = client
    assert c.get("/api/mail/nope/part/logo@fools.gold").status_code == 404


# ------------------------------------------------- going back to the original

def test_the_containment_check_lives_in_the_source_not_the_router(client):
    """It has to be here, where the only code that knows what a legitimate path
    looks like lives. In the router it is one guard a second caller can forget;
    here there is nothing to forget."""
    from app.sources.applemail import AppleMailSource
    _, tmp_path = client
    outside = tmp_path / "elsewhere.emlx"
    outside.write_bytes(_message_with_inline())
    with db.connect() as conn:
        conn.execute("UPDATE emails SET source_path = ? WHERE id = 'e1'", (str(outside),))
        conn.commit()
    source = AppleMailSource()
    assert source.inline_part("e1", "logo@fools.gold") is None
    assert source.parent_message("e1") is None


def test_a_source_with_no_way_back_says_so_rather_than_failing(client):
    """A Graph or Gmail message has no file. The base implementation returning
    None is what lets the reading pane show a missing picture instead of a
    stack trace, and what lets the compose pane open at all."""
    from app.sources.base import MailSource

    class Bare(MailSource):
        name = "bare"
        def status(self): ...
        async def sync(self, days=None, max_messages=None): ...

    assert Bare().inline_part("e1", "logo@fools.gold") is None
    assert Bare().parent_message("e1") is None


def test_the_reply_headers_come_back_off_the_file(client):
    """The Message-ID is hashed into the row id on purpose, so it survives a
    re-sync -- which means the database cannot give it back. The file still
    has it, so the answer is to read it rather than store a second copy that
    can drift out of step with the first."""
    from app.sources.applemail import AppleMailSource
    _, tmp_path = client
    source = tmp_path / "Mail" / "INBOX.mbox" / "Messages" / "1.emlx"
    raw = source.read_bytes()
    body = raw.split(b"\n", 1)[1]
    patched = body.replace(b"Subject: Newsletter",
                           b"Message-ID: <real@uni.edu>\r\n"
                           b"References: <a@x> <b@x>\r\n"
                           b"Subject: Newsletter", 1)
    source.write_bytes(str(len(patched)).encode() + b"\n" + patched)

    parent = AppleMailSource().parent_message("e1")
    assert parent is not None
    assert parent.message_id == "<real@uni.edu>"
    assert parent.references == "<a@x> <b@x>"
    assert parent.subject == "Newsletter"
    assert [m.address for m in parent.sender] == ["dept@uni.edu"]
    assert parent.raw, "the raw bytes a message/rfc822 forward needs"


def test_the_headers_feed_a_reply_that_actually_threads(client):
    """The end of the seam: file -> ParentMessage -> a threaded reply, with no
    schema change anywhere in between."""
    from app.compose import Mailbox, build_reply
    from app.sources.applemail import AppleMailSource
    _, tmp_path = client
    source = tmp_path / "Mail" / "INBOX.mbox" / "Messages" / "1.emlx"
    raw = source.read_bytes()
    body = raw.split(b"\n", 1)[1].replace(
        b"Subject: Newsletter", b"Message-ID: <real@uni.edu>\r\nSubject: Newsletter", 1)
    source.write_bytes(str(len(body)).encode() + b"\n" + body)

    parent = AppleMailSource().parent_message("e1")
    reply = build_reply(parent, sender=Mailbox("me@x.com", "Me"), text="Got it")
    assert reply["In-Reply-To"] == "<real@uni.edu>"
    assert "<real@uni.edu>" in reply["References"]
    assert reply["Subject"] == "Re: Newsletter"
    assert "dept@uni.edu" in reply["To"]


def test_the_reply_body_comes_back_with_the_headers(client):
    """`ParentMessage` feeds the quote block as well as the threading headers.
    Returning the headers alone gives a reply that threads correctly and quotes
    nothing, which reads as a bug in the compose pane."""
    from app.sources.applemail import AppleMailSource
    parent = AppleMailSource().parent_message("e1")
    assert parent.body_text.strip() == "plain text"
    assert "<img" in parent.body_html


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_a_stored_path_that_is_not_a_regular_file_is_never_opened(client, kind):
    """Containment is not enough on its own: the *shape* of the target matters.

    A directory would merely error. A named pipe inside the mail root would
    make the read block forever, and this endpoint is reachable by id -- so an
    ordinary-looking request hangs a worker with no error anywhere. `is_file()`
    is the line that stops it, which is why this is asserted on `_source_file`
    directly: a test that went through the reader would prove the point by
    hanging."""
    from app.sources.applemail import AppleMailSource
    _, tmp_path = client
    messages = tmp_path / "Mail" / "INBOX.mbox" / "Messages"
    if kind == "directory":
        target = messages
    else:
        target = messages / "pipe.emlx"
        os.mkfifo(target)
    with db.connect() as conn:
        conn.execute("UPDATE emails SET source_path = ? WHERE id = 'e1'", (str(target),))
        conn.commit()
    assert AppleMailSource()._source_file("e1") is None


def test_the_renderer_is_never_handed_a_non_image(client, monkeypatch):
    """The source filters to `image/*` and so does this endpoint, on purpose.
    A second source will be written by someone who has not read `inline_parts`,
    and this is the guard that decides what the reading pane may be handed."""
    from app.routers import mail as mail_router

    class Sneaky:
        def inline_part(self, email_id, cid):
            return ("text/html", b"<script>alert(1)</script>")

    monkeypatch.setattr(mail_router, "get_source", lambda: Sneaky())
    c, _ = client
    reply = c.get("/api/mail/e1/part/anything")
    assert reply.status_code == 404
    assert b"<script>" not in reply.content


# ------------------------------------- the reason line, recomputed at serve time

def test_a_message_classified_before_the_reason_was_translatable_still_reads(client):
    """Every message in a real mailbox was classified when the rationale was an
    English sentence. The detail route recomputes the structural reason from
    the row rather than trusting storage, so they all render in the user's
    language at once -- no migration, no re-classification, and no way for a
    stored copy to disagree with the rule.
    """
    import json
    c, _ = client
    db.save_classification({
        "email_id": "e1", "bucket": "action", "deadline": "2026-09-18",
        "rationale": "Deadline 2026-09-18 found in the text; has an attachment.",
        "score": 90, "matched": "", "model": "structural", "source": "structural",
        "created_at": db.now_iso(),
    })
    body = c.get("/api/mail/e1").json()
    codes = json.loads(body["rationale"])
    assert [x["key"] for x in codes][0] == "sigDeadline"
    assert codes[0]["vars"] == {"date": "2026-09-18"}


def test_a_model_written_rationale_is_passed_through_untouched(client):
    """The model wrote it in the user's language. Nothing here can re-derive
    that, so nothing here may overwrite it."""
    c, _ = client
    prose = "마감이 임박했고 본인에게 직접 온 메일입니다."
    db.save_classification({
        "email_id": "e1", "bucket": "action", "deadline": None, "rationale": prose,
        "score": 90, "matched": "", "model": "qwen3.5:9b", "source": "llm",
        "created_at": db.now_iso(),
    })
    assert c.get("/api/mail/e1").json()["rationale"] == prose
