#!/usr/bin/env python3
"""Diagnose what Fools Gold can actually see on this Mac.

Run this before launching the app. It answers three questions in order:
  1. Is there a mail store at all? (did Apple Mail finish downloading?)
  2. Can this process read it? (Full Disk Access)
  3. What would Fools Gold import?

    python3 scripts/check-mail.py

No arguments, no dependencies, reads nothing but message counts and dates.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

APPLE_MAIL = Path.home() / "Library" / "Mail"
OUTLOOK_MAC = (
    Path.home() / "Library" / "Group Containers"
    / "UBF8T346G9.Office" / "Outlook" / "Outlook 15 Profiles"
)

EXCLUDED = {
    "trash", "deleted messages", "deleted items", "junk", "junk e-mail", "spam",
    "sent", "sent messages", "sent items", "drafts", "outbox", "archive", "notes", "rss",
}

GREEN, RED, YELLOW, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = BOLD = OFF = ""


def ok(msg: str) -> None:
    print(f"  {GREEN}OK{OFF}    {msg}")


def bad(msg: str) -> None:
    print(f"  {RED}NO{OFF}    {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}HMM{OFF}   {msg}")


def head(msg: str) -> None:
    print(f"\n{BOLD}{msg}{OFF}")


def newest_version_dir(root: Path) -> Path | None:
    versions = [
        (int(p.name[1:]), p)
        for p in root.iterdir()
        if p.is_dir() and p.name.startswith("V") and p.name[1:].isdigit()
    ]
    return max(versions)[1] if versions else None


def scan(store: Path) -> tuple[Counter, float | None, int]:
    """Count .emlx per mailbox without parsing any of them.

    os.walk hides directory errors unless you ask for them, and a store blocked
    by Full Disk Access looks identical to an empty one. Collect the errors so a
    permission problem reports itself as a permission problem.
    """
    per_mailbox: Counter = Counter()
    newest: float | None = None
    unreadable = 0
    errors: list[OSError] = []

    for dirpath, dirnames, filenames in os.walk(store, followlinks=False, onerror=errors.append):
        dirnames[:] = [d for d in dirnames if d != "Attachments"]
        if os.path.basename(dirpath) != "Messages":
            continue
        try:
            rel = Path(dirpath).relative_to(store).parts
        except ValueError:
            rel = Path(dirpath).parts
        chain = [p[:-5] for p in rel if p.endswith(".mbox")]
        if not chain:
            continue
        label = "/".join(chain)
        for name in filenames:
            if not name.endswith(".emlx"):
                continue
            per_mailbox[label] += 1
            try:
                mtime = os.stat(os.path.join(dirpath, name)).st_mtime
                if newest is None or mtime > newest:
                    newest = mtime
            except OSError:
                unreadable += 1

    if not per_mailbox:
        for exc in errors:
            if isinstance(exc, PermissionError):
                raise exc
    return per_mailbox, newest, unreadable


def main() -> int:
    print(f"{BOLD}Fools Gold — mail store check{OFF}")

    head("1. Apple Mail")

    # Path.exists() swallows OSError and returns False, so under a TCC block it
    # can report "does not exist" for a directory that is right there. Probe it.
    try:
        os.scandir(APPLE_MAIL).close()
    except PermissionError:
        bad(f"{APPLE_MAIL} exists, but macOS is blocking access to it.")
        print(f"{DIM}        This is Full Disk Access, not a missing mailbox.")
        print(f"        System Settings → Privacy & Security → Full Disk Access →")
        print(f"        add the app you ran this from (Terminal / iTerm), then")
        print(f"        QUIT AND REOPEN it. macOS will not apply it to a running app.{OFF}")
        return 1
    except FileNotFoundError:
        pass
    except OSError:
        pass

    if not APPLE_MAIL.exists():
        bad(f"{APPLE_MAIL} does not exist.")
        print(f"{DIM}        Apple Mail has never been set up with an account.")
        print(f"        Mail → Settings → Accounts → the + at the BOTTOM of the account")
        print(f"        list → Microsoft Exchange.{OFF}")
        return check_outlook(found_apple=False)

    ok(f"{APPLE_MAIL} exists.")

    try:
        store = newest_version_dir(APPLE_MAIL)
    except PermissionError:
        bad("macOS is blocking access to ~/Library/Mail.")
        print(f"{DIM}        This is Full Disk Access, not a broken install.")
        print(f"        System Settings → Privacy & Security → Full Disk Access →")
        print(f"        add the app you ran this from (Terminal / iTerm), then")
        print(f"        QUIT AND REOPEN it. macOS will not apply it to a running app.{OFF}")
        return 1

    if store is None:
        warn("No V<n> message store yet — Mail is probably still setting up.")
        print(f"{DIM}        Give it a few minutes and run this again.{OFF}")
        return check_outlook(found_apple=False)

    ok(f"Message store: {store}")

    try:
        per_mailbox, newest, unreadable = scan(store)
    except PermissionError:
        bad("macOS is blocking access to the message files.")
        print(f"{DIM}        This is Full Disk Access, not a broken mailbox — the store is")
        print(f"        right there, this process just is not allowed to read it.")
        print(f"        System Settings → Privacy & Security → Full Disk Access →")
        print(f"        add the app you ran this from (Terminal / iTerm), then")
        print(f"        QUIT AND REOPEN it. macOS will not apply it to a running app.{OFF}")
        return 1

    if not per_mailbox:
        warn("Store found, but no messages have been downloaded yet.")
        print(f"{DIM}        Open Mail and let it finish syncing.{OFF}")
        return check_outlook(found_apple=False)

    head("2. What Fools Gold would read")
    usable = 0
    for label, count in sorted(per_mailbox.items(), key=lambda kv: -kv[1]):
        top = label.split("/")[0].strip().lower()
        skipped = any(part.strip().lower() in EXCLUDED for part in label.split("/"))
        if skipped:
            print(f"  {DIM}skip  {label:<34} {count:>6}  (Trash/Junk/Sent are never read){OFF}")
        else:
            marker = "read " if top == "inbox" else "extra"
            note = "" if top == "inbox" else DIM + "  (only with 'Inbox only' unticked)" + OFF
            print(f"  {marker} {label:<34} {count:>6}{note}")
            if top == "inbox":
                usable += count

    print()
    if usable:
        ok(f"{usable} message(s) in INBOX ready to import.")
    else:
        warn("No INBOX found. Fools Gold falls back to all non-excluded mailboxes,")
        print(f"{DIM}        or you can untick 'Inbox only' in Settings.{OFF}")

    if newest:
        age_days = (datetime.now(timezone.utc) - datetime.fromtimestamp(newest, timezone.utc)).days
        when = datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M")
        if age_days > 3:
            warn(f"Newest message is from {when} ({age_days} days ago) — Mail may not be syncing.")
        else:
            ok(f"Newest message: {when}")
    if unreadable:
        warn(f"{unreadable} file(s) could not be read and will be skipped.")

    head("3. Next step")
    print("  Settings → Mail source → Apple Mail on this Mac, enter your address,")
    print("  then hit refresh. You should see roughly the INBOX count above.")
    check_outlook(found_apple=True)
    return 0


def check_outlook(found_apple: bool) -> int:
    head("Outlook for Mac (fallback source)")
    if not OUTLOOK_MAC.exists():
        print(f"  {DIM}Not installed. Only relevant if your university blocks Apple Mail —")
        print(f"  schools that do almost always still allow the official Outlook app.{OFF}")
        return 0 if found_apple else 1

    count = sum(1 for _ in OUTLOOK_MAC.rglob("*.olk15Message"))
    if count:
        ok(f"Outlook for Mac has {count} message(s) cached locally.")
    else:
        warn("Outlook for Mac is installed but has no local messages cached.")
    print(f"{DIM}  Fools Gold cannot read this yet. If Apple Mail is blocked for you,")
    print(f"  say so and an Outlook-for-Mac source can be added.{OFF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
