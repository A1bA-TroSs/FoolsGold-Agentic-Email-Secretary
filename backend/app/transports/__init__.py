"""Getting a finished message off the machine. See `base.py` for the contract."""
from .base import (
    DELIVERS, HANDS_OFF, MailTransport, NullTransport, SendResult,
    TransportError, TransportStatus,
    SENT_APPENDED, SENT_FAILED, SENT_SERVER, SENT_SKIPPED,
    check_sendable, envelope_recipients, sender_address, transmissible,
)
from .registry import all_status, get_transport

__all__ = [
    "DELIVERS", "HANDS_OFF", "MailTransport", "NullTransport", "SendResult",
    "TransportError", "TransportStatus", "SENT_APPENDED", "SENT_FAILED",
    "SENT_SERVER", "SENT_SKIPPED", "check_sendable", "envelope_recipients",
    "sender_address", "transmissible", "all_status", "get_transport",
]
