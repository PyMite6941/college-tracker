# College Tracker

A local-first college planning dashboard. Today it can log into configured
college portals with Playwright, pull registration-checklist pages, and show
the results. The product is growing into one place to monitor authorized
websites, manage application work, and plan campus visits.

Credentials are currently read from `.env` (gitignored); website definitions
live in `sites.json`. The broader roadmap is tracked in
[GitHub Issues](https://github.com/PyMite6941/college-tracker/issues).

## Quick start — Windows (cmd / PowerShell)

```powershell
.\setup.bat
.\run.bat
# open http://127.0.0.1:5000
```

Or with the activated venv:

```powershell
.venv\Scripts\activate
python run.py
```

## Quick start — macOS / Linux (bash / zsh)

```bash
chmod +x setup.sh run.sh
./setup.sh
./run.sh
# open http://127.0.0.1:5000
```

### Scripts

| Script | Platform | Purpose |
|--------|----------|---------|
| `setup.bat` | Windows | Create `.venv`, install deps + Playwright, create `.env` |
| `setup.sh` | macOS / Linux | Same as `setup.bat` |
| `run.bat` | Windows | Start the Flask site on localhost |
| `run.sh` | macOS / Linux | Same as `run.bat` |
| `run.py` | All | Python entrypoint (reads `HOST`/`PORT`/`FLASK_DEBUG` from `.env`) |

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

## Product direction

The goal is a personal college-planning hub, not an unrestricted bot for
logging into arbitrary accounts. Checks should be read-only, configured by
the user, and limited to accounts and sites the user is authorized to access.
When a site requires MFA, a passkey, CAPTCHA, or renewed consent, the user
should complete that step in a visible browser; the app must not bypass site
security controls. Secrets and reusable browser sessions must stay local and
out of Git.

Planned work, in a suggested implementation order:

1. [Harden local app and site configuration boundaries (#4)](https://github.com/PyMite6941/college-tracker/issues/4)
2. [Add human-in-the-loop sign-in and safer session handling (#6)](https://github.com/PyMite6941/college-tracker/issues/6)
3. [Support configurable read-only checks across authorized sites (#3)](https://github.com/PyMite6941/college-tracker/issues/3)
4. [Track check history, content changes, and optional alerts (#1)](https://github.com/PyMite6941/college-tracker/issues/1)
5. [Add application tasks, deadlines, and actionable source links (#5)](https://github.com/PyMite6941/college-tracker/issues/5)
6. [Add college profiles and campus visit planning (#2)](https://github.com/PyMite6941/college-tracker/issues/2)

### Campus visit planning

The visit planner is planned, not implemented yet. It should let you save
official campus-tour and admissions-event links, then track in-person or
virtual visit dates, registration and confirmation status, travel notes,
questions to ask, and follow-up tasks. It should also support college fairs
and user-added map/directions links. College-specific registration URLs
must come from the college's official site rather than being guessed, and
reminders should be opt-in.

Useful starting points:

- [College Board: visiting a college](https://bigfuture.collegeboard.org/plan-for-college/college-search/visit-a-college)
- [NACAC: college fairs](https://www.nacacnet.org/college-fairs/)
- For a specific tour or event, use the college's official admissions or
  campus-visit page and save that source URL with the visit.

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
  run.py           # Python entrypoint (all platforms)
  run.bat          # Windows: start the site
  run.sh           # macOS / Linux: start the site
  sites.json       # Websites to log into and check (committed)
  setup.bat        # Windows: one-command setup
  setup.sh         # macOS / Linux: one-command setup
  .env.example     # Credential template (copy to .env, never commit)
  .gitignore
  requirements.txt
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
- Use only on sites and accounts you are authorized to access, and respect
  the site's terms and access controls.
- The current credential-based login flow is limited; human-assisted
  reauthentication and safer session handling are tracked in issue #6.
