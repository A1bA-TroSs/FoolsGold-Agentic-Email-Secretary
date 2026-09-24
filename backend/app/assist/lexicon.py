"""The words this mailbox actually uses.

A spell checker that does not know your world is mostly wrong about it. On the
reference mailbox, every one of the top false alarms was a real word in the
user's life -- UROP, CLE, LANG4030, HKUST, a colleague's name -- and not one of
them is in any dictionary. The user would have to teach each one to the checker
by hand, which nobody does, and until they did the checker would be noise.

So the mailbox teaches it instead: a token the user's own mail uses repeatedly
is a word, not a typo. This is the same idea as a personal dictionary, built
from evidence that already exists rather than from a chore.

Deliberately *not* a frequency ranking or an embedding: it is a set, it is
cheap, and it can be explained in one sentence to the person whose mail it is.
"""
from __future__ import annotations

import re
from functools import lru_cache

from .. import db

# Latin-script tokens only: this list exists to silence an English checker.
# Hyphens and apostrophes stay inside a token so "co-op" and "don't" count once.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]{1,}")

MIN_COUNT = 3          # seen three times in your own mail = a word you use
MAX_WORDS = 40_000     # a ceiling so a huge mailbox cannot eat memory


def _count(text: str, seen: dict[str, int]) -> None:
    for token in _TOKEN.findall(text or ""):
        key = token.lower()
        seen[key] = seen.get(key, 0) + 1


@lru_cache(maxsize=4)
def _build(generation: int, min_count: int) -> frozenset[str]:
    seen: dict[str, int] = {}
    with db.connect() as conn:
        for row in conn.execute("SELECT subject, body_text FROM emails"):
            _count(row["subject"], seen)
            _count(row["body_text"], seen)
    words = [w for w, n in seen.items() if n >= min_count]
    words.sort(key=lambda w: -seen[w])
    return frozenset(words[:MAX_WORDS])


def familiar_words(min_count: int = MIN_COUNT) -> frozenset[str]:
    """Tokens the mailbox uses at least `min_count` times.

    Cached against the number of stored emails, so a sync that brings in new
    mail rebuilds it and a settings screen that asks twice does not."""
    try:
        with db.connect() as conn:
            generation = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    except Exception:                                   # noqa: BLE001
        return frozenset()
    return _build(int(generation), min_count)
