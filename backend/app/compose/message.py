"""RFC 5322 message construction: new mail, replies, forwards.

No network, no provider, no database. Everything here is a pure function of its
arguments, which is what makes the nasty parts -- threading and subject
prefixes -- testable at all.
"""
from __future__ import annotations

import email.message
import email.policy
import email.utils
import re
from dataclasses import dataclass
from datetime import datetime

# ---------------------------------------------------------------- addresses


@dataclass(frozen=True)
class Mailbox:
    """One address, with the display name kept separate so it can be encoded
    properly rather than pasted into the header as raw UTF-8."""
    address: str
    name: str = ""

    @property
    def key(self) -> str:
        """What two mailboxes are compared on. Addresses are case-insensitive
        in the domain and conventionally treated so in the local part too --
        treating `Prof@Uni.edu` and `prof@uni.edu` as two people puts one
        person on both the To and the Cc line of the same reply."""
        return self.address.strip().lower()

    def encode(self) -> str:
        return email.utils.formataddr((self.name, self.address))

    @classmethod
    def parse(cls, raw: str) -> tuple[Mailbox, ...]:
        """A raw header value -> mailboxes. Malformed entries are dropped
        rather than raised on: one bad address in a Cc list of twenty is not a
        reason to refuse to compose a reply."""
        out: list[Mailbox] = []
        for name, address in email.utils.getaddresses([raw or ""]):
            address = (address or "").strip()
            if "@" not in address:
                continue
            out.append(cls(address=address, name=(name or "").strip()))
        return tuple(out)


def _dedupe(boxes: list[Mailbox], seen: set[str]) -> list[Mailbox]:
    out: list[Mailbox] = []
    for box in boxes:
        if not box.key or box.key in seen:
            continue
        seen.add(box.key)
        out.append(box)
    return out


def _join(boxes: tuple[Mailbox, ...] | list[Mailbox]) -> str:
    return ", ".join(b.encode() for b in boxes)


# ---------------------------------------------------------------- the parent


@dataclass(frozen=True)
class ParentMessage:
    """The message being replied to or forwarded.

    Headers, not a database row, because the three things that actually decide
    threading -- `Message-ID`, `References`, `In-Reply-To` -- are headers this
    app does not currently store. See `from_headers`.
    """
    message_id: str = ""
    references: str = ""
    in_reply_to: str = ""
    subject: str = ""
    sender: tuple[Mailbox, ...] = ()
    reply_to: tuple[Mailbox, ...] = ()
    to: tuple[Mailbox, ...] = ()
    cc: tuple[Mailbox, ...] = ()
    date: str = ""
    body_text: str = ""
    body_html: str = ""
    raw: bytes | None = None          # only needed for message/rfc822 forwards
    # Where the message was actually delivered, from the headers the receiving
    # server adds. Not the same as To: a list mail is *to* the list and
    # *delivered to* you, and the second is the address a reply should come
    # from. Ordered as Thunderbird's catch-all rule reads them.
    delivered_to: tuple[Mailbox, ...] = ()

    @classmethod
    def from_headers(cls, message: email.message.Message, **extra) -> ParentMessage:
        """Build from a parsed message -- the `.emlx` path, and the Gmail
        `format=raw` path, both end up here."""
        def head(name: str) -> str:
            return str(message.get(name) or "").strip()
        return cls(
            message_id=head("Message-ID") or head("Message-Id"),
            references=head("References"),
            in_reply_to=head("In-Reply-To"),
            subject=head("Subject"),
            sender=Mailbox.parse(head("From")),
            reply_to=Mailbox.parse(head("Reply-To")),
            to=Mailbox.parse(head("To")),
            cc=Mailbox.parse(head("Cc")),
            date=head("Date"),
            delivered_to=tuple(
                box for name in ("Delivered-To", "Envelope-To", "X-Original-To")
                for value in (message.get_all(name) or [])
                for box in Mailbox.parse(str(value))),
            **extra,
        )


# ---------------------------------------------------------------- threading

_MSG_ID = re.compile(r"<[^<>\s]+>")


