"""Getting a message off the machine.

The SMTP half is tested against a real server speaking the real protocol on a
real socket (`tests/fake_smtp.py`), because the questions worth asking are
about the bytes on the wire. The IMAP half is tested against a narrow fake
object, because there the risk is in the *decision* -- which mailbox, and
whether to write to it at all -- and not in the socket.
"""
from __future__ import annotations

import email.message
import email.policy
import smtplib
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fake_smtp import FakeSMTP                                  # noqa: E402

from app import db                                              # noqa: E402
from app.compose import Mailbox, build_new                      # noqa: E402
from app.transports import base, imap_folders as sent_folder                    # noqa: E402
from app.transports.registry import get_transport               # noqa: E402
from app.transports.smtp_transport import SmtpTransport         # noqa: E402

ME = Mailbox("danny@uni.edu", "Danny Park")
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def a_message(bcc: list[Mailbox] | None = None) -> email.message.EmailMessage:
    return build_new(
        sender=ME,
        to=[Mailbox("prof@uni.edu", "Prof Kim")],
        cc=[Mailbox("ta@uni.edu", "TA")],
        bcc=bcc or [],
        subject="Midterm",
        text="See you there.",
        now=NOW,
    )


# ------------------------------------------------------------------ envelope

def test_bcc_is_in_the_envelope_and_not_in_the_message():
    """One field, two requirements that pull opposite ways. In the envelope or
    the blind recipient gets nothing; out of the headers or they are not
    blind."""
    msg = a_message(bcc=[Mailbox("dean@uni.edu")])
    assert "dean@uni.edu" in base.envelope_recipients(msg)
    wire = base.transmissible(msg)
    assert b"dean@uni.edu" not in wire
    assert b"prof@uni.edu" in wire


def test_stripping_bcc_does_not_damage_the_original_message():
    """The compose pane still has to show the user what they addressed."""
    msg = a_message(bcc=[Mailbox("dean@uni.edu")])
    base.transmissible(msg)
    assert "dean@uni.edu" in str(msg["Bcc"])


def test_one_person_on_two_lines_is_delivered_to_once():
    msg = build_new(sender=ME, to=[Mailbox("Prof@Uni.edu")], cc=[Mailbox("prof@uni.edu")],
                    subject="x", text="y", now=NOW)
    assert base.envelope_recipients(msg) == ["Prof@Uni.edu"]


@pytest.mark.parametrize("drop,complaint", [
    ("To", "recipients"),
    ("Message-ID", "Message-ID"),
    ("Date", "Date"),
    ("From", "From"),
])
def test_a_half_built_message_is_refused_before_it_reaches_a_server(drop, complaint):
    """Each of these has been shipped by somebody. The compose layer sets all
    four; this is the assertion that nothing downstream stopped doing so."""
    msg = a_message()
    del msg["Cc"]
    del msg[drop]
    with pytest.raises(base.TransportError, match=complaint):
        base.check_sendable(msg)


def test_with_nothing_configured_nothing_is_sent_and_the_error_says_where_to_go():
    transport = base.NullTransport()
    assert transport.status().needs_setup
    with pytest.raises(base.TransportError, match="Settings"):
        transport.send(a_message())


# ------------------------------------------------------------------ settings

@pytest.fixture()
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()

    def setup(port: int, **overrides) -> SmtpTransport:
        values = {
            "mail_transport": "smtp", "smtp_host": "127.0.0.1",
            "smtp_port": str(port), "smtp_security": "plain",
            "smtp_username": "danny@uni.edu", "smtp_password": "app-specific",
            "sent_copy": "skip",
        }
        values.update(overrides)
        for key, value in values.items():
            db.set_setting(key, value)
        return SmtpTransport()
    return setup


def test_the_registry_never_guesses_a_transport(configured, monkeypatch):
    """`sources/registry` falls back to Apple Mail when the name is unknown --
    reading the wrong mailbox is recoverable. Sending is not."""
    configured(25, mail_transport="carrier-pigeon")
    assert isinstance(get_transport(), base.NullTransport)
    configured(25, mail_transport="smtp")
    assert isinstance(get_transport(), SmtpTransport)


def test_status_asks_for_the_missing_piece_by_name(configured):
    assert configured(25, smtp_host="").status().detail.startswith("No outgoing server")
    assert "app-specific password" in configured(25, smtp_password="").status().detail
    assert configured(25).status().ready


