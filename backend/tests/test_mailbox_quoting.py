"""Mailbox names with spaces, as Outlook and Gmail name their Sent folders.

imaplib passes mailbox arguments through unquoted. `APPEND Sent Items ...` is
two atoms to a real server and it answers BAD; the send had already gone, so
the only symptom was a missing copy in Sent -- found by the automatic-sending
e2e, whose fake server names the folder the way Exchange does.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.transports.imap_folders import open_imap, quoted, save_copy

sys.path.insert(0, str(Path(__file__).parent))
from fake_imap import FakeIMAP                                    # noqa: E402

RAW = b"Message-ID: <q1@example.edu>\r\nSubject: hi\r\n\r\nbody\r\n"


@pytest.mark.parametrize("name, wire", [
    ("Sent", '"Sent"'),
    ("Sent Items", '"Sent Items"'),
    ("[Gmail]/Sent Mail", '"[Gmail]/Sent Mail"'),
    ('"Already quoted"', '"Already quoted"'),
    ('odd"name', '"odd\\"name"'),
    ("back\\slash", '"back\\\\slash"'),
    ("&vPSwuA- &ycDGtA-", '"&vPSwuA- &ycDGtA-"'),   # modified UTF-7 stays as is
])
def test_quoted(name, wire):
    assert quoted(name) == wire


@pytest.mark.parametrize("sent", ["Sent Items", "[Gmail]/Sent Mail"])
def test_a_sent_copy_lands_in_a_folder_whose_name_has_a_space(sent):
    imap = FakeIMAP(username="u@example.edu", password="p", persistent=True,
                    mailboxes=[("\\HasNoChildren", "INBOX"),
                               ("\\HasNoChildren \\Sent", sent)])
    imap.start(); imap.ready.wait(5)
    try:
        client = open_imap("127.0.0.1", imap.port, "plain", "u@example.edu", "p", timeout=5)
        outcome, where = save_copy(client, RAW, "<q1@example.edu>", delays=(0.0,))
        client.logout()
    finally:
        imap.stop()
    assert (outcome, where) == ("appended", sent)
    assert [a["mailbox"] for a in imap.appended] == [sent]
    assert imap.selected == [sent], "the Message-ID search looked in the right folder"
