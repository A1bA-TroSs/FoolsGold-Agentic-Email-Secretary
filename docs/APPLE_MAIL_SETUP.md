# Reading mail from Apple Mail

This is the default source and needs no account registration. Fools Gold reads
the mail **already synced to your Mac** by Apple Mail — whoever your provider
is. It never signs in to anything.

## 1. Have Apple Mail hold the account

Open Mail and add the account normally (Mail → Settings → Accounts). Let it
finish downloading. Fools Gold reads what Mail has; it cannot fetch anything
Mail has not.

If your organisation blocks adding the account to Mail, that blocks this route
too — there is nothing the app can do about it.

## 2. Grant Full Disk Access

macOS protects `~/Library/Mail`. Without permission the folder looks *empty*
rather than forbidden, which is why the app says so explicitly instead of
reporting "no mail".

1. System Settings → Privacy & Security → **Full Disk Access**
2. **+**, then add the app (or your terminal, if running from source)
3. **Quit and reopen** the app — macOS only applies this at launch

## 3. Check it worked

```bash
python3 scripts/check-mail.py
```

Runs standalone, needs nothing installed, and reports what it can see: where
the store is, how many messages, and what the app would import. If it prints a
permission error, step 2 has not taken effect yet.

## Which mailboxes are read

By default, INBOX only. Settings → Mail source → *Inbox only* — turn it off to
include every non-excluded mailbox. Junk, Trash and Drafts are always skipped.

The Sent mailbox is read separately, headers only, to learn who you actually
correspond with. That is what lets a message from someone you email weekly
outrank a newsletter.

## If the store is somewhere unusual

Settings → Mail source → *Mail folder* accepts an explicit path. Leave it blank
to auto-detect. This exists for non-standard setups and for testing; you should
not normally need it.

## What is stored

Message metadata and bodies are cached in `~/.foolsgold/foolsgold.db` so that
ranking does not re-read the store every time. Delete it to start clean — your
actual mail is untouched, since Fools Gold only ever reads.
