# Connecting Fools Gold to your Outlook mailbox

Fools Gold talks to Outlook through the Microsoft Graph API. Microsoft requires
every application that does this to be *registered*, and the registration has to
live under your own Microsoft account — nobody can create it for you, and there
is no shared key to hand out.

This is a one-time, ten-minute job. You will end up with one value: an
**Application (client) ID**, which you paste into Settings.

---

## 1. Open the app registration portal

Go to <https://entra.microsoft.com> and sign in with the **same Microsoft
account whose mail you want to read**.

In the left sidebar: **Applications → App registrations → + New registration**.

> If you have both a personal and a school/work Microsoft account, sign in with
> the one that owns the inbox. You can register under either, as long as you
> pick the multi-tenant + personal option in step 2.

## 2. Fill in the registration

| Field | Value |
|---|---|
| **Name** | `Fools Gold` (only you ever see this) |
| **Supported account types** | **Accounts in any organizational directory and personal Microsoft accounts** |
| **Redirect URI** | Platform: **Public client/native (mobile & desktop)**<br>URI: `http://localhost:8765/api/auth/callback` |

Click **Register**.

**The redirect URI must match exactly**, including the port and the path. If you
change `FOOLSGOLD_PORT`, change this too. Microsoft permits plain `http` for
`localhost` specifically — this is the standard loopback flow for desktop apps,
not a downgrade.

## 3. Copy the client ID

On the app's **Overview** page, copy **Application (client) ID**. It looks like
`3f9a2c10-8e4b-4c77-b0a1-9d2e5f8a1234`.

**You do not need a client secret.** Fools Gold is a *public client*: it runs on
your machine, where no secret can actually be kept secret. It uses PKCE instead,
which is what Microsoft recommends for desktop apps.

## 4. Confirm the permissions

Go to **API permissions**. You should already see `User.Read` under Microsoft
Graph. Add the one that matters:

**+ Add a permission → Microsoft Graph → Delegated permissions** → search for
and tick:

- `Mail.Read` — read the signed-in user's mail
- `offline_access` — stay signed in without re-prompting every hour

Click **Add permissions**.

> **Delegated**, not Application. Delegated means the app acts as *you*, with
> exactly your access and nothing more. Note there is no `Mail.Send` and no
> `Mail.ReadWrite` here — v1.0 cannot write to your mailbox even if it tried.

If your account is a school or work one, you may see *"Admin consent required"*
next to a permission. `Mail.Read` normally does not require it, but some
universities restrict it. If it is blocked, your IT administrator has to approve
it — or you can point Fools Gold at a personal Outlook account instead.

## 5. Make sure the public client flow is allowed

Go to **Authentication**. Scroll to **Advanced settings** and confirm
**Allow public client flows** is set to **Yes**. Save if you changed it.

## 6. Paste it into Fools Gold

Launch the app, open **Settings** (gear, bottom of the left rail), paste the
client ID under **Microsoft app registration**, and hit **Save client ID**.

Then go back, type your Outlook address, and click **Sign in with Microsoft**.
Your real browser opens on `login.microsoftonline.com` — check that address bar,
because a well-built app should never ask you to type a Microsoft password
inside its own window. Approve the consent screen and the tab will tell you to
come back.

---

## If something goes wrong

**"AADSTS50011: redirect URI does not match"**
The URI in the portal differs from what the app sent. It must be exactly
`http://localhost:8765/api/auth/callback` — check for a trailing slash, `https`
instead of `http`, or `127.0.0.1` instead of `localhost`.

**"AADSTS7000218: request body must contain client_assertion or client_secret"**
The registration is being treated as a confidential client. Set **Allow public
client flows → Yes** (step 5).

**"AADSTS65001: user or administrator has not consented"**
Approve the consent screen, or ask your admin if your organization requires it.

**The browser tab succeeded, but the app still says "waiting"**
The app polls every two seconds for up to five minutes. If it never notices,
the backend probably is not on port 8765 — check the terminal you launched from.

**"Session expired — please sign in to Outlook again"**
The refresh token was revoked (password change, MFA policy change, or you
removed the app from <https://myapps.microsoft.com>). Just sign in again.

---

## What Fools Gold does with the access

- Tokens are encrypted with a local key at `~/.foolsgold/secret.key` (0600) and
  stored in `~/.foolsgold/foolsgold.db`. They never leave the machine.
- Only `GET` requests are ever issued against Graph. v1.0 has no code path that
  writes to your mailbox.
- To revoke access entirely: **Sign out** in Settings, and remove the app at
  <https://myapps.microsoft.com>. Deleting `~/.foolsgold/` erases the local
  cache and tokens.
