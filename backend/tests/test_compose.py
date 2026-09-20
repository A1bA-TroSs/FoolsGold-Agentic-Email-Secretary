"""Building outgoing mail: threading, subjects, recipients, quoting.

Every assertion here is made against a message that has been **serialised and
re-parsed**, not against the object under construction. Header folding,
encoding and continuation are where RFC 5322 bites, and an assertion on the
in-memory value never sees any of it.
"""
from __future__ import annotations

import email
import email.policy
from datetime import datetime, timezone

import pytest

from app.compose import message as C
from app.compose import Mailbox, ParentMessage


ME = Mailbox("danny@uni.edu", "Danny Park")
PROF = Mailbox("prof@uni.edu", "Prof Kim")
TA = Mailbox("ta@uni.edu", "TA")
CLASSMATE = Mailbox("friend@uni.edu", "")
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def roundtrip(msg) -> email.message.Message:
    """Serialise and re-parse -- the only way to see what actually goes out."""
    return email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)


def parent(**kw) -> ParentMessage:
    base = dict(
        message_id="<parent@uni.edu>",
        subject="Midterm room change",
        sender=(PROF,),
        to=(ME, TA),
        date="Fri, 19 Sep 2026 09:00:00 +0900",
        body_text="The midterm moves to room 302.",
    )
    base.update(kw)
    return ParentMessage(**base)


# ------------------------------------------------------------------ threading

def test_a_reply_carries_the_parent_as_in_reply_to_and_extends_references():
    msg = roundtrip(C.build_reply(parent(references="<a@x> <b@x>"), sender=ME,
                                  text="Thanks", now=NOW))
    assert msg["In-Reply-To"] == "<parent@uni.edu>"
    assert C.message_ids(msg["References"]) == ["<a@x>", "<b@x>", "<parent@uni.edu>"]


def test_a_parent_with_no_message_id_gets_no_in_reply_to_at_all():
    """RFC 5322 says omit the field. An empty `In-Reply-To:` is worse than a
    missing one -- it is a malformed header some servers reject outright."""
    msg = roundtrip(C.build_reply(parent(message_id=""), sender=ME, text="ok", now=NOW))
    assert "In-Reply-To" not in msg
    assert "References" not in msg


def test_a_single_in_reply_to_stands_in_for_a_missing_references_chain():
    """The parent was written by a client that sets In-Reply-To and not
    References. Dropping it starts a new thread beside the real one."""
    msg = roundtrip(C.build_reply(
        parent(references="", in_reply_to="<grandparent@x>"), sender=ME, text="ok", now=NOW))
    assert C.message_ids(msg["References"]) == ["<grandparent@x>", "<parent@uni.edu>"]


def test_a_multi_parent_in_reply_to_is_not_guessed_at():
    """RFC 5322 explicitly declines to define References for multiple parents.
    Inventing one fabricates a thread instead of continuing one."""
    msg = roundtrip(C.build_reply(
        parent(references="", in_reply_to="<a@x> <b@x>"), sender=ME, text="ok", now=NOW))
    assert C.message_ids(msg["References"]) == ["<parent@uni.edu>"]


def test_the_parent_id_is_not_appended_twice_when_it_already_ends_the_chain():
    msg = roundtrip(C.build_reply(parent(references="<a@x> <parent@uni.edu>"),
                                  sender=ME, text="ok", now=NOW))
    assert C.message_ids(msg["References"]) == ["<a@x>", "<parent@uni.edu>"]


def test_a_long_chain_is_trimmed_from_the_front_and_keeps_both_ends():
    """RFC 5322 caps a header line at 998 characters. Trimming the *tail*
    would detach the reply from the message it answers -- the one thing
    References exists to prevent -- so the root and the recent end survive."""
    ids = [f"<msg{n:04d}@example.edu>" for n in range(80)]
    got = C.trim_references(ids)
    assert len(C.REFERENCES_SEP.join(got)) <= C.REFERENCES_MAX
    assert got[0] == ids[0], "the thread root must survive"
    assert got[-1] == ids[-1], "the immediate parent must survive"
    assert len(got) < len(ids), "this fixture has to actually overflow"
    assert got[1:] == ids[len(ids) - len(got) + 1:], "the kept tail is contiguous"