def test_the_password_is_stored_encrypted(configured):
    configured(25)
    with db.connect() as conn:
        row = conn.execute("SELECT value, secret, encrypted FROM settings "
                           "WHERE key = 'smtp_password'").fetchone()
    assert row["encrypted"] == 1
    assert row["value"] is None
    assert b"app-specific" not in bytes(row["secret"])
    assert db.all_settings()["smtp_password"] != "app-specific"


# ------------------------------------------------------------------ the wire

@pytest.fixture()
def server():
    servers: list[FakeSMTP] = []

    def start(**kw) -> FakeSMTP:
        s = FakeSMTP(**kw)
        s.start()
        s.ready.wait(timeout=5)
        servers.append(s)
        return s
    yield start
    for s in servers:
        s.stop()


def test_a_send_puts_the_right_addresses_in_the_envelope_and_the_right_bytes_in_data(
        configured, server):
    fake = server()
    transport = configured(fake.port)
    result = transport.send(a_message(bcc=[Mailbox("dean@uni.edu")]))
    fake.join(timeout=5)

    assert fake.mail_from == "danny@uni.edu"
    assert sorted(fake.rcpt_to) == ["dean@uni.edu", "prof@uni.edu", "ta@uni.edu"]
    assert b"dean@uni.edu" not in fake.payload, "the blind recipient must stay blind"
    assert b"Subject: Midterm" in fake.payload
    assert result.message_id.endswith("uni.edu>")
    assert set(result.recipients) == set(fake.rcpt_to)


def test_the_credentials_offered_are_the_ones_configured(configured, server):
    fake = server()
    configured(fake.port).send(a_message())
    fake.join(timeout=5)
    assert fake.credentials == ("danny@uni.edu", "app-specific")


def test_a_rejected_password_says_what_to_try_instead(configured, server):
    """The commonest real failure, and the commonest real fix. "535" is not an
    answer the user can act on; "your provider probably wants an app-specific
    password" is."""
    fake = server(auth_ok=False)
    with pytest.raises(base.TransportError, match="app-specific password"):
        configured(fake.port).send(a_message())


def test_a_partly_refused_send_reports_who_missed_out_and_does_not_raise(configured, server):
    """`sendmail` raises only when *every* recipient is refused. Refuse one of
    three and it comes back as a return value, so ignoring it means the app
    reports unqualified success while somebody got nothing."""
    fake = server(refuse=("ta@uni.edu",))
    result = configured(fake.port).send(a_message())
    fake.join(timeout=5)
    assert fake.payload, "the others really were sent to"
    assert result.refused == ("ta@uni.edu",)
    assert "ta@uni.edu" in result.detail
    assert "prof@uni.edu" in result.recipients
    assert "ta@uni.edu" not in result.recipients


def test_every_recipient_refused_means_nothing_was_sent(configured, server):
    fake = server(refuse=("prof@uni.edu", "ta@uni.edu"))
    with pytest.raises(base.TransportError, match="Nothing was sent"):
        configured(fake.port).send(a_message())


def test_starttls_never_silently_downgrades_to_plaintext(configured, server):
    """A submission server that will not do STARTTLS is a downgrade attack as
    often as it is a misconfiguration. The test that matters is not that we
    raise -- it is that the password never left the machine."""
    fake = server(advertise_starttls=False)
    transport = configured(fake.port, smtp_security="starttls")
    with pytest.raises(base.TransportError, match="clear"):
        transport.send(a_message())
    fake.join(timeout=5)
    assert fake.credentials is None, "the password must not be offered"
    assert fake.payload == b""


def test_an_unreachable_server_is_reported_as_unreachable(configured):
    with pytest.raises(base.TransportError, match="Could not reach"):
        configured(9).send(a_message())          # discard port, nothing listens


def test_a_server_that_rejects_the_body_does_not_report_success(configured, server):
    fake = server(data_reply="552 message too large")
    with pytest.raises(base.TransportError, match="552"):
        configured(fake.port).send(a_message())


# ------------------------------------------------------------------ Sent copy

