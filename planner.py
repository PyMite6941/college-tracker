"""College profiles, campus visits/events and application tasks.

These are planning records only. The app never registers for an event,
submits a form, or invents a college URL: every link is supplied by the
user, and is validated to be a plain http(s) link before it is saved.

Stored in ``data/planner.json``:

    {"colleges": [...], "visits": [...], "tasks": [...]}
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from security import ConfigError, validate_link
from storage import data_dir, new_id, read_json, slugify, write_json

VISIT_KINDS = {
    "campus_tour": "Campus tour",
    "open_house": "Open house",
    "info_session": "Information session",
    "class_visit": "Class visit",
    "college_fair": "College fair",
    "virtual_tour": "Virtual tour",
    "interview": "Interview",
    "other": "Other event",
}
VISIT_FORMATS = {"in_person": "In person", "virtual": "Virtual", "fair": "College fair"}
RSVP_STATES = {
    "planned": "Planned (not registered)",
    "registered": "Registered",
    "confirmed": "Confirmed",
    "completed": "Completed",
    "cancelled": "Cancelled",
}
TASK_CATEGORIES = {
    "application": "Application",
    "essay": "Essay",
    "financial_aid": "Financial aid",
    "scholarship": "Scholarship",
    "transcript": "Transcript request",
    "test_scores": "Test scores",
    "recommendation": "Recommendation",
    "visit": "Visit",
    "housing": "Housing",
    "registration": "Registration",
    "other": "Other",
}
TASK_STATUSES = {"todo": "To do", "in_progress": "In progress", "done": "Done"}
PRIORITIES = {"high": "High", "normal": "Normal", "low": "Low"}

UPCOMING_DAYS = 14


def planner_path() -> Path:
    return data_dir() / "planner.json"


def load() -> dict:
    data = read_json(planner_path(), {})
    for key in ("colleges", "visits", "tasks"):
        data.setdefault(key, [])
    return data


def save(data: dict) -> None:
    write_json(planner_path(), data)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> date:
    return datetime.now().astimezone().date()


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------

def _text(form, key: str, limit: int = 4000, required: bool = False, label: str = "") -> str:
    v = (form.get(key) or "").strip()
    if required and not v:
        raise ConfigError(f"{label or key} is required.")
    if len(v) > limit:
        raise ConfigError(f"{label or key} is too long (max {limit} characters).")
    return v


def _choice(form, key: str, choices: dict, default: str) -> str:
    v = (form.get(key) or default).strip()
    if v not in choices:
        raise ConfigError(f"Unknown {key}: {v!r}.")
    return v


def _date(form, key: str, required: bool = False, label: str = "") -> str:
    v = (form.get(key) or "").strip()
    if not v:
        if required:
            raise ConfigError(f"{label or key} is required.")
        return ""
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        raise ConfigError(f"{label or key} must be a date (YYYY-MM-DD).") from None


def _time(form, key: str, label: str = "") -> str:
    v = (form.get(key) or "").strip()
    if not v:
        return ""
    try:
        return time.fromisoformat(v).strftime("%H:%M")
    except ValueError:
        raise ConfigError(f"{label or key} must be a time (HH:MM).") from None


def _tz(form, key: str = "timezone") -> str:
    v = (form.get(key) or "").strip()
    if not v:
        return local_tz_name()
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"Unknown time zone {v!r} (use a name like America/New_York).") from None
    return v


def _money(form, key: str) -> float | None:
    v = (form.get(key) or "").strip().replace("$", "").replace(",", "")
    if not v:
        return None
    try:
        n = round(float(v), 2)
    except ValueError:
        raise ConfigError("Estimated cost must be a number.") from None
    if n < 0:
        raise ConfigError("Estimated cost cannot be negative.")
    return n


def _lines(form, key: str, limit: int = 50) -> list[str]:
    raw = form.get(key) or ""
    items = [l.strip().lstrip("-*•[] ").strip() for l in raw.splitlines()]
    return [i[:300] for i in items if i][:limit]


def _checklist(form, key: str, existing: list[dict] | None = None) -> list[dict]:
    """Textarea of lines -> [{text, done}], preserving done flags by text."""
    done = {i["text"]: i.get("done", False) for i in (existing or [])}
    return [{"text": t, "done": done.get(t, False)} for t in _lines(form, key)]


def _reminder(form) -> tuple[bool, int]:
    remind = (form.get("remind") or "") in {"on", "1", "true", "yes"}
    try:
        days = int(form.get("remind_days") or 3)
    except ValueError:
        raise ConfigError("Reminder days must be a whole number.") from None
    if not 0 <= days <= 60:
        raise ConfigError("Reminder days must be between 0 and 60.")
    return remind, days


def local_tz_name() -> str:
    try:
        from tzlocal import get_localzone_name  # optional
        return get_localzone_name()
    except Exception:
        pass
    import os
    return os.getenv("TZ") or "UTC"


def timezone_choices() -> list[str]:
    common = ["America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
              "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu",
              "Europe/London", "Asia/Bangkok", "UTC"]
    try:
        rest = sorted(available_timezones() - set(common))
    except Exception:
        rest = []
    return common + rest


# ---------------------------------------------------------------------------
# Colleges
# ---------------------------------------------------------------------------

def get_college(data: dict, college_id: str) -> dict | None:
    return next((c for c in data["colleges"] if c["id"] == college_id), None)


def college_from_form(form, existing: dict | None = None, data: dict | None = None) -> dict:
    name = _text(form, "name", 200, required=True, label="College name")
    c = dict(existing or {})
    if not existing:
        base = slugify(name)
        cid, n = base, 2
        taken = {x["id"] for x in (data or {}).get("colleges", [])}
        while cid in taken:
            cid, n = f"{base}-{n}", n + 1
        c["id"] = cid
        c["created_at"] = _now_iso()
    c.update({
        "name": name,
        "location": _text(form, "location", 200),
        "admissions_url": validate_link(form.get("admissions_url"), "Official admissions URL"),
        "visit_url": validate_link(form.get("visit_url"), "Official visit/tour page"),
        "portal_site_id": _text(form, "portal_site_id", 64),
        "email_domains": parse_domains(form.get("email_domains")),
        "notes": _text(form, "notes", 8000),
        "updated_at": _now_iso(),
    })
    return c


DOMAIN_RE = re.compile(r"^(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def parse_domains(raw) -> list[str]:
    """'northfield.edu, @mail.northfield.edu' -> ['northfield.edu', 'mail.northfield.edu']."""
    if isinstance(raw, list):
        items = raw
    else:
        items = re.split(r"[\s,;]+", raw or "")
    out = []
    for d in items:
        d = d.strip().lower().lstrip("@").rstrip(".")
        if not d:
            continue
        if "@" in d:
            d = d.split("@", 1)[1]
        if not DOMAIN_RE.match(d):
            raise ConfigError(f"{d!r} is not an email domain (example: northfield.edu).")
        if d not in out:
            out.append(d)
    return out[:20]


def suggest_domain(url: str) -> str:
    """Registrable-looking domain from a URL the user supplied (a hint, never saved automatically)."""
    from urllib.parse import urlsplit
    host = (urlsplit(url or "").hostname or "").lower()
    parts = host.split(".")
    if len(parts) >= 3 and parts[-1] == "edu":
        return ".".join(parts[-2:])
    if len(parts) >= 3 and parts[0] in {"www", "admissions", "admission", "apply", "visit"}:
        return ".".join(parts[1:])
    return host


def delete_college(data: dict, college_id: str) -> None:
    if any(v["college_id"] == college_id for v in data["visits"]) or \
            any(t["college_id"] == college_id for t in data["tasks"]):
        raise ConfigError("Delete or move this college's visits and tasks first.")
    data["colleges"] = [c for c in data["colleges"] if c["id"] != college_id]


# ---------------------------------------------------------------------------
# Visits / events
# ---------------------------------------------------------------------------

def get_visit(data: dict, visit_id: str) -> dict | None:
    return next((v for v in data["visits"] if v["id"] == visit_id), None)


def visit_from_form(form, data: dict, existing: dict | None = None) -> dict:
    college_id = _text(form, "college_id", 64, required=True, label="College")
    if not get_college(data, college_id):
        raise ConfigError("Choose a college for this visit (add it on the Colleges page first).")
    kind = _choice(form, "kind", VISIT_KINDS, "campus_tour")
    fmt = _choice(form, "format", VISIT_FORMATS,
                  "fair" if kind == "college_fair" else "virtual" if kind == "virtual_tour" else "in_person")
    start, end = _time(form, "start_time", "Start time"), _time(form, "end_time", "End time")
    if start and end and end <= start:
        raise ConfigError("End time must be after the start time.")
    meeting_url = validate_link(form.get("meeting_url"), "Meeting link")
    directions_url = validate_link(form.get("directions_url"), "Map/directions link")
    location = _text(form, "location", 300)

    v = dict(existing or {})
    if not existing:
        v["id"] = new_id("v")
        v["created_at"] = _now_iso()
        v["status"] = "planned"
        v["reminded_for"] = ""
    remind, remind_days = _reminder(form)
    v.update({
        "college_id": college_id,
        "title": _text(form, "title", 200, required=True, label="Event name"),
        "kind": kind,
        "format": fmt,
        "date": _date(form, "date", required=True, label="Date"),
        "start_time": start,
        "end_time": end,
        "timezone": _tz(form),
        "location": location,
        "source_url": validate_link(form.get("source_url"), "Official event/registration page", required=True),
        "status": _choice(form, "status", RSVP_STATES, v.get("status", "planned")),
        "confirmation": _text(form, "confirmation", 1000),
        "directions_url": directions_url,
        "meeting_url": meeting_url,
        "logistics": _text(form, "logistics", 8000),
        "accessibility": _text(form, "accessibility", 2000),
        "estimated_cost": _money(form, "estimated_cost"),
        "prep": _checklist(form, "prep", v.get("prep")),
        "questions": _lines(form, "questions"),
        "notes": _text(form, "notes", 8000),
        "contacts": _text(form, "contacts", 2000),
        "followups": _checklist(form, "followups", v.get("followups")),
        "remind": remind,
        "remind_days": remind_days,
        "updated_at": _now_iso(),
    })
    if existing and (existing.get("date") != v["date"] or existing.get("start_time") != v["start_time"]):
        v["reminded_for"] = ""
    return v


def set_visit_status(v: dict, status: str) -> None:
    if status not in RSVP_STATES:
        raise ConfigError(f"Unknown status {status!r}.")
    v["status"] = status
    v["updated_at"] = _now_iso()


def visit_start(v: dict) -> datetime:
    try:
        tz = ZoneInfo(v.get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    t = time.fromisoformat(v["start_time"]) if v.get("start_time") else time(0, 0)
    return datetime.combine(date.fromisoformat(v["date"]), t, tzinfo=tz)


def visit_end(v: dict) -> datetime:
    start = visit_start(v)
    if v.get("end_time"):
        return start.replace(hour=int(v["end_time"][:2]), minute=int(v["end_time"][3:5]))
    if v.get("start_time"):
        return start + timedelta(hours=1)
    return start + timedelta(days=1)


def visit_is_past(v: dict, now: datetime | None = None) -> bool:
    return visit_end(v) <= (now or datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def get_task(data: dict, task_id: str) -> dict | None:
    return next((t for t in data["tasks"] if t["id"] == task_id), None)


def task_from_form(form, data: dict, existing: dict | None = None) -> dict:
    college_id = _text(form, "college_id", 64, required=True, label="College")
    if not get_college(data, college_id):
        raise ConfigError("Choose a college for this task (add it on the Colleges page first).")
    visit_id = _text(form, "visit_id", 64)
    if visit_id and not get_visit(data, visit_id):
        raise ConfigError("Linked visit not found.")
    t = dict(existing or {})
    if not existing:
        t["id"] = new_id("t")
        t["created_at"] = _now_iso()
        t["reminded_for"] = ""
    remind, remind_days = _reminder(form)
    status = _choice(form, "status", TASK_STATUSES, t.get("status", "todo"))
    t.update({
        "title": _text(form, "title", 200, required=True, label="Task"),
        "college_id": college_id,
        "category": _choice(form, "category", TASK_CATEGORIES, "application"),
        "due_date": _date(form, "due_date", label="Due date"),
        "status": status,
        "priority": _choice(form, "priority", PRIORITIES, "normal"),
        "source_url": validate_link(form.get("source_url"), "Official source/action URL"),
        "notes": _text(form, "notes", 8000),
        "visit_id": visit_id,
        "check_ref": _text(form, "check_ref", 130),
        "remind": remind,
        "remind_days": remind_days,
        "updated_at": _now_iso(),
    })
    if status == "done" and not t.get("completed_at"):
        t["completed_at"] = _now_iso()
    if status != "done":
        t.pop("completed_at", None)
    if existing and existing.get("due_date") != t["due_date"]:
        t["reminded_for"] = ""
    return t


def set_task_status(t: dict, status: str) -> None:
    if status not in TASK_STATUSES:
        raise ConfigError(f"Unknown status {status!r}.")
    t["status"] = status
    t["updated_at"] = _now_iso()
    if status == "done":
        t["completed_at"] = _now_iso()
    else:
        t.pop("completed_at", None)


def task_sort_key(t: dict):
    return (t["status"] == "done", t.get("due_date") or "9999-12-31",
            {"high": 0, "normal": 1, "low": 2}.get(t.get("priority"), 1), t["title"].lower())


# ---------------------------------------------------------------------------
# Dashboard summary and reminders
# ---------------------------------------------------------------------------

def summary(data: dict | None = None, now: datetime | None = None) -> dict:
    data = data or load()
    now = now or datetime.now(timezone.utc)
    d_today = today()
    horizon = d_today + timedelta(days=UPCOMING_DAYS)
    colleges = {c["id"]: c for c in data["colleges"]}

    open_tasks = [t for t in data["tasks"] if t["status"] != "done"]
    overdue = sorted([t for t in open_tasks if t.get("due_date") and date.fromisoformat(t["due_date"]) < d_today],
                     key=task_sort_key)
    upcoming = sorted([t for t in open_tasks if t.get("due_date")
                       and d_today <= date.fromisoformat(t["due_date"]) <= horizon], key=task_sort_key)
    undated = sorted([t for t in open_tasks if not t.get("due_date")], key=task_sort_key)

    active = [v for v in data["visits"] if v["status"] not in {"completed", "cancelled"}]
    upcoming_visits = sorted([v for v in active if not visit_is_past(v, now)], key=visit_start)
    needs_wrapup = sorted([v for v in active if visit_is_past(v, now)], key=visit_start)
    followups = []
    for v in data["visits"]:
        if v["status"] == "completed":
            for item in v.get("followups", []):
                if not item.get("done"):
                    followups.append({"visit": v, "text": item["text"]})

    return {
        "colleges": colleges,
        "overdue_tasks": overdue,
        "upcoming_tasks": upcoming,
        "undated_tasks": undated,
        "upcoming_visits": upcoming_visits,
        "needs_wrapup": needs_wrapup,
        "open_followups": followups,
        "horizon_days": UPCOMING_DAYS,
    }


def due_reminders(data: dict | None = None, now: datetime | None = None) -> list[dict]:
    """Opt-in reminders whose window has opened and that have not been sent
    for the current date. Returns [{kind, item, title, body, key}]."""
    data = data or load()
    now = now or datetime.now(timezone.utc)
    d_today = today()
    colleges = {c["id"]: c["name"] for c in data["colleges"]}
    out = []
    for t in data["tasks"]:
        if not t.get("remind") or t["status"] == "done" or not t.get("due_date"):
            continue
        due = date.fromisoformat(t["due_date"])
        if d_today >= due - timedelta(days=t.get("remind_days", 3)) and t.get("reminded_for") != t["due_date"]:
            when = "overdue" if due < d_today else "today" if due == d_today else f"due {due.isoformat()}"
            out.append({"kind": "task", "item": t, "key": f"task:{t['id']}:{t['due_date']}",
                        "title": f"Task {when}: {t['title']}",
                        "body": f"{colleges.get(t['college_id'], '')} — {TASK_CATEGORIES.get(t['category'], '')}"})
    for v in data["visits"]:
        if not v.get("remind") or v["status"] in {"completed", "cancelled"}:
            continue
        start = visit_start(v)
        if visit_is_past(v, now):
            continue
        if now >= start - timedelta(days=v.get("remind_days", 3)) and v.get("reminded_for") != v["date"]:
            state = RSVP_STATES[v["status"]]
            out.append({"kind": "visit", "item": v, "key": f"visit:{v['id']}:{v['date']}",
                        "title": f"Upcoming visit {v['date']}: {v['title']}",
                        "body": f"{colleges.get(v['college_id'], '')} — {state}"})
    return out


def send_due_reminders() -> int:
    """Record due reminders as alerts (which notify if configured)."""
    from history import add_alert

    data = load()
    sent = 0
    for r in due_reminders(data):
        link = f"/tasks/{r['item']['id']}/edit" if r["kind"] == "task" else f"/visits/{r['item']['id']}"
        add_alert("reminder", r["title"], r["body"], link, key=r["key"])
        r["item"]["reminded_for"] = r["item"].get("due_date") or r["item"].get("date")
        sent += 1
    if sent:
        save(data)
    return sent


# ---------------------------------------------------------------------------
# Calendar export (.ics), user-initiated download only
# ---------------------------------------------------------------------------

def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")


def to_ics(data: dict) -> str:
    colleges = {c["id"]: c["name"] for c in data["colleges"]}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//college-tracker//EN", "CALSCALE:GREGORIAN"]
    for v in data["visits"]:
        if v["status"] == "cancelled":
            continue
        lines += ["BEGIN:VEVENT", f"UID:{v['id']}@college-tracker", f"DTSTAMP:{stamp}"]
        if v.get("start_time"):
            s = visit_start(v).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            e = visit_end(v).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            lines += [f"DTSTART:{s}", f"DTEND:{e}"]
        else:
            d = date.fromisoformat(v["date"])
            lines += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"]
        desc = f"{RSVP_STATES[v['status']]}. Official page: {v['source_url']}"
        if v.get("meeting_url"):
            desc += f"\nMeeting link: {v['meeting_url']}"
        lines += [f"SUMMARY:{_ics_escape(colleges.get(v['college_id'], '') + ': ' + v['title'])}",
                  f"DESCRIPTION:{_ics_escape(desc)}", f"URL:{v['source_url']}"]
        if v.get("location"):
            lines.append(f"LOCATION:{_ics_escape(v['location'])}")
        lines.append("END:VEVENT")
    for t in data["tasks"]:
        if t["status"] == "done" or not t.get("due_date"):
            continue
        d = date.fromisoformat(t["due_date"])
        lines += ["BEGIN:VEVENT", f"UID:{t['id']}@college-tracker", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
                  f"SUMMARY:{_ics_escape('Due: ' + t['title'] + ' (' + colleges.get(t['college_id'], '') + ')')}"]
        if t.get("source_url"):
            lines.append(f"URL:{t['source_url']}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
