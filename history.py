"""Check history, change detection, alerts and opt-in notifications.

Each run of a check is appended to ``data/history/<site>/<check>.jsonl``.
A new run is compared with the most recent *successful* run, so a login
or network failure is reported as an error, never as a content change.

Retention is configurable in .env:

    HISTORY_MAX_RUNS=50      keep at most this many runs per check
    HISTORY_MAX_DAYS=90      drop runs older than this (0 = keep forever)

Notifications are off unless NOTIFY_WEBHOOK_URL is set (see .env.example).
In-app alerts are always recorded in ``data/alerts.json``.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from security import ConfigError, redact, validate_link
from storage import data_dir, new_id, read_json, write_json

ERROR_STATUSES = {"error", "network_error", "login_failed", "reauth_required", "config_error"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def history_path(site_id: str, check_id: str) -> Path:
    return data_dir() / "history" / site_id / f"{check_id}.jsonl"


def load_history(site_id: str, check_id: str) -> list[dict]:
    """Return runs oldest-first."""
    path = history_path(site_id, check_id)
    runs: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        runs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except FileNotFoundError:
        pass
    return runs


def _write_history(site_id: str, check_id: str, runs: list[dict]) -> None:
    path = history_path(site_id, check_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in runs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def apply_retention(runs: list[dict]) -> list[dict]:
    max_runs = _int_env("HISTORY_MAX_RUNS", 50)
    max_days = _int_env("HISTORY_MAX_DAYS", 90)
    if max_days:
        cutoff = _now() - timedelta(days=max_days)
        kept = []
        for r in runs:
            try:
                ts = datetime.fromisoformat(r.get("checked_at") or "")
            except ValueError:
                ts = None
            if ts is None or ts >= cutoff:
                kept.append(r)
        # Always keep the latest successful run as a baseline.
        if runs and not kept:
            kept = runs[-1:]
        runs = kept
    if max_runs and len(runs) > max_runs:
        runs = runs[-max_runs:]
    return runs


def content_hash(text: str | None) -> str | None:
    if text is None:
        return None
    normalised = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


def text_diff(old: str | None, new: str | None, context: int = 1, limit: int = 80) -> list[dict]:
    """Line diff as [{op: '+'|'-'|' ', text}]. Ignores whitespace-only edits."""
    a = [l.rstrip() for l in (old or "").splitlines() if l.strip()]
    b = [l.rstrip() for l in (new or "").splitlines() if l.strip()]
    out: list[dict] = []
    for line in difflib.unified_diff(a, b, lineterm="", n=context):
        if line.startswith(("---", "+++", "@@")):
            if line.startswith("@@") and out:
                out.append({"op": "…", "text": ""})
            continue
        op = line[:1] if line[:1] in "+- " else " "
        out.append({"op": op, "text": line[1:]})
        if len(out) >= limit:
            out.append({"op": "…", "text": "(diff truncated)"})
            break
    return out


def watch_hits(text: str | None, watch: list[str]) -> list[str]:
    """Return the watched keywords/phrases present in text (case-insensitive)."""
    low = (text or "").lower()
    return [w for w in watch if w and w.lower() in low]


def record_run(result: dict, watch: list[str] | None = None) -> dict:
    """Append a run to history, annotate it with change info, return it.

    Adds ``change`` to the result:
        first_run, changed, diff, watch_hits, watch_added, watch_removed
    """
    site_id, check_id = result["site_id"], result["check_id"]
    watch = watch or []
    runs = load_history(site_id, check_id)
    prev_ok = next((r for r in reversed(runs) if r.get("status") == "ok"), None)
    prev_any = runs[-1] if runs else None

    change = {"first_run": prev_ok is None, "changed": False, "diff": [],
              "watch_hits": [], "watch_added": [], "watch_removed": [],
              "recovered": False}

    if result.get("status") == "ok":
        result["hash"] = content_hash(result.get("excerpt"))
        hits = watch_hits(result.get("excerpt"), watch)
        change["watch_hits"] = hits
        if prev_ok is not None:
            change["changed"] = result["hash"] != prev_ok.get("hash")
            if change["changed"]:
                change["diff"] = text_diff(prev_ok.get("excerpt"), result.get("excerpt"))
            prev_hits = set(watch_hits(prev_ok.get("excerpt"), watch))
            change["watch_added"] = [h for h in hits if h not in prev_hits]
            change["watch_removed"] = sorted(prev_hits - set(hits))
        if prev_any is not None and prev_any.get("status") in ERROR_STATUSES:
            change["recovered"] = True
    result["change"] = change

    stored = {k: v for k, v in result.items() if k != "change"}
    stored["changed"] = change["changed"]
    runs.append(stored)
    _write_history(site_id, check_id, apply_retention(runs))
    return result


def history_with_diffs(site_id: str, check_id: str) -> list[dict]:
    """Runs newest-first, each successful run annotated with a diff to the
    previous successful run."""
    runs = load_history(site_id, check_id)
    out = []
    prev_ok = None
    for r in runs:
        r = dict(r)
        if r.get("status") == "ok":
            if prev_ok is not None and r.get("hash") != prev_ok.get("hash"):
                r["diff"] = text_diff(prev_ok.get("excerpt"), r.get("excerpt"))
            prev_ok = r
        out.append(r)
    return list(reversed(out))


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def alerts_path() -> Path:
    return data_dir() / "alerts.json"


def load_alerts() -> list[dict]:
    return read_json(alerts_path(), [])


def add_alert(kind: str, title: str, body: str = "", link: str = "", key: str = "") -> dict | None:
    """Record an in-app alert. ``key`` de-duplicates open alerts."""
    alerts = load_alerts()
    if key and any(a.get("key") == key and not a.get("dismissed") for a in alerts):
        return None
    alert = {"id": new_id("a"), "kind": kind, "title": title, "body": body,
             "link": link, "key": key, "created_at": _now().isoformat(),
             "dismissed": False}
    alerts.append(alert)
    alerts = alerts[-200:]
    write_json(alerts_path(), alerts)
    notify(alert)
    return alert


def dismiss_alert(alert_id: str) -> bool:
    alerts = load_alerts()
    found = False
    for a in alerts:
        if a.get("id") == alert_id or alert_id == "all":
            a["dismissed"] = True
            found = True
    write_json(alerts_path(), alerts)
    return found


def dismiss_key(key: str) -> None:
    alerts = load_alerts()
    changed = False
    for a in alerts:
        if a.get("key") == key and not a.get("dismissed"):
            a["dismissed"] = True
            changed = True
    if changed:
        write_json(alerts_path(), alerts)


def open_alerts() -> list[dict]:
    return [a for a in reversed(load_alerts()) if not a.get("dismissed")]


def alerts_for_result(result: dict, check_name: str, site_name: str) -> None:
    """Turn a recorded run into alerts according to its status and change."""
    sid, cid = result["site_id"], result["check_id"]
    label = f"{site_name} — {check_name}"
    link = f"/history/{sid}/{cid}"
    status = result.get("status")
    change = result.get("change") or {}

    if status == "reauth_required":
        add_alert("reauth", f"Sign-in needed: {site_name}",
                  result.get("error") or "The site asked you to sign in again.",
                  f"/sites#site-{sid}", key=f"reauth:{sid}")
    elif status in ERROR_STATUSES:
        add_alert("error", f"Check failed: {label}", result.get("error") or status,
                  link, key=f"error:{sid}:{cid}")
    elif status == "ok":
        if change.get("watch_added"):
            add_alert("watch", f"Watched text appeared: {label}",
                      ", ".join(change["watch_added"]), link)
        if change.get("watch_removed"):
            add_alert("watch", f"Watched text disappeared: {label}",
                      ", ".join(change["watch_removed"]), link)
        if change.get("changed") and not (change.get("watch_added") or change.get("watch_removed")):
            added = sum(1 for d in change.get("diff", []) if d["op"] == "+")
            removed = sum(1 for d in change.get("diff", []) if d["op"] == "-")
            add_alert("change", f"Page changed: {label}",
                      f"{added} line(s) added, {removed} removed.", link)


# ---------------------------------------------------------------------------
# Notifications (opt-in)
# ---------------------------------------------------------------------------

def notify_config() -> dict:
    url = os.getenv("NOTIFY_WEBHOOK_URL", "").strip()
    fmt = os.getenv("NOTIFY_FORMAT", "json").strip().lower() or "json"
    kinds = {k.strip() for k in os.getenv("NOTIFY_ON", "change,watch,reauth,error,reminder,email").split(",") if k.strip()}
    error = None
    if url:
        try:
            validate_link(url, "NOTIFY_WEBHOOK_URL", required=True)
            if not url.startswith("https://"):
                raise ConfigError("NOTIFY_WEBHOOK_URL must use https://.")
        except ConfigError as exc:
            error = str(exc)
    return {"enabled": bool(url) and error is None, "url": url, "format": fmt,
            "kinds": kinds, "error": error}


def _payload(alert: dict, fmt: str) -> tuple[bytes, str]:
    text = f"{alert['title']}\n{alert.get('body') or ''}".strip()
    if fmt == "discord":
        return json.dumps({"content": text[:1900]}).encode(), "application/json"
    if fmt == "slack":
        return json.dumps({"text": text}).encode(), "application/json"
    if fmt == "ntfy":
        return text.encode("utf-8"), "text/plain; charset=utf-8"
    body = {k: alert.get(k) for k in ("kind", "title", "body", "created_at")}
    return json.dumps(body).encode(), "application/json"


def notify(alert: dict) -> bool:
    """Send an alert to the configured webhook. Never raises."""
    cfg = notify_config()
    if not cfg["enabled"] or alert.get("kind") not in cfg["kinds"]:
        return False
    data, ctype = _payload(alert, cfg["format"])
    req = urllib.request.Request(cfg["url"], data=data, method="POST",
                                 headers={"Content-Type": ctype,
                                          "User-Agent": "college-tracker"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (https enforced)
            return 200 <= resp.status < 300
    except Exception as exc:  # notifications are best-effort
        print(f"[notify] failed: {redact(str(exc))}")
        return False
