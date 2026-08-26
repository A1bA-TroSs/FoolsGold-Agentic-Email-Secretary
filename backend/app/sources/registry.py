"""Picks the mail source the user selected in Settings."""
from __future__ import annotations

from .. import db
from .applemail import AppleMailSource
from .base import MailSource
from .graph_source import GraphSource

SOURCES: dict[str, type[MailSource]] = {
    "graph": GraphSource,
    "applemail": AppleMailSource,
}

_cache: dict[str, MailSource] = {}


def get_source(name: str | None = None) -> MailSource:
    name = (name or db.get_setting("mail_source", "applemail") or "applemail").lower()
    cls = SOURCES.get(name, AppleMailSource)
    if cls.name not in _cache:
        _cache[cls.name] = cls()
    return _cache[cls.name]


def all_status() -> dict[str, dict]:
    return {name: cls().status().as_dict() for name, cls in SOURCES.items()}
