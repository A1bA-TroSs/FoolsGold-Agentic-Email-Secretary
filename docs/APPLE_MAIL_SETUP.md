# Reading mail without any app registration

This is the setup path that needs **no Microsoft app registration, no OAuth
flow, and no cooperation from your university's IT department.**

## Why this exists

Microsoft requires an Entra app registration before any program can read a
mailbox over the Graph API. Many university tenants block students from
creating one — you open the portal and get "액세스 권한 없음 / You don't have
access". That is a policy decision by your institution and there is no way
around it from the outside.

But Apple Mail has *already* signed into your mailbox and downloaded the
messages. They are sitting on your disk right now. Fool's Gold reads those.

| | Apple Mail | Outlook (Graph) |
|---|---|---|
| App registration | not needed | required |
| Sign-in | macOS already did it | OAuth every session |
| Works if IT blocks Entra | **yes** | no |
| Network traffic | none | every sync |
| Freshness | whatever Mail has synced | live |
| Mail leaves your Mac | never | never (read-only API) |

## Setup

### 1. Add your account to Apple Mail

1. Open **Mail**
2. **Mail → Settings…** in the menu bar (or `⌘ ,`)
3. The **Accounts** tab
4. The small **`+`** at the **bottom of the account list on the left** — not
   anything in the settings panel on the right
5. Pick **Microsoft Exchange**. This is correct for both `outlook.com` and
   university Microsoft 365 addresses.
6. Enter your name and address, then choose **Sign in with Microsoft**

macOS handles the whole OAuth flow itself. That is the entire point of this path.

**Two things that look like they should work but don't:**

- **"Add another email address" / alias.** That creates an extra *iCloud*
  address, which is why it only offers iCloud domains. It has nothing to do with
  connecting another mailbox.
- **"Other Mail Account…"** in the provider list. That tries plain IMAP with
  basic authentication, which Microsoft has already removed from Exchange Online
  and is retiring for Outlook.com. It will fail. Use **Microsoft Exchange**.
- **File → Import Mailboxes.** That reads mbox/olm files exported from another
  app. It does not connect an account.

Let Mail finish downloading before you continue. A large mailbox takes a while,
and Fool's Gold can only see what Mail has actually fetched.

**If your university blocks this.** Some tenants have not consented to Apple's
account app, or use Conditional Access to allow only the official Outlook
client. You would see *"Need admin approval"* or *"blocked by your
organization's policy"* on the Microsoft sign-in screen. That is a real block and
not something to work around — but schools that do this almost always still
allow **Outlook for Mac**, which also keeps a local copy of your mail. Say so and
an Outlook-for-Mac source can be added.

### 2. Grant Full Disk Access

macOS protects `~/Library/Mail` behind TCC, so the process doing the reading
needs Full Disk Access. Open **System Settings → Privacy & Security → Full Disk
Access** and add:

- **during development**, whichever app you launch from — usually **Terminal**
  (or iTerm). The permission attaches to the launching app, not to Python.
- **once packaged**, **Fool's Gold** itself.

Quit and reopen the app afterwards. macOS does not apply the change to an
already-running process.

Without it you will see: *"macOS is blocking access to ~/Library/Mail."*

### 3. Tell Fool's Gold your address

In **Settings → Mail source**, make sure **Apple Mail on this Mac** is selected
and type your own email address.

This is not cosmetic. "Addressed **to** me" versus "I'm on **Cc**" is the single
most reliable structural signal an assistant has, and without your address
Fool's Gold cannot tell the difference.

### 4. Check, then refresh

Before launching the app, you can confirm what is actually on disk:

```bash
python3 scripts/check-mail.py
```

It reports whether the store exists, whether this process is allowed to read it,
and how many messages sit in each mailbox — so you can tell "Mail hasn't synced"
apart from "macOS is blocking me", which otherwise look identical.

Then open Fool's Gold and hit the refresh icon. The count should roughly match.

## What it actually reads

```
~/Library/Mail/V<n>/<account-uuid>/<Mailbox>.mbox/.../Messages/*.emlx
```

Each `.emlx` file is three parts: a line with a byte count, the RFC822 message,
and an Apple plist holding Mail's own metadata. Fool's Gold parses these
directly with the Python standard library.

**It deliberately does not read Mail's `Envelope Index` SQLite database.** That
schema changes between macOS releases and the file is locked while Mail is
running. The read/unread flag we need is in each message's own plist, so the
fragile dependency would buy nothing.

Trash, Junk, Spam, Sent, Drafts, Outbox and Archive are always excluded. By
default only INBOX is read; untick "Inbox only" in Settings to include your
other folders.

Fool's Gold only ever **reads** these files. It never writes, moves or deletes
anything in `~/Library/Mail`.

## Troubleshooting

**"No ~/Library/Mail directory found"**
Apple Mail has never run, or has no accounts. Do step 1 — and note the `+` is at
the bottom of the *account list*, not in the panel on the right.

**"macOS is blocking access to ~/Library/Mail"**
Full Disk Access, step 2 — and remember to restart the app.

**"Found ~/Library/Mail but no V<n> message store inside it"**
Mail is still setting up. Give it a few minutes and hit Re-check.

**The inbox is empty but Mail shows plenty**
Your account may not label its inbox `INBOX`. Fool's Gold falls back to all
non-excluded mailboxes automatically, but you can also untick "Inbox only".
Also check that **Days of mail to keep** in Settings covers the period you want.

**Messages look older than they should**
Files are pre-filtered by modification time before parsing, so a huge mailbox
stays fast. The *displayed* date always comes from the real `Date:` header.

**I want a non-standard location**
Settings → Mail source → **Mail store path**. An explicit path always wins over
auto-detection.

## Switching to Outlook later

Nothing is lost. Settings → Mail source → Outlook. If you eventually get an
Entra client ID, see [ENTRA_SETUP.md](ENTRA_SETUP.md). Your priorities,
classifications and digests are keyed to the mail itself and carry over.
