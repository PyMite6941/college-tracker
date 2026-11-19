# College Tracker

Localhost website that logs into college portals with Playwright, pulls
registration-checklist pages, and displays the results. Credentials are
stored in `.env` (gitignored). The list of websites to check lives in
`sites.json`.

## Quick start (Windows)

```powershell
.\setup.bat
.venv\Scripts\python run.py
# open http://127.0.0.1:5000
```

Or with the activated venv:

```powershell
.venv\Scripts\activate
python run.py
```

## Quick start (macOS / Linux)

```bash
chmod +x setup.sh
./setup.sh
.venv/bin/python run.py
# open http://127.0.0.1:5000
```

## How it works

1. `sites.json` lists the websites to log into and check (login URL,
   registration-checklist URL, field selectors, and the `.env` variable
   names for username/password).
2. `.env` holds the stored credentials and site URLs. It is gitignored —
   never commit it.
3. `scraper.py` launches Chromium with Playwright, logs into each site,
   navigates to its checklist page, and saves extracted text to
   `data/results.json`.
4. `app.py` is a Flask site on `http://127.0.0.1:5000` that shows the
   latest scrape results. Click **Run check now** to re-scrape.

## Managing sites (web UI)

You can add and remove websites without editing files by hand:

- **Dashboard** (`/`) — click **Add a website to track** to save a new
  site to `sites.json` through a form.
- **Manage sites** (`/sites`) — view all configured sites and delete
  ones you no longer need.

Changes are saved to `sites.json` immediately. Credentials still live
in `.env` (gitignored) and are referenced by `user_env` / `pass_env`.

## File layout

```
college-tracker/
  app.py           # Flask website (localhost)
  scraper.py       # Playwright login + checklist extraction
  run.py           # Entrypoint
  sites.json       # Websites to log into and check (committed)
  .env.example     # Credential template (copy to .env, never commit .env)
  .gitignore
  requirements.txt
  setup.bat / setup.sh
  tests/           # mock HTML pages for end-to-end testing
  data/            # runtime output: results.json (gitignored)
```

## Adding a site manually

Add an entry to `sites.json`:

```json
{
  "id": "mycollege",
  "name": "My College Portal",
  "login_url": "https://college.edu/login",
  "checklist_url": "https://college.edu/student/registration-checklist",
  "user_env": "MYCOLLEGE_USER",
  "pass_env": "MYCOLLEGE_PASS",
  "selectors": {
    "username": "input[name=\"email\"]",
    "password": "input[type=\"password\"]",
    "submit": "button[type=\"submit\"]"
  },
  "checklist_selectors": ["main", ".checklist", "body"]
}
```

Then add `MYCOLLEGE_USER` and `MYCOLLEGE_PASS` to `.env`.

## Notes

- Default selectors target common login forms; override per site in
  `sites.json` if the markup differs.
- Set `PLAYWRIGHT_HEADLESS=false` in `.env` to watch the browser run
  (helpful for debugging logins).
- This is built as a college tracker now; the scraper/app split keeps it
  easy to repurpose later.
