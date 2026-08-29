# Connecting Outlook / Microsoft 365 directly

**Read this first:** most people should not need it. If the account is already
in Apple Mail, use the default source — it needs no registration and no
administrator. This route exists for people who want Fools Gold to talk to
Microsoft Graph itself.

It requires registering an application in **your own** Entra (Azure AD) tenant.
Many organisations, universities included, block that for ordinary accounts. If
you get "You do not have access", that is your answer: use Apple Mail instead.

## 1. Register the application

1. <https://entra.microsoft.com> → Applications → **App registrations** → New
2. Name it anything.
3. Supported account types: **Accounts in any organizational directory and
   personal Microsoft accounts**, unless your tenant requires otherwise.
4. Redirect URI: platform **Mobile and desktop applications**, value
   `http://localhost:8765/api/auth/callback`
5. Register, then copy the **Application (client) ID**.

There is no client secret. This is a public client using the authorization code
flow with PKCE, because a secret shipped inside a desktop app is not a secret.

## 2. Permissions

API permissions → Microsoft Graph → **Delegated**:

- `User.Read`
- `Mail.Read`

Read-only on purpose — the app never sends, deletes or moves anything. If your
tenant requires admin consent, an administrator has to grant it once.

## 3. Enter the client ID

Settings → Mail source → Outlook → paste the Application (client) ID → Sign in.
A browser window opens for Microsoft; the app receives the result on
`localhost:8765` and never sees your password.

## Tokens

The refresh token is encrypted at rest in `~/.foolsgold/foolsgold.db` with the
key at `~/.foolsgold/secret.key` (mode 0600). Signing out deletes it. Deleting
`~/.foolsgold/` removes everything.

## Troubleshooting

| What you see | What it means |
| --- | --- |
| "You do not have access" on the portal | Your tenant blocks app registration. Use Apple Mail. |
| `AADSTS50011` redirect mismatch | The redirect URI must match exactly, including the port. |
| `AADSTS65001` consent required | An administrator must consent to the two delegated permissions. |
| Sign-in works, no mail appears | Check the permissions are **delegated**, not application. |
