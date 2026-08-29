# Fools Gold

An email secretary that runs on your own machine. It reads the mail already on
your computer, works out what actually needs you, and keeps a calendar of what
you owe people.

Nothing leaves your machine except the text of the emails being classified, and
only if you connect an AI provider yourself.

---

## What it does

- **Ranks your inbox by what needs you**, not by arrival time — using the
  deadline, whether you were addressed directly, whether you have already
  replied, who the sender is, and how you have treated them before.
- **Reads to-dos out of the message body.** One email often carries several
  obligations on different dates; each becomes its own item.
- **Keeps a calendar** of everything due, with a checklist per day. Ticking
  something off leaves it in place, struck through — the calendar is a record
  as well as a plan.
- **Silences or highlights a correspondent**, not just a message.
- **Reminds you twice a day** with the day's list.
- Interface in English, 한국어, 中文 and 日本語.

## Requirements

- macOS (the Apple Mail source reads the local mail store)
- **Python 3.11 or newer.** macOS ships 3.9, which is too old —
  `brew install python@3.13`
- Node.js 18+

## Install

```bash
git clone <this repo>
cd FoolsGold-Agentic-Email-Secretary
./scripts/setup.sh          # builds the Python environment and the UI
npm start                   # launches the app
```

`scripts/setup.sh` checks your Python version and tells you what to install if
it is too old. If something looks wrong with the mail store,
`python3 scripts/check-mail.py` diagnoses it without touching the app.

## Connecting your mail

**Apple Mail (default, no account setup).** Reads the local store at
`~/Library/Mail` — the mail already synced to your Mac, whoever your provider
is. macOS protects that folder, so the first run asks for **Full Disk Access**:
System Settings → Privacy & Security → Full Disk Access → add the app, then
restart it. See [docs/APPLE_MAIL_SETUP.md](docs/APPLE_MAIL_SETUP.md).

**Outlook / Microsoft 365.** Needs an app registration in your own Entra
tenant, which many organisations restrict. See
[docs/ENTRA_SETUP.md](docs/ENTRA_SETUP.md). If your organisation blocks it, add
the account to Apple Mail and use the default source instead.

## Connecting AI — bring your own key

**The app ships with no AI configured and no key of any kind.** Without one it
still ranks mail, using structural signals only; it just cannot read a body for
meaning. Settings → AI provider:

| Provider | What you need | Billing |
| --- | --- | --- |
| **Anthropic** | an API key from console.anthropic.com | your account, per token |
| **OpenAI** | an API key, and optionally a custom endpoint for Azure, OpenRouter or a local server | your account, per token |
| **GitHub Copilot** | a Copilot subscription and `pip install github-copilot-sdk` | your subscription, per premium request |

Your key is encrypted at rest and never leaves your machine except to the
provider you chose. Press **Test connection** after saving — it spends one
small request and tells you plainly if something is wrong.

To keep the cost down: raise *Days to keep* and *Max messages* slowly, and note
that classification is cached per message, so each email is read once.

## Where your data lives

Everything is in `~/.foolsgold/`:

```
~/.foolsgold/foolsgold.db     mail cache, rankings, tasks, settings
~/.foolsgold/secret.key       encrypts API keys and OAuth tokens (mode 0600)
```

Delete that directory and the app is factory-fresh. It contains a copy of your
mail, so treat it like your mailbox. Nothing is uploaded anywhere, and there is
no telemetry.

## Development

```bash
cd backend && .venv/bin/python -m pytest      # 234 tests
npm run test:electron                          # reminder scheduling
cd frontend && npm run dev                     # UI with hot reload
```

## Licence

Not yet chosen.
