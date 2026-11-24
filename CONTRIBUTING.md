# Contributing

Thanks for helping improve College Tracker. It's a small, local-first tool,
and contributions of every size are welcome: bug reports, docs fixes, new
check types, better category rules, and tests.

## Ground rules

These are part of the project's design, not style preferences. Pull
requests that break them can't be merged.

- **Local only and authorised use.** The app serves `127.0.0.1` for the
  person at the keyboard, and only for accounts they're allowed to use. See
  [SECURITY.md](SECURITY.md).
- **Never bypass MFA, CAPTCHAs, passkeys or rate limits.** When a site needs
  a human, stop and report **Sign-in needed**.
- **Read-only.** Checks don't submit forms (other than a login form in
  `credentials` mode), register for events or change accounts. Email sync
  never marks, moves or deletes mail.
- **Never invent college URLs, dates or contact details.** Every link comes
  from the user or the college's official site.
- **No secrets or personal data in Git.** Keep `.env`, `credentials.toml`,
  `sites.json`, `data/` and CSV exports out of commits, issues and
  screenshots. Use the fictional `example.edu` demo data.
- **No inline scripts or styles.** The Content-Security-Policy blocks them.
  JavaScript goes in `static/app.js` and CSS in `static/style.css`.

## Development setup

```bash
./setup.sh            # or .\setup.bat on Windows
uv pip install -r requirements-dev.txt --python .venv
.venv/bin/python -m pytest                 # .venv\Scripts\python on Windows
.venv/bin/python -m pytest -m "not e2e"    # skip the Playwright browser tests
```

The tests use a throwaway data directory, an in-memory keyring, a local mock
portal (`tests/mock_portal.py`) and a fake IMAP server. They never touch
your real data, OS keyring or mailbox.

## Making a change

1. Open an issue first for anything bigger than a small fix, so the approach
   can be agreed.
2. Branch from `master`.
3. Add or update tests. Security-relevant code (`security.py`, URL handling,
   credentials, email sync) needs a test showing the unsafe case is rejected.
4. If the UI changed, run `python scripts/screenshots.py` and update
   `README.md` and `docs/index.html`.
5. Open a pull request. CI runs the full suite on Ubuntu and Windows.

Commit messages should be in the imperative mood and say *why* as well as
*what*. By contributing, you agree that your work is released under the
[MIT License](LICENSE).

## Reporting security problems

Don't open a public issue with exploit details or secrets. Follow
[SECURITY.md](SECURITY.md).

## Contributors

- [@PyMite6941](https://github.com/PyMite6941) — creator and maintainer

Want to be listed? Add yourself in your first pull request.
