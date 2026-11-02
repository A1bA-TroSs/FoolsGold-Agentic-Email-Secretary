# Fools Gold

A macOS email secretary that ranks your inbox and pulls out deadlines, with AI optional.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/readme/hero-dark.png">
  <img alt="Diagram: mail from Apple Mail or Outlook is stored in a local database, ranked on structural signals with optional AI, and shown as a priority list, checklist, calendar and recap." src="docs/readme/hero.png">
</picture>

## Why use it

**It reads the mail already on your Mac.** The default source parses Apple Mail's local store, so there is no account to connect and no server of ours to trust. A hosted filter such as [SaneBox](https://www.sanebox.com/help/155-how-does-sanebox-work) connects to your mail server instead. macOS asks you to grant Full Disk Access first.

**Ranking works with no AI and no key.** With the default provider, `none`, mail is ranked on deadlines, senders and direct address. Add a model only if you want one: Ollama runs on your Mac, and Anthropic, OpenAI or GitHub Copilot use your own account. A cloud provider receives nothing until you have read who gets what and pressed Allow.

**It says why, and what you owe.** [Outlook's Focused Inbox](https://support.microsoft.com/en-us/outlook/mail/focused-inbox-for-outlook) splits mail into two tabs. Fools Gold orders one list, labels rows with a reason such as a deadline or "addressed to you", and puts due dates on a calendar with a checklist per day.

Real interface with fixture data, captured in August 2026. It predates the "Piled up" recap and later menu changes.

![Priority inbox showing four items that need attention, with deadlines and reasons, beside a briefing of what is crucial today.](docs/screenshot-gold.png)

## Quick start

You need macOS, Python 3.11 or newer and Node.js 18 or newer. macOS ships Python 3.9, so install a newer one first with `brew install python@3.13`.

```bash
git clone https://github.com/A1bA-TroSs/FoolsGold-Agentic-Email-Secretary.git
cd FoolsGold-Agentic-Email-Secretary
./scripts/setup.sh    # builds the Python environment and the UI
npm start
```

`python3 scripts/check-mail.py` diagnoses mail store access without touching the app.

- [Set up Apple Mail](docs/APPLE_MAIL_SETUP.md): the default, including the Full Disk Access step
- [Set up Outlook](docs/ENTRA_SETUP.md): needs an app registration in your own Entra tenant, which many organisations restrict

## What it does

- Priority list, all-mail list and a "Piled up" recap of what arrived while you were away
- To-dos read from message bodies, when a model is connected; deadlines are also found without one
- Calendar with a checklist per day; ticked items stay visible, struck through
- Morning and evening checklist reminders
- Mute or highlight a sender, not just a message
- Ranking that can learn from your actions once you switch learning on
- Reply and forward with a review step: the exact message is shown before it can be sent, or saved to Drafts if the server refuses
- Interface in English, Korean, Chinese and Japanese, with four colour themes

## How it works

An Electron shell runs a Python backend on 127.0.0.1 and a React interface. Everything is kept in `~/.foolsgold/`:

```
~/.foolsgold/foolsgold.db     mail cache, rankings, tasks, settings
~/.foolsgold/secret.key       encrypts API keys and OAuth tokens (mode 0600)
```

The folder holds a copy of your mail and the key stored beside what it encrypts, so treat it like your mailbox. Delete it and the app is factory-fresh. There is no telemetry. See the [draft privacy policy](docs/PRIVACY.md) for what each provider receives.

## Status and limits

- Version 1.2.7, run from source. No packaged release is published yet.
- macOS only, and the packaging config targets Apple Silicon. The Apple Mail route reads only what Apple Mail has downloaded.
- Without a model, deadlines are read only from ISO and English date forms, so Korean dates are missed.
- The privacy policy is a draft with unfilled fields.
- Not for shared machines: the local database is protected only by your user account.
- Not in the Mac App Store, whose sandbox blocks reading `~/Library/Mail`.

## Contributing

Branch from `develop` and open pull requests against it, using Conventional Commits. Turn on the commit message check with:

```bash
git config core.hooksPath .githooks
```

```bash
cd backend && .venv/bin/python -m pytest   # backend tests
npm run test:electron                      # reminders and backend guard
cd frontend && npm run dev                 # UI with hot reload
```

## License

No license yet, all rights reserved.
