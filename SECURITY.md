# Security and trust boundary

College Tracker is a **single-user, local** tool. It runs on your computer,
for you, against accounts you own or are authorised to use.

## Assumptions

- **Local only.** The web UI binds to `127.0.0.1`. `run.py` refuses any other
  `HOST` unless `ALLOW_REMOTE=1`, and every request whose `Host` header is not
  a loopback name is rejected (this blocks DNS-rebinding attacks from web
  pages you visit). There is no login screen, so never expose the port to a
  network.
- **Authorised use.** Only add sites and accounts you are allowed to access,
  and follow each site's terms. Checks are read-only: the checker opens the
  pages you configured and reads their text. The only form it ever submits
  is a login form, and only in `credentials` mode.
- **Humans handle security challenges.** When a site asks for MFA, a passkey,
  a CAPTCHA or renewed consent, the check stops with **Sign-in needed**. You
  finish the step yourself in a visible browser (**Sign in** on the Sites
  page). The app never tries to bypass or automate these steps.

## What the app enforces

| Area | Safeguard |
|---|---|
| Cross-site requests | Every POST needs a per-session CSRF token. The session cookie is `SameSite=Strict` and `HttpOnly`. |
| Browser hardening | A strict Content-Security-Policy (no inline scripts or styles), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. |
| Redirects | `next=` redirects only go to same-site relative paths. |
| Check URLs | Must be `https://`. No embedded credentials. Loopback, private, link-local, `.local` and similar internal addresses are rejected unless `ALLOW_LOCAL_TARGETS=1`. |
| Redirects during checks | A Playwright route guard re-checks every request (redirects and sub-resources included), resolving DNS, and blocks internal addresses. A blocked request shows as a configuration error. |
| Display links | College, visit, task, map and meeting links must be `http(s)://`, so `javascript:` and `data:` links cannot be stored. They open with `rel="noopener noreferrer"`. The app never invents college URLs. |
| Secrets in output | Saved usernames and passwords are scrubbed from error messages and page text before anything is stored. Token-like query parameters are stripped from stored URLs. Credentials are never written to `sites.json`. |
| Debugger | `FLASK_DEBUG` defaults to `0`. Turn the Werkzeug debugger on only for local development. |
| Uploads | The Chrome CSV upload is capped at 2 MB, written to a temporary file and deleted straight after import. |

## Where secrets live (all gitignored)

| File | Contents | Notes |
|---|---|---|
| `credentials.toml` | Usernames and passwords saved from the Sites page, plus the `[imap]` email app password | Plain text, mode `600` on macOS/Linux, unless `CREDENTIAL_STORE=keyring`. In that case passwords go to Windows Credential Manager / macOS Keychain / Secret Service (service `college-tracker`) and only a marker stays in the file. Run `python run.py secure` after switching. Prefer **manual** sign-in where possible. |
| `data/auth/<site>.json` | Playwright session state (cookies, local storage) from a sign-in | Treat as a password. **Forget session** deletes it, and so does deleting the site. |
| `data/.secret_key` | Random Flask session key | Generated on first run. |
| `.env` | Settings and optional fallback credentials | Never commit it. |
| `sites.json` | Your portal URLs and checks (no passwords) | Personal, so it is gitignored. `sites.example.json` is the committed template. |

Everything else in `data/` (results, history, alerts, planner, `emails.json`) is personal
data. It is local-only and also gitignored.

### Session expiry

Sessions last as long as the website allows. When a check reaches a sign-in
page, it is marked **Sign-in needed**, an alert is raised, and later checks
for that site are skipped until you sign in again. Separately, a saved
session older than `SESSION_MAX_AGE_DAYS` (default 14, per-site override on
the site's edit page) is flagged on the Sites page as a reminder to refresh
it.

## Email

- **Gmail filters** are generated locally and imported by you. They only
  add labels and "never send to spam". They never delete, archive or
  forward.
- **Inbox sync** is opt-in. It uses IMAP over TLS with certificate
  verification. The folder is opened read-only and messages are fetched
  with `BODY.PEEK`, so nothing in the mailbox changes. Use an app password,
  not your main password, and revoke it any time from your mail provider.
- Only messages whose sender domain matches one of your colleges are kept:
  the subject, sender and a 400-character snippet. Other mail is discarded
  in memory. Lookalike domains (`college.edu.evil.com`) do not match.
- **Clear stored email** deletes `data/emails.json` without touching the
  mailbox.

## Notifications

Notifications are off by default. When `NOTIFY_WEBHOOK_URL` is set (https
only), alert titles and short summaries are sent to that webhook. Page text
and credentials are never sent. Use a private topic or channel.

## Reporting a problem

Open an issue without including secrets, or contact the maintainer privately
for anything sensitive.