def message_ids(raw: str) -> list[str]:
    """Every `<...>` in a header value, in order.

    Folding means a `References` header arrives with newlines and arbitrary
    whitespace between entries, so this cannot be a `split()`. And a few
    mailers emit a bare `Message-ID` with no angle brackets at all; rather than
    drop the only identifier that makes threading possible, it gets wrapped.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    found = _MSG_ID.findall(raw)
    if found:
        return found
    if "<" not in raw and ">" not in raw and not re.search(r"\s", raw):
        return [f"<{raw}>"]
    return []


# Thunderbird's number, and the reason for it: RFC 5322 caps a header line at
# 998 characters, so `References` is trimmed to stay under it.
REFERENCES_MAX = 986
# RFC 5322 separates msg-ids by optional whitespace, not commas -- measuring
# with ", " and emitting " " is how a header creeps back over the limit.
REFERENCES_SEP = " "


def trim_references(ids: list[str]) -> list[str]:
    """Drop from the *front*, never the back, and always keep the first.

    The first identifier is the root of the thread and the most recent ones are
    what the receiving client matches on, so those are the two ends worth
    keeping. Dropping the tail instead would detach the reply from the message
    it is answering -- which is the one thing `References` exists to prevent.

    (This is Thunderbird's rule, read from `MimeMessageUtils.sys.mjs`. The
    often-repeated "~20 entries" cap has no source; the real constraint is
    character length.)
    """
    if not ids:
        return []
    if len(REFERENCES_SEP.join(ids)) <= REFERENCES_MAX:
        return list(ids)
    first, rest = ids[0], ids[1:]
    budget = REFERENCES_MAX - len(first)
    kept: list[str] = []
    for ident in reversed(rest):
        cost = len(ident) + len(REFERENCES_SEP)
        if cost > budget:
            break
        budget -= cost
        kept.append(ident)
    return [first] + list(reversed(kept))


def thread_headers(parent: ParentMessage) -> dict[str, str]:
    """`In-Reply-To` and `References` for a reply to `parent`, per RFC 5322
    section 3.6.4.

    The three rules, and why each one has to be written out:

    * `In-Reply-To` is the parent's `Message-ID`. If the parent has none, the
      field is **omitted entirely** -- the spec says so, and an empty
      `In-Reply-To:` is worse than none because some servers reject it.
    * `References` is the parent's `References` followed by the parent's
      `Message-ID`.
    * If the parent has no `References` but has an `In-Reply-To` holding a
      *single* identifier, that identifier stands in for the missing chain.
      The "single" matters: the spec declines to define the multi-parent case,
      so guessing there invents a thread rather than continuing one.
    """
    parent_id = message_ids(parent.message_id)[:1]
    chain = message_ids(parent.references)
    if not chain:
        inherited = message_ids(parent.in_reply_to)
        if len(inherited) == 1:
            chain = inherited
    if parent_id and (not chain or chain[-1] != parent_id[0]):
        chain = chain + parent_id
    out: dict[str, str] = {}
    if parent_id:
        out["In-Reply-To"] = parent_id[0]
    if chain:
        out["References"] = REFERENCES_SEP.join(trim_references(chain))
    return out


# ---------------------------------------------------------------- subjects

# Reply and forward abbreviations by locale. Localized clients each prepend
# their own, which is how a thread ends up titled "Re: AW: Re: AW: ..." -- so
# every one of these has to be recognised, not just the English pair.
_REPLY_TOKENS = (
    "re", "aw", "antw", "sv", "vs", "vá", "va", "odp", "res", "ref", "rif",
    "回复", "回覆", "答复", "회신", "답장", "απ", "σχετ", "השב", "bls", "rsp",
    "ynt", "réf", "rép",
)
_FORWARD_TOKENS = (
    "fw", "fwd", "wg", "tr", "rv", "enc", "vb", "vl", "doorst", "vs", "pd",
    "tov", "转发", "轉寄", "転送", "전달", "προθ", "πρθ", "העבר", "trs", "ilt",
    "i̇lt", "fs",
)


def _prefix_re(tokens: tuple[str, ...]) -> re.Pattern[str]:
    # Single ASCII letters are excluded deliberately. Italian uses "R:" and
    # "I:", but so does every subject line that happens to start with an
    # initial -- "R: D2 results" is a real email, not a reply. Same trade-off
    # priority.py makes for short search terms.
    safe = sorted({t for t in tokens if not (t.isascii() and len(t) < 2)}, key=len, reverse=True)
    alternation = "|".join(re.escape(t) for t in safe)
    # `Re[2]:` and `Re(2):` are emitted by some clients; `RE :` by French ones.
    return re.compile(
        rf"^(?:\s*(?:{alternation})\s*(?:\[\d+\]|\(\d+\))?\s*:)+\s*",
        re.IGNORECASE | re.UNICODE,
    )


_REPLY_RE = _prefix_re(_REPLY_TOKENS)
_FORWARD_RE = _prefix_re(_FORWARD_TOKENS)
NO_SUBJECT = "(no subject)"


def strip_prefixes(subject: str, pattern: re.Pattern[str]) -> str:
    """Remove a leading *run* of one kind of prefix.

    Only the leading run, and only one kind. Replying to a forwarded message
    should give `Re: Fwd: x`, not `Re: x` -- the `Fwd:` is information the
    recipient was shown and is not ours to delete. What must not survive is
    `Re:` stacking on `Re:`, which is what the run-stripping prevents.
    """
    return pattern.sub("", subject or "", count=1).strip()


def reply_subject(subject: str) -> str:
    body = strip_prefixes(subject, _REPLY_RE)
    return f"Re: {body or NO_SUBJECT}"


def forward_subject(subject: str) -> str:
    body = strip_prefixes(subject, _FORWARD_RE)
    return f"Fwd: {body or NO_SUBJECT}"


# ---------------------------------------------------------------- recipients


def reply_recipients(
    parent: ParentMessage, me: Mailbox, *, reply_all: bool = False
) -> tuple[list[Mailbox], list[Mailbox]]:
    """`(to, cc)` for a reply.

    `Reply-To` wins over `From` when present: that header exists precisely to
    say "answer somewhere else", and ignoring it sends departmental replies to
    a `noreply@` box.

    The user is removed from both lines. Not cosmetic -- a reply-all that
    includes the sender puts a copy of every message in their own inbox, and on
    a mailing list it is how loops start.
    """
    to = list(parent.reply_to or parent.sender)
    seen = {me.key}
    to = _dedupe(to, seen)
    if not reply_all:
        return to, []
    cc = _dedupe(list(parent.to) + list(parent.cc), seen)
    if not to and cc:                # everyone else dropped out; promote the Cc
        return cc, []
    return to, cc


# ---------------------------------------------------------------- quoting


def _attribution(parent: ParentMessage) -> str:
    who = parent.sender[0].name or (parent.sender[0].address if parent.sender else "")
    when = parent.date.strip()
    if when and who:
        return f"On {when}, {who} wrote:"
    if who:
        return f"{who} wrote:"
    return "Previously:"


def _quote_text(parent: ParentMessage) -> str:
    lines = (parent.body_text or "").splitlines()
    quoted = "\n".join(f"> {line}" if line else ">" for line in lines)
    return f"{_attribution(parent)}\n{quoted}\n"


def _quote_html(parent: ParentMessage) -> str:
    import html as _html
    body = parent.body_html or f"<pre>{_html.escape(parent.body_text or '')}</pre>"
    return (
        f"<p>{_html.escape(_attribution(parent))}</p>"
        f'<blockquote type="cite">{body}</blockquote>'
    )


_FORWARD_BANNER = "---------- Forwarded message ----------"


def _forward_intro(parent: ParentMessage) -> list[str]:
    rows = [_FORWARD_BANNER]
    if parent.sender:
        rows.append(f"From: {_join(parent.sender)}")
    if parent.date:
        rows.append(f"Date: {parent.date}")
    if parent.subject:
        rows.append(f"Subject: {parent.subject}")
    if parent.to:
        rows.append(f"To: {_join(parent.to)}")
    if parent.cc:
        rows.append(f"Cc: {_join(parent.cc)}")
    return rows


# ---------------------------------------------------------------- building


def _new_message(
    *,
    sender: Mailbox,
    to: list[Mailbox],
    cc: list[Mailbox],
    bcc: list[Mailbox],
    subject: str,
    text: str,
    html: str | None,
    now: datetime | None,
) -> email.message.EmailMessage:
    msg = email.message.EmailMessage(policy=email.policy.SMTP)
    msg["From"] = sender.encode()
    if to:
        msg["To"] = _join(to)
    if cc:
        msg["Cc"] = _join(cc)
    if bcc:
        msg["Bcc"] = _join(bcc)
    msg["Subject"] = subject
    msg["Date"] = email.utils.format_datetime(now) if now else email.utils.formatdate(localtime=True)
    # RFC 5322: the right-hand side should be a domain the generator controls,
    # so uniqueness is guaranteed within it. The sender's own domain is the only
    # one this app can make that claim about.
    domain = sender.address.rpartition("@")[2] or None
    msg["Message-ID"] = email.utils.make_msgid(domain=domain)
    msg.set_content(text)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    return msg


def build_new(
    *,
    sender: Mailbox,
    to: list[Mailbox],
    subject: str,
    text: str,
    html: str | None = None,
    cc: list[Mailbox] | None = None,
    bcc: list[Mailbox] | None = None,
    now: datetime | None = None,
) -> email.message.EmailMessage:
    return _new_message(
        sender=sender, to=to, cc=cc or [], bcc=bcc or [],
        subject=subject or NO_SUBJECT, text=text, html=html, now=now,
    )


def build_reply(
    parent: ParentMessage,
    *,
    sender: Mailbox,
    text: str,
    html: str | None = None,
    reply_all: bool = False,
    quote: bool = True,
    to: list[Mailbox] | None = None,
    cc: list[Mailbox] | None = None,
    bcc: list[Mailbox] | None = None,
    now: datetime | None = None,
) -> email.message.EmailMessage:
    """A reply that threads.

    `to`/`cc` override the derived recipients -- the user edited the fields in
    the compose pane and their choice outranks ours.
    """
    derived_to, derived_cc = reply_recipients(parent, sender, reply_all=reply_all)
    body_text = f"{text}\n\n{_quote_text(parent)}" if quote else text
    body_html = html
    if quote and html is not None:
        body_html = f"{html}{_quote_html(parent)}"
    msg = _new_message(
        sender=sender,
        to=to if to is not None else derived_to,
        cc=cc if cc is not None else derived_cc,
        bcc=bcc or [],
        subject=reply_subject(parent.subject),
        text=body_text,
        html=body_html,
        now=now,
    )
    for name, value in thread_headers(parent).items():
        msg[name] = value
    return msg


def build_forward(
    parent: ParentMessage,
    *,
    sender: Mailbox,
    to: list[Mailbox],
    text: str = "",
    html: str | None = None,
    as_attachment: bool = False,
    cc: list[Mailbox] | None = None,
    bcc: list[Mailbox] | None = None,
    now: datetime | None = None,
) -> email.message.EmailMessage:
    """Inline by default; `message/rfc822` on request.

    Inline is what users expect and what Thunderbird moved its default toward.
    The attachment form is lossless -- it preserves the original's headers and
    any signature -- so it is offered rather than chosen, and it needs
    `parent.raw`, which the inline form does not.
    """
    if as_attachment and parent.raw:
        msg = _new_message(
            sender=sender, to=to, cc=cc or [], bcc=bcc or [],
            subject=forward_subject(parent.subject),
            text=text or "", html=html, now=now,
        )
        carried = email.message_from_bytes(parent.raw, policy=email.policy.SMTP)
        msg.add_attachment(carried, filename=f"{forward_subject(parent.subject)}.eml")
        return msg

    intro = "\n".join(_forward_intro(parent))
    body_text = f"{text}\n\n{intro}\n\n{parent.body_text or ''}".lstrip("\n")
    body_html = html
    if html is not None:
        import html as _html
        rows = "<br>".join(_html.escape(line) for line in _forward_intro(parent))
        original = parent.body_html or f"<pre>{_html.escape(parent.body_text or '')}</pre>"
        body_html = f"{html}<p>{rows}</p>{original}"
    return _new_message(
        sender=sender, to=to, cc=cc or [], bcc=bcc or [],
        subject=forward_subject(parent.subject),
        text=body_text, html=body_html, now=now,
    )