class FakeImap:
    """Only the five calls `sent_folder` makes."""

    def __init__(self, mailboxes: list[bytes], found: dict[str, list[bytes]] | None = None):
        self.mailboxes = mailboxes
        self.found = found or {}
        self.appended: list[tuple[str, bytes]] = []
        self.selected: list[str] = []
        self.append_result = ("OK", [b"done"])

    def capability(self): return ("OK", [b"IMAP4rev1"])
    def list(self, directory='""', pattern="*"): return ("OK", self.mailboxes)

    def select(self, mailbox, readonly=False):
        self.selected.append(mailbox)
        return ("OK", [b"1"])

    def uid(self, command, *args):
        mailbox = self.selected[-1] if self.selected else ""
        return ("OK", [b" ".join(self.found.get(mailbox, []))])

    def append(self, mailbox, flags, date_time, message):
        self.appended.append((mailbox, message))
        self.append_flags = flags
        return self.append_result


GMAIL_KO = [
    rb'(\HasNoChildren) "/" "INBOX"',
    rb'(\HasNoChildren \Sent) "/" "[Gmail]/&vPSwuLDpwuU-"',
    '(\\HasNoChildren) "/" "보낸 편지함"'.encode("utf-8"),
]


def test_the_special_use_flag_beats_a_folder_that_merely_looks_right():
    """A server that advertises `\\Sent` has told us the answer. A name match
    is a guess, and it is wrong for everyone who renamed the folder."""
    got = sent_folder.discover_sent(FakeImap(GMAIL_KO))
    assert got == "[Gmail]/&vPSwuLDpwuU-"


def test_a_localised_folder_name_is_recognised_when_there_are_no_flags():
    """This app's own user has a mailbox called 보낸 편지함, and Gmail localises
    its IMAP names. An English-only heuristic fails on the first real user."""
    unflagged = [line.replace(rb"\Sent", rb"\HasNoChildren") for line in GMAIL_KO]
    assert sent_folder.discover_sent(FakeImap(unflagged)) == "보낸 편지함"


@pytest.mark.parametrize("line,flags,name", [
    (rb'(\HasNoChildren \Sent) "/" "Sent Items"', ["\\HasNoChildren", "\\Sent"], "Sent Items"),
    (rb'() NIL "INBOX"', [], "INBOX"),
    (rb'(\Noselect) "." "a.b"', ["\\Noselect"], "a.b"),
])
def test_list_lines_are_parsed_into_flags_and_a_name(line, flags, name):
    assert sent_folder.parse_list_line(line) == (flags, name)


def test_a_manual_override_wins_over_discovery():
    imap = FakeImap(GMAIL_KO)
    assert sent_folder.discover_sent(imap, override="Archive/Mine") == "Archive/Mine"


def test_no_sent_folder_is_an_actionable_message_not_a_crash():
    with pytest.raises(LookupError, match="Settings"):
        sent_folder.discover_sent(FakeImap([rb'(\HasNoChildren) "/" "INBOX"']))


def test_the_server_filing_its_own_copy_means_we_file_none():
    """Gmail and Exchange save SMTP-submitted mail themselves. Appending on top
    is how a Sent folder ends up with two of everything."""
    imap = FakeImap(GMAIL_KO, found={"[Gmail]/&vPSwuLDpwuU-": [b"7"]})
    outcome, detail = sent_folder.save_copy(imap, b"raw", "<m@x>", sleep=lambda _: None)
    assert outcome == "server"
    assert imap.appended == []


def test_a_server_that_files_nothing_gets_a_copy_uploaded():
    imap = FakeImap(GMAIL_KO)
    outcome, mailbox = sent_folder.save_copy(imap, b"raw", "<m@x>", sleep=lambda _: None)
    assert outcome == "appended"
    assert imap.appended == [("[Gmail]/&vPSwuLDpwuU-", b"raw")]


def test_we_wait_for_the_sent_folder_to_settle_before_appending():
    """SMTP submission and the server's IMAP write are two different systems
    and the second one lags. Checking once and appending immediately is how the
    duplicate gets created."""
    slept: list[float] = []
    imap = FakeImap(GMAIL_KO)
    sent_folder.save_copy(imap, b"raw", "<m@x>", sleep=slept.append)
    assert slept == [d for d in sent_folder.SETTLE_DELAYS if d]
    assert len(slept) >= 3


def test_a_copy_that_appears_late_is_still_found():
    class Late(FakeImap):
        calls = 0

        def uid(self, command, *args):
            Late.calls += 1
            return ("OK", [b"12"]) if Late.calls > 2 else ("OK", [b""])

    imap = Late(GMAIL_KO)
    outcome, _ = sent_folder.save_copy(imap, b"raw", "<m@x>", sleep=lambda _: None)
    assert outcome == "server"
    assert imap.appended == []


