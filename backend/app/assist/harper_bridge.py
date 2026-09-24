"""Harper, spawned as a subprocess, or nothing at all.

Harper is Apache-2.0, runs on-device as WebAssembly, and checks **English
only** -- which is the honest division of labour here: LanguageTool has no
Korean either (not in its 40-language list) and would put a Java runtime in the
DMG, and a 4B model has no business correcting spelling in any language (the
best purpose-built Korean GEC system measures F0.5 31.70).

Every failure mode returns an empty list. A checker that cannot run must make
the app quieter, never broken: no node, no module, a crash, a timeout, or
output that is not JSON all mean "no English issues found today".
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

# Precision first. Harper emits 21 lint kinds; these are the ones that are
# nearly always right about an email. Style, Readability, WordChoice and
# friends are opinions, and an opinion that fires on mail the user would have
# sent anyway is how a checker trains people to ignore it.
# Measured on the reference mailbox before this list was cut: 45 flags over
# 2,771 words of the user's own sent mail. Capitalization wanted "Github" ->
# "GitHub" and "hkust" -> "HKUST"; Readability objected to one long sentence;
# Grammar, Usage and WordChoice produced opinions. All of it was mail the user
# had already sent and was happy with. Spelling and Typo are what remain,
# because "sucessful" and "teh" are facts and the rest are preferences.
DEFAULT_KINDS = ("Spelling", "Typo")

# Things that are not words and must never be called misspellings: commit
# hashes, ids, ligature debris from pasted PDFs, version fragments. Measured:
# "fd6ff14", "da49", "a33" and "fi" were four of the thirteen remaining flags
# on the reference mailbox's own sent mail.
_NOT_A_WORD = re.compile(r"^(?:.{1,2}|[0-9A-Fa-f]{4,}|.*\d.*|.*[_/\\@:].*)$")

_SCRIPT = Path(__file__).with_name("harper_lint.mjs")
TIMEOUT = 8.0


def _node() -> str | None:
    return os.environ.get("FG_NODE") or shutil.which("node")


def _module_dir() -> str:
    """Where `harper.js` is installed. The repo root in development; overridden
    by the packaged app, which ships its own copy."""
    override = os.environ.get("FG_HARPER_DIR", "").strip()
    if override:
        return override
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "node_modules" / "harper.js").is_dir():
            return str(parent)
    return str(here.parents[3]) if len(here.parents) > 3 else "."


def keep(evidence: str, known: frozenset[str] | set[str] = frozenset()) -> bool:
    """Whether a flagged token is worth showing. Pure, so it can be tested
    without a Node process, and separate because every rule in it was added in
    response to a measured false alarm on the user's own sent mail."""
    token = (evidence or "").strip()
    if not token:
        return False
    if token.lower() in known:
        return False                         # a word this mailbox uses is a word
    if " " in token:
        return False                         # a phrase lint ("out last" -> "outlast") is an opinion
    if token.isupper():
        return False                         # an acronym no dictionary has met
    if _NOT_A_WORD.match(token):
        return False                         # an id, a hash, or two stray letters
    return True


def available() -> bool:
    node = _node()
    return bool(node) and _SCRIPT.is_file() and \
        (Path(_module_dir()) / "node_modules" / "harper.js").is_dir()


def lint(text: str, kinds: Sequence[str] = DEFAULT_KINDS,
         familiar: frozenset[str] | None = None,
         dialect: str = "") -> list[tuple[int, int, str, str]]:
    """(start, end, kind, evidence) for each English issue. Offsets index `text`.

    `familiar` is the mailbox's own vocabulary (see lexicon.py); anything in it
    is not a typo, whatever the dictionary thinks. `dialect` is Harper's
    English variant -- American by default, which is Harper's default too, and
    a setting because "fulfilment" is not a misspelling in Hong Kong."""
    if not text.strip() or not available():
        return []
    try:
        done = subprocess.run(
            [_node() or "node", str(_SCRIPT)],
            input=json.dumps({"text": text, "kinds": list(kinds),
                              "dialect": dialect or os.environ.get("FG_DIALECT", "")}),
            capture_output=True, text=True, timeout=TIMEOUT, cwd=_module_dir(),
        )
        if done.returncode != 0:
            return []
        found = json.loads(done.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    known = familiar or frozenset()
    out: list[tuple[int, int, str, str]] = []
    for item in found:
        try:
            evidence = str(item.get("evidence", ""))
            if not keep(evidence, known):
                continue
            out.append((int(item["start"]), int(item["end"]),
                        str(item.get("kind", "")), evidence))
        except (KeyError, TypeError, ValueError):
            continue
    return out
