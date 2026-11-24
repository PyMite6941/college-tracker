"""Optional in-app automation: run checks, email sync and reminders on a timer.

Off by default. Settings live in ``data/settings.json`` (edited on the
Settings page). The scheduler only runs while the website is open
(``python run.py``); for unattended runs use Task Scheduler / cron with
``python run.py auto`` instead. Jobs never overlap: if something else is
running, the tick is skipped and retried a minute later.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from security import ConfigError
from storage import data_dir, read_json, write_json

DEFAULTS = {"check_hours": 0, "email_hours": 0, "reminders": True, "last": {}}
TICK_SECONDS = 60
_thread: threading.Thread | None = None
_stop = threading.Event()


def settings_path():
    return data_dir() / "settings.json"


def load() -> dict:
    s = {**DEFAULTS, **read_json(settings_path(), {})}
    s["last"] = dict(s.get("last") or {})
    return s


def save_from_form(form) -> dict:
    s = load()
    try:
        check_hours = float(form.get("check_hours") or 0)
        email_hours = float(form.get("email_hours") or 0)
    except ValueError:
        raise ConfigError("Intervals must be numbers of hours.") from None
    for v, name in ((check_hours, "Portal checks"), (email_hours, "Email sync")):
        if v and not 1 <= v <= 168:
            raise ConfigError(f"{name}: use 0 (off) or between 1 and 168 hours.")
    s.update(check_hours=check_hours, email_hours=email_hours, reminders=form.get("reminders") == "on")
    write_json(settings_path(), s)
    return s


def mark_run(kind: str) -> None:
    s = load()
    s["last"][kind] = datetime.now(timezone.utc).isoformat()
    write_json(settings_path(), s)


def due(kind: str, hours: float, now: datetime | None = None, s: dict | None = None) -> bool:
    if not hours:
        return False
    s = s or load()
    last = s["last"].get(kind)
    if not last:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        return now - datetime.fromisoformat(last) >= timedelta(hours=hours)
    except ValueError:
        return True


def next_run(kind: str, hours: float, s: dict | None = None) -> str:
    s = s or load()
    if not hours:
        return ""
    last = s["last"].get(kind)
    if not last:
        return "on the next minute"
    try:
        return (datetime.fromisoformat(last) + timedelta(hours=hours)).isoformat()
    except ValueError:
        return ""


def _run_checks(progress=None):
    import os
    import scraper
    try:
        return scraper.run_checks(headless=os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false",
                                  progress=progress)
    finally:
        mark_run("checks")


def _run_email(progress=None):
    import inbox
    try:
        return inbox.sync(progress=progress)
    finally:
        mark_run("email")


def tick(now: datetime | None = None) -> str | None:
    """Start at most one due job. Returns its kind, or None."""
    import jobs
    import planner
    s = load()
    if jobs.is_running():
        return None
    if due("checks", s["check_hours"], now, s):
        if jobs.start("check", "Scheduled check of all sites", _run_checks):
            return "checks"
    elif due("email", s["email_hours"], now, s):
        import inbox
        if inbox.account_config()["configured"]:
            if jobs.start("email", "Scheduled email sync", _run_email):
                return "email"
        else:
            mark_run("email")  # not set up: don't retry every minute
    if s["reminders"]:
        planner.send_due_reminders()
    return None


def start_background() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return

    def loop():
        while not _stop.wait(TICK_SECONDS):
            try:
                tick()
            except Exception:  # never take the web app down
                import traceback
                traceback.print_exc()

    _thread = threading.Thread(target=loop, name="scheduler", daemon=True)
    _thread.start()
