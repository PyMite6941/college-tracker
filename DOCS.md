# College Tracker - documentation

## Architecture

- `app.py` — Flask website served on localhost (127.0.0.1:5000).
  Routes:
  - `GET /` — dashboard: latest results + configured sites; form to trigger a scan.
  - `POST /scan` — runs the Playwright scrape and reloads the dashboard.
  - `GET /sites` — pretty-prints `sites.json`.
- `scraper.py` — `run_scrape(headless=True)` drives Chromium via Playwright.
  For each site in `sites.json`: open login URL, fill credentials from
  `.env`, submit, navigate to the checklist URL, extract page title +
  visible checklist text, write `data/results.json`.
- `sites.json` — committed list of websites to log into and check.
- `.env` — local-only stored credentials and URLs (gitignored).

## Data flow

1. User edits `sites.json` and `.env` (once).
2. `POST /scan` (or manual `python scraper.py`) starts Chromium.
3. Per site: login → checklist page → extract → `data/results.json`.
4. Dashboard reads `data/results.json` on each page load.

## Credentials

Never store real passwords in `sites.json`. Put them in `.env` and
reference them via `user_env` / `pass_env`:

```
COLLEGE_USER=abc123
COLLEGE_PASS=correct-horse-battery-staple
```

`scraper.py` resolves `${VAR}` tokens inside `sites.json` URLs too.

## Selectors

Login selectors (comma-separated fallbacks, first visible wins):

- username: `input[type="email"], input[name="username"], input[name="email"]`
- password: `input[type="password"]`
- submit: `button[type="submit"], input[type="submit"]`

Checklist text is pulled from the first visible of `checklist_selectors`,
falling back to `<body>`.

## Headless vs headed

`PLAYWRIGHT_HEADLESS=true` (default). Set to `false` to watch the
browser open and run — useful when a login flow changes and selectors
need updating.

## Localhost only

The app binds to 127.0.0.1 by default (`HOST`/`PORT` in `.env`). It is
intended for local use, not public hosting.
