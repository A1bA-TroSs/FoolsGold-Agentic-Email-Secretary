"""Building outgoing messages.

Deliberately transport-free. Every way this app might eventually send mail --
Microsoft Graph, the Gmail API, SMTP, or handing a file to Apple Mail -- accepts
an RFC 5322 message, so the message is built once here and the transport is
bound later. That is not tidiness: it is the only way to get threading right.
Graph's `createReply` does not document whether it sets `In-Reply-To`, and
`PATCH` cannot add `internetMessageHeaders` to a draft afterwards; Gmail
requires the RFC headers *in addition to* its own `threadId`. A convenience API
per provider would leave threading correctness undefined in three places.
"""
from .message import (
    Mailbox,
    ParentMessage,
    build_forward,
    build_new,
    build_reply,
    forward_subject,
    reply_recipients,
    reply_subject,
    strip_prefixes,
    thread_headers,
)

__all__ = [
    "Mailbox", "ParentMessage", "build_forward", "build_new", "build_reply",
    "forward_subject", "reply_recipients", "reply_subject", "strip_prefixes",
    "thread_headers",
]
