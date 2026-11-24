#!/usr/bin/env python3
"""Entrypoint: run the College Tracker website on localhost.

Reads HOST / PORT / FLASK_DEBUG from .env. Refuses to bind to a
non-loopback address unless ALLOW_REMOTE=1 (see SECURITY.md).

    python run.py           start the website
    python run.py check     run all checks once from the command line
    python run.py remind    record due reminders (for a scheduled task)
    python run.py email     sync college email once (read-only IMAP)
    python run.py auto      run whatever Settings -> Automation says is due, then exit
    python run.py secure    move saved passwords to match CREDENTIAL_STORE (keyring or file)
"""
from __future__ import annotations

import sys


def cli(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "serve"
    if cmd == "serve":
        from app import main
        main()
        return 0
    if cmd == "check":
        import os
        import scraper
        headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
        results = scraper.run_checks(headless=headless, progress=print)
        for r in results:
            line = f"{r['site_name']} / {r['check_name']}: {r['status']}"
            if r.get("error"):
                line += f" - {r['error']}"
            print(line)
        return 0 if all(r["status"] == "ok" for r in results) else 1
    if cmd == "remind":
        import planner
        print(f"{planner.send_due_reminders()} reminder(s) recorded.")
        return 0
    if cmd == "email":
        import inbox
        import scheduler
        try:
            out = inbox.sync(progress=print)
        finally:
            scheduler.mark_run("email")
        print(f"{out['added']} new, {out['matched']} college message(s).")
        return 0
    if cmd == "secure":
        import scraper
        out = scraper.migrate_credentials()
        if out["store"] == "keyring" and not out["keyring_available"]:
            print("No OS keyring is available; passwords stay in credentials.toml.")
            return 1
        where = "the OS keyring" if out["store"] == "keyring" else "credentials.toml (plain text)"
        print(f"{out['sites']} site password(s){' and the email password' if out['imap'] else ''} now stored in {where}.")
        return 0
    if cmd == "auto":
        import os
        import inbox
        import planner
        import scheduler
        import scraper
        s = scheduler.load()
        ran = []
        if scheduler.due("checks", s["check_hours"], s=s):
            headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
            try:
                scraper.run_checks(headless=headless, progress=print)
            finally:
                scheduler.mark_run("checks")
            ran.append("checks")
        if scheduler.due("email", s["email_hours"], s=s) and inbox.account_config()["configured"]:
            try:
                inbox.sync(progress=print)
            finally:
                scheduler.mark_run("email")
            ran.append("email")
        if s["reminders"]:
            planner.send_due_reminders()
        print("Ran: " + (", ".join(ran) or "nothing was due"))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parent / ".env")
    sys.exit(cli(sys.argv))