def test_a_short_chain_is_left_exactly_alone():
    ids = ["<a@x>", "<b@x>"]
    assert C.trim_references(ids) == ids


def test_references_survives_header_folding():
    """A long References header is folded across lines by the serialiser and
    must come back whole. (This guards the round trip, not the reader: the
    parser unfolds before we see the value -- see the parametrised cases below
    for what the reader itself has to survive.)"""
    ids = [f"<msg{n:04d}@example.edu>" for n in range(20)]
    msg = roundtrip(C.build_reply(parent(references=" ".join(ids)), sender=ME,
                                  text="ok", now=NOW))
    assert "\n" in msg.as_string().split("References:")[1][:400], "fixture must fold"
    assert C.message_ids(msg["References"]) == ids + ["<parent@uni.edu>"]


@pytest.mark.parametrize("raw,want", [
    ("<a@x>", ["<a@x>"]),
    ("<a@x> <b@x>", ["<a@x>", "<b@x>"]),
    ("<a@x>\r\n\t<b@x>", ["<a@x>", "<b@x>"]),      # folded
    ("<a@x>, <b@x>", ["<a@x>", "<b@x>"]),           # comma-separated: real, non-RFC
    ("<a@x> (an old thread) <b@x>", ["<a@x>", "<b@x>"]),   # RFC 5322 CFWS comment
    ("<a@x>;<b@x>", ["<a@x>", "<b@x>"]),            # no whitespace at all
    ("bare@x", ["<bare@x>"]),                       # malformed but salvageable
    ("", []),
    ("   ", []),
])
def test_message_ids_are_read_by_bracket_not_by_split(raw, want):
    assert C.message_ids(raw) == want


def test_every_reply_gets_its_own_message_id_in_the_senders_domain():
    a = roundtrip(C.build_reply(parent(), sender=ME, text="x", now=NOW))
    b = roundtrip(C.build_reply(parent(), sender=ME, text="x", now=NOW))
    assert a["Message-ID"] != b["Message-ID"]
    assert a["Message-ID"].rstrip(">").endswith("uni.edu")


# ------------------------------------------------------------------ subjects

@pytest.mark.parametrize("given,want", [
    ("Midterm", "Re: Midterm"),
    ("Re: Midterm", "Re: Midterm"),                      # no stacking
    ("RE: Midterm", "Re: Midterm"),
    ("Re: Re: Re: Midterm", "Re: Midterm"),
    ("AW: Re: AW: Midterm", "Re: Midterm"),              # the documented pathology
    ("회신: Midterm", "Re: Midterm"),                     # Korean
    ("답장: Midterm", "Re: Midterm"),
    ("SV: Odp: Midterm", "Re: Midterm"),
    ("Re[2]: Midterm", "Re: Midterm"),                   # numbered, some clients
    ("RE : Midterm", "Re: Midterm"),                     # French spacing
    ("", "Re: (no subject)"),
    ("Re:", "Re: (no subject)"),
])
def test_reply_prefixes_never_stack_whatever_locale_produced_them(given, want):
    assert C.reply_subject(given) == want


def test_replying_to_a_forward_keeps_the_forward_marker():
    """`Fwd:` is information the recipient was shown; it is not ours to delete.
    What must not survive is `Re:` on `Re:`."""
    assert C.reply_subject("Fwd: Midterm") == "Re: Fwd: Midterm"
    assert C.reply_subject("Re: Fwd: Midterm") == "Re: Fwd: Midterm"


def test_reply_subject_is_idempotent():
    """The property that actually matters: a thread's subject cannot grow
    without bound however many times it is replied to."""
    once = C.reply_subject("AW: Midterm")
    assert C.reply_subject(once) == once
    assert C.reply_subject(C.reply_subject(once)) == once


@pytest.mark.parametrize("given,want", [
    ("Midterm", "Fwd: Midterm"),
    ("Fwd: Midterm", "Fwd: Midterm"),
    ("FW: WG: Midterm", "Fwd: Midterm"),
    ("전달: Midterm", "Fwd: Midterm"),
])
def test_forward_prefixes_do_not_stack_either(given, want):
    assert C.forward_subject(given) == want