def test_duplicates_are_reported_and_never_deleted():
    """Mailspring removes the extras it finds. Deleting from a user's Sent
    folder on the strength of a header match is a worse failure than showing
    them two, and it cannot be undone."""
    imap = FakeImap(GMAIL_KO, found={"[Gmail]/&vPSwuLDpwuU-": [b"7", b"8"]})
    outcome, detail = sent_folder.save_copy(imap, b"raw", "<m@x>", sleep=lambda _: None)
    assert outcome == "server"
    assert "2 copies" in detail
    assert imap.appended == []
    assert not hasattr(imap, "deleted")


# ------------------------------------------------------- the mail is already gone

def test_a_failed_sent_copy_does_not_look_like_a_failed_send(configured, server, monkeypatch):
    """Past the SMTP handshake the mail is gone. Raising here tells the user it
    did not go out, and invites them to send it a second time."""
    fake = server()
    transport = configured(fake.port, sent_copy="auto")
    monkeypatch.setattr(transport, "_imap", lambda cfg: (_ for _ in ()).throw(
        OSError("imap.uni.edu refused the connection")))

    result = transport.send(a_message())
    fake.join(timeout=5)

    assert fake.payload, "the message really was transmitted"
    assert result.sent_copy == base.SENT_FAILED
    assert "was sent" in result.detail
    assert "imap.uni.edu" in result.detail


def test_turning_the_sent_copy_off_touches_no_imap_server(configured, server, monkeypatch):
    fake = server()
    transport = configured(fake.port, sent_copy="skip")

    def explode(cfg):
        raise AssertionError("IMAP must not be contacted when copies are off")
    monkeypatch.setattr(transport, "_imap", explode)

    assert transport.send(a_message()).sent_copy == base.SENT_SKIPPED


# ------------------------------------------------------------------ the API

def test_settings_can_see_every_transport_and_which_one_is_chosen(configured):
    from fastapi.testclient import TestClient
    from app.main import app
    configured(25)
    with TestClient(app) as client:
        body = client.get("/api/transports").json()
    assert body["current"] == "smtp"
    assert set(body["statuses"]) == {"none", "smtp", "imap_draft"}
    assert body["statuses"]["smtp"]["ready"] is True
    # The mode is what decides whether the button says "Send" or "Save to
    # Drafts", and whether the user still has something to do afterwards.
    # Settings cannot infer it from the name.
    assert body["statuses"]["smtp"]["mode"] == base.DELIVERS
    assert body["statuses"]["imap_draft"]["mode"] == base.HANDS_OFF
    assert body["statuses"]["imap_draft"]["label"] == "Save to Drafts"


def test_no_http_route_can_send_anything_yet():
    """The approval gate is not built, so the safe state is that there is no
    way to reach a transport over HTTP at all. If this test ever fails,
    something added a send route without a confirmation step."""
    from app.main import app
    sending = [r for r in app.routes
               if getattr(r, "path", "").startswith("/api")
               and any(word in getattr(r, "path", "") for word in ("send", "reply", "forward"))]
    assert sending == [], f"unreviewed send routes: {[r.path for r in sending]}"


# --------------------------------------------------- handing off, not sending

class FakeDraftImap(FakeImap):
    def __init__(self, mailboxes=None):
        super().__init__(mailboxes if mailboxes is not None else DRAFTS_LISTING)
        self.logged_out = False

    def logout(self):
        self.logged_out = True
        return ("BYE", [b""])


DRAFTS_LISTING = [
    rb'(\HasNoChildren) "/" "INBOX"',
    rb'(\HasNoChildren \Sent) "/" "Sent Messages"',
    rb'(\HasNoChildren \Drafts) "/" "Drafts"',
]