@pytest.mark.parametrize("subject", [
    "R: D2 results",            # an initial, not Italian "R:"
    "I: the plan",              # likewise
    "[cs101] Midterm",          # a list tag is part of the subject
    "Note: room 302",           # an ordinary word before a colon
    "12:30 meeting",
])
def test_a_subject_that_merely_looks_like_a_prefix_is_left_alone(subject):
    """Over-stripping is worse than under-stripping: it silently rewrites a
    subject the user chose. Single ASCII letters are excluded for this reason,
    even though Italian really does use `R:` and `I:`."""
    assert C.reply_subject(subject) == f"Re: {subject}"


# ------------------------------------------------------------------ recipients

def test_a_plain_reply_goes_to_the_sender_only():
    to, cc = C.reply_recipients(parent(), ME)
    assert [m.key for m in to] == ["prof@uni.edu"]
    assert cc == []


def test_reply_all_keeps_the_others_and_drops_me():
    """A reply-all that includes the sender files a copy of every message in
    their own inbox, and on a list it is how loops start."""
    to, cc = C.reply_recipients(parent(cc=(CLASSMATE,)), ME, reply_all=True)
    assert [m.key for m in to] == ["prof@uni.edu"]
    assert [m.key for m in cc] == ["ta@uni.edu", "friend@uni.edu"]
    assert all(m.key != ME.key for m in to + cc)


def test_reply_to_beats_from():
    """That header exists to say 'answer somewhere else'. Ignoring it sends
    departmental replies to a noreply box."""
    p = parent(reply_to=(Mailbox("registrar@uni.edu"),))
    to, _ = C.reply_recipients(p, ME)
    assert [m.key for m in to] == ["registrar@uni.edu"]


def test_one_person_never_lands_on_both_lines():
    """`Prof@Uni.edu` and `prof@uni.edu` are one person. Comparing the raw
    strings puts them on the To line and the Cc line of the same reply."""
    p = parent(sender=(PROF,), to=(Mailbox("PROF@UNI.EDU", "Prof"), ME))
    to, cc = C.reply_recipients(p, ME, reply_all=True)
    assert [m.address.lower() for m in to] == ["prof@uni.edu"]
    assert "prof@uni.edu" not in [m.address.lower() for m in cc]
    everyone = [m.address.lower() for m in to + cc]
    assert len(everyone) == len(set(everyone))


def test_replying_to_my_own_message_still_has_a_recipient():
    """Otherwise the compose pane opens with an empty To line and no
    explanation."""
    p = parent(sender=(ME,), to=(PROF,))
    to, cc = C.reply_recipients(p, ME, reply_all=True)
    assert [m.key for m in to] == ["prof@uni.edu"]
    assert cc == []


def test_a_malformed_address_does_not_sink_the_whole_list():
    boxes = Mailbox.parse("Good <good@x.edu>, junk-with-no-at, Other <other@x.edu>")
    assert [b.key for b in boxes] == ["good@x.edu", "other@x.edu"]


def test_a_display_name_with_a_comma_survives_the_round_trip():
    """`Park, Danny <x@y>` unquoted parses as two recipients -- the name is
    silently eaten and half of it becomes a bogus address. The name has to be
    quoted on the way out, not pasted in."""
    weird = Mailbox("danny@uni.edu", "Park, Danny")
    msg = roundtrip(C.build_new(sender=ME, to=[weird], subject="hi", text="x", now=NOW))
    got = Mailbox.parse(msg["To"])
    assert len(got) == 1
    assert got[0].name == "Park, Danny"
    assert got[0].address == "danny@uni.edu"


def test_a_non_ascii_display_name_is_encoded_not_pasted_in_raw():
    msg = C.build_new(sender=Mailbox("danny@uni.edu", "박윤서"), to=[PROF],
                      subject="안녕하세요", text="본문", now=NOW)
    raw = msg.as_bytes()
    assert b"\xeb\xb0\x95" not in raw.split(b"\r\n\r\n", 1)[0], "headers must be encoded"
    assert roundtrip(msg)["Subject"] == "안녕하세요"


# ------------------------------------------------------------------ bodies

def test_a_reply_quotes_the_original_beneath_the_new_text():
    msg = roundtrip(C.build_reply(parent(), sender=ME, text="Understood, thanks.", now=NOW))
    body = msg.get_body(("plain",)).get_content()
    assert body.index("Understood, thanks.") < body.index("> The midterm moves")
    assert "Prof Kim wrote:" in body