@pytest.fixture()
def drafts(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()
    from app.transports.drafts_transport import ImapDraftTransport

    def setup(imap=None, **overrides):
        values = {"mail_transport": "imap_draft", "imap_host": "imap.uni.edu",
                  "imap_username": "danny@uni.edu", "smtp_password": "app-specific"}
        values.update(overrides)
        for key, value in values.items():
            db.set_setting(key, value)
        transport = ImapDraftTransport()
        fake = imap if imap is not None else FakeDraftImap()
        monkeypatch.setattr(transport, "_imap", lambda cfg: fake)
        return transport, fake
    return setup


def test_a_handed_off_message_is_never_called_sent(drafts):
    """Two measured facts killed the Apple Mail handoff: `outgoing message` has
    no header properties, and a `.eml` opens read-only. IMAP APPEND stores the
    message verbatim, so this is the handoff that keeps threading -- but
    nothing has been delivered, and the result has to say so."""
    transport, fake = drafts()
    result = transport.send(a_message())
    assert result.delivered is False
    assert result.handoff == "Drafts"
    assert "send" in result.detail.lower()
    assert fake.appended and fake.appended[0][0] == "Drafts"


def test_the_threading_headers_survive_the_round_trip(drafts):
    """The entire point. A reply appended to Drafts has to arrive threaded, or
    this transport is no better than the two that were ruled out."""
    from app.compose import ParentMessage, build_reply
    parent = ParentMessage(message_id="<parent@uni.edu>", references="<root@uni.edu>",
                           subject="Midterm", sender=(Mailbox("prof@uni.edu"),))
    transport, fake = drafts()
    transport.send(build_reply(parent, sender=ME, text="ok", now=NOW))

    stored = email.message_from_bytes(fake.appended[0][1], policy=email.policy.default)
    assert stored["In-Reply-To"] == "<parent@uni.edu>"
    assert "<root@uni.edu>" in stored["References"]
    assert stored["Subject"] == "Re: Midterm"


def test_bcc_is_kept_on_a_draft_and_stripped_on_a_delivery(drafts, configured, server):
    """The two rules look contradictory and are not. On delivery the envelope
    carries the blind recipients, so the header must go or they are not blind.
    On handoff there is no envelope yet -- the user's client builds one -- so
    stripping it would drop those recipients instead of hiding them."""
    msg = a_message(bcc=[Mailbox("dean@uni.edu")])
    transport, fake = drafts()
    transport.send(msg)
    assert b"dean@uni.edu" in fake.appended[0][1]

    fake_smtp = server()
    configured(fake_smtp.port).send(a_message(bcc=[Mailbox("dean@uni.edu")]))
    fake_smtp.join(timeout=5)
    assert b"dean@uni.edu" not in fake_smtp.payload
    assert "dean@uni.edu" in fake_smtp.rcpt_to


def test_the_draft_is_flagged_as_a_draft(drafts):
    """Without `\\Draft` the message shows up in the folder but the client will
    not open it in a compose window."""
    transport, fake = drafts()
    transport.send(a_message())
    assert fake.append_flags == "(\\Draft)"


def test_the_drafts_folder_is_found_by_attribute_across_locales():
    imap = FakeImap([
        rb'(\HasNoChildren) "/" "INBOX"',
        '(\\HasNoChildren \\Drafts) "/" "임시 보관함"'.encode("utf-8"),
    ])
    assert sent_folder.discover_drafts(imap) == "임시 보관함"


def test_a_localised_drafts_name_is_recognised_without_attributes():
    imap = FakeImap(['(\\HasNoChildren) "/" "임시 보관함"'.encode("utf-8")])
    assert sent_folder.discover_drafts(imap) == "임시 보관함"


def test_sent_and_drafts_are_not_confused_for_each_other():
    imap = FakeImap(DRAFTS_LISTING)
    assert sent_folder.discover_sent(imap) == "Sent Messages"
    assert sent_folder.discover_drafts(imap) == "Drafts"


def test_no_drafts_folder_is_an_actionable_message(drafts):
    transport, _ = drafts(imap=FakeDraftImap([rb'(\HasNoChildren) "/" "INBOX"']))
    with pytest.raises(base.TransportError, match="Settings"):
        transport.send(a_message())


def test_this_transport_needs_no_smtp_at_all(drafts):
    """The accounts it exists for often have SMTP submission disabled while
    IMAP syncs perfectly well."""
    transport, fake = drafts(smtp_host="", smtp_username="")
    assert transport.status().ready
    assert transport.send(a_message()).handoff == "Drafts"


def test_the_connection_is_closed_even_when_the_append_fails(drafts):
    broken = FakeDraftImap()
    broken.append_result = ("NO", [b"over quota"])
    transport, fake = drafts(imap=broken)
    with pytest.raises(base.TransportError, match="over quota"):
        transport.send(a_message())
    assert broken.logged_out