def test_quoting_can_be_turned_off():
    msg = roundtrip(C.build_reply(parent(), sender=ME, text="ok", quote=False, now=NOW))
    assert ">" not in msg.get_body(("plain",)).get_content()


def test_an_html_reply_nests_the_original_in_a_cite_blockquote():
    msg = C.build_reply(parent(body_html="<p>room 302</p>"), sender=ME,
                        text="ok", html="<p>ok</p>", now=NOW)
    html = roundtrip(msg).get_body(("html",)).get_content()
    assert '<blockquote type="cite">' in html
    assert html.index("<p>ok</p>") < html.index("room 302")


def test_a_forward_is_inline_by_default_and_names_the_original_headers():
    msg = roundtrip(C.build_forward(parent(), sender=ME, to=[CLASSMATE], now=NOW))
    body = msg.get_body(("plain",)).get_content()
    assert "Forwarded message" in body
    assert "Prof Kim <prof@uni.edu>" in body
    assert "The midterm moves to room 302." in body
    assert msg["Subject"] == "Fwd: Midterm room change"


def test_a_forward_does_not_thread():
    """A forward starts a new conversation with a new audience. Carrying the
    parent's References would file it into a thread its recipient cannot see."""
    msg = roundtrip(C.build_forward(parent(references="<a@x>"), sender=ME,
                                    to=[CLASSMATE], now=NOW))
    assert "In-Reply-To" not in msg
    assert "References" not in msg


def test_an_attached_forward_carries_the_original_message_intact():
    """The lossless option: original headers and any signature survive."""
    original = C.build_new(sender=PROF, to=[ME], subject="Midterm room change",
                           text="The midterm moves to room 302.", now=NOW)
    msg = roundtrip(C.build_forward(
        parent(raw=original.as_bytes()), sender=ME, to=[CLASSMATE],
        as_attachment=True, now=NOW))
    carried = [p for p in msg.walk() if p.get_content_type() == "message/rfc822"]
    assert len(carried) == 1
    inner = carried[0].get_payload()[0]
    assert inner["Message-ID"] == original["Message-ID"]
    assert inner["Subject"] == "Midterm room change"


def test_an_attached_forward_falls_back_to_inline_without_the_raw_bytes():
    """`parent.raw` is only populated on the paths that have the file. Asking
    for an attachment without it must still produce a sendable message."""
    msg = roundtrip(C.build_forward(parent(raw=None), sender=ME, to=[CLASSMATE],
                                    as_attachment=True, now=NOW))
    assert "Forwarded message" in msg.get_body(("plain",)).get_content()


# ------------------------------------------------------------------ parsing in

def test_a_parent_can_be_built_from_a_real_parsed_message():
    """The `.emlx` reader and Gmail's `format=raw` both end up here, so this is
    the seam the whole reply path hangs off."""
    original = C.build_reply(parent(references="<a@x>"), sender=PROF, text="see below",
                             now=NOW)
    p = ParentMessage.from_headers(email.message_from_bytes(
        original.as_bytes(), policy=email.policy.default))
    assert p.message_id == original["Message-ID"]
    assert C.message_ids(p.references) == ["<a@x>", "<parent@uni.edu>"]
    assert p.subject == "Re: Midterm room change"
    assert [m.key for m in p.sender] == ["prof@uni.edu"]

    # and a reply to *that* continues the same chain
    msg = roundtrip(C.build_reply(p, sender=ME, text="ok", now=NOW))
    assert C.message_ids(msg["References"]) == [
        "<a@x>", "<parent@uni.edu>", original["Message-ID"]]
    assert msg["Subject"] == "Re: Midterm room change"


def test_a_three_deep_thread_keeps_one_growing_chain_and_one_stable_subject():
    """The end-to-end property. Subject stays fixed; References grows by
    exactly one identifier per hop."""
    p = parent(references="")
    seen = []
    sender, other = ME, PROF
    for _ in range(3):
        msg = C.build_reply(p, sender=sender, text="...", now=NOW)
        parsed = roundtrip(msg)
        seen.append(parsed["Subject"])
        p = ParentMessage.from_headers(parsed)
        sender, other = other, sender
    assert seen == ["Re: Midterm room change"] * 3
    assert C.message_ids(p.references) == ["<parent@uni.edu>"] + [
        m for m in C.message_ids(p.references)[1:]]
    assert len(C.message_ids(p.references)) == 3
