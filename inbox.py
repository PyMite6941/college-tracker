"""College email filtering: classify mail by college and task category.

Two ways to use it:

1. **Gmail filter export** (no credentials): ``gmail_filters_xml()`` builds a
   filters file you import in Gmail (Settings → Filters → Import filters).
   Gmail then labels mail itself as ``College/<College>`` and
   ``College/<Category>``. ``search_queries()`` gives the same rules as
   plain search strings for other mail apps.

2. **Read-only inbox sync** (opt-in, IMAP over TLS): ``sync()`` reads recent
   mail with ``BODY.PEEK`` from a folder opened read-only, so nothing is
   marked read, moved or deleted. Only messages from a college's email
   domains are stored (``data/emails.json``, gitignored); everything else
   is discarded immediately. Bodies are kept only as a short snippet.

College matching uses the email domains you enter on each college profile.
Categories reuse the task categories, with keyword rules you can edit on
the Email page (stored in ``data/email_rules.json``).
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import html
import imaplib
import os
import re
import ssl
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from xml.sax.saxutils import quoteattr

import planner
from security import ConfigError, redact
from storage import data_dir, read_json, write_json

# Keyword rules per task category (case-insensitive, matched on subject + body).
DEFAULT_RULES: dict[str, list[str]] = {
    "application": ["application", "apply", "applicant portal", "early action", "early decision",
                    "regular decision", "application status", "submitted", "common app"],
    "financial_aid": ["financial aid", "fafsa", "css profile", "award letter", "aid offer", "net price",
                      "verification", "student aid report", "work-study"],
    "scholarship": ["scholarship", "merit award", "honors program", "grant"],
    "transcript": ["transcript", "school report", "mid-year report", "final report"],
    "test_scores": ["test score", "sat", "act score", "ap score", "toefl", "ielts", "duolingo english"],
    "recommendation": ["recommendation", "letter of rec", "counselor letter", "evaluator"],
    "essay": ["essay", "supplement", "writing prompt", "personal statement"],
    "visit": ["campus tour", "open house", "info session", "information session", "visit", "virtual tour",
              "college fair", "admitted student day", "interview"],
    "housing": ["housing", "residence hall", "roommate", "dorm", "meal plan"],
    "registration": ["registration", "orientation", "enrollment deposit", "enroll", "course registration",
                     "placement test", "advising"],
}
IMPORTANT_WORDS = ["action required", "action needed", "deadline", "due ", "missing", "incomplete",
                   "decision", "admitted", "congratulations", "waitlist", "deferred", "hold",
                   "verify", "final reminder", "last chance"]
MAX_SNIPPET = 400
MAX_STORED = 2000


def emails_path() -> Path:
    return data_dir() / "emails.json"


def rules_path() -> Path:
    return data_dir() / "email_rules.json"


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def load_rules() -> dict[str, list[str]]:
    saved = read_json(rules_path(), {})
    rules = {k: list(v) for k, v in DEFAULT_RULES.items()}
    for k, v in saved.items():
        if k in planner.TASK_CATEGORIES and isinstance(v, list):
            rules[k] = [str(x) for x in v]
    return rules


def save_rules_from_form(form) -> dict[str, list[str]]:
    rules = {}
    for key in planner.TASK_CATEGORIES:
        if key == "other":
            continue
        raw = form.get(f"rule_{key}")
        if raw is None:
            continue
        words = [w.strip().lower() for w in re.split(r"[\n,]", raw) if w.strip()]
        if any(len(w) > 80 for w in words):
            raise ConfigError("Keep each keyword under 80 characters.")
        rules[key] = words[:60]
    write_json(rules_path(), rules)
    return load_rules()


def reset_rules() -> None:
    try:
        rules_path().unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _domain_of(addr: str) -> str:
    return (parseaddr(addr or "")[1].rsplit("@", 1)[-1] or "").lower().strip(">").strip()


def domain_matches(domain: str, college_domain: str) -> bool:
    return domain == college_domain or domain.endswith("." + college_domain)


def match_college(from_addr: str, colleges: list[dict]) -> tuple[dict | None, str]:
    domain = _domain_of(from_addr)
    for c in colleges:
        for d in c.get("email_domains") or []:
            if domain and domain_matches(domain, d):
                return c, f"sender domain {d}"
    return None, ""


def _keyword_in(word: str, text: str) -> bool:
    # Whole-word-ish match so "sat" doesn't hit "Saturday".
    return re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])", text) is not None


def categorize(subject: str, body: str, rules: dict[str, list[str]] | None = None) -> tuple[str, list[str]]:
    """Return (category, matched keywords). Subject hits count triple."""
    rules = rules or load_rules()
    subj, text = (subject or "").lower(), (body or "").lower()[:5000]
    best, best_score, best_hits = "other", 0, []
    for cat, words in rules.items():
        score, hits = 0, []
        for w in words:
            if _keyword_in(w, subj):
                score += 3
                hits.append(w)
            elif _keyword_in(w, text):
                score += 1
                hits.append(w)
        if score > best_score:
            best, best_score, best_hits = cat, score, hits
    return best, best_hits


def is_important(subject: str, body: str) -> list[str]:
    text = f"{subject}\n{body[:3000]}".lower()
    return [w.strip() for w in IMPORTANT_WORDS if w in text]


def classify(msg: dict, colleges: list[dict], rules=None) -> dict | None:
    """Annotate a parsed message; None if it isn't from a tracked college."""
    college, why = match_college(msg["from"], colleges)
    if not college:
        return None
    category, hits = categorize(msg["subject"], msg.get("body", ""), rules)
    flags = is_important(msg["subject"], msg.get("body", ""))
    return {**{k: v for k, v in msg.items() if k != "body"},
            "snippet": re.sub(r"\s+", " ", msg.get("body", ""))[:MAX_SNIPPET].strip(),
            "college_id": college["id"], "category": category, "keywords": hits,
            "important": flags, "matched_by": why}


# ---------------------------------------------------------------------------
# Stored emails
# ---------------------------------------------------------------------------

def load_emails() -> list[dict]:
    return read_json(emails_path(), [])


def save_emails(items: list[dict]) -> None:
    items = sorted(items, key=lambda m: m.get("date") or "", reverse=True)[:MAX_STORED]
    write_json(emails_path(), items, private=True)


def reclassify_all() -> int:
    """Re-run college/category rules on stored mail (after editing rules or domains)."""
    colleges = planner.load()["colleges"]
    rules = load_rules()
    kept = []
    for m in load_emails():
        college, why = match_college(m["from"], colleges)
        if not college:
            continue
        m["college_id"], m["matched_by"] = college["id"], why
        if not m.get("category_locked"):
            m["category"], m["keywords"] = categorize(m["subject"], m.get("snippet", ""), rules)
        kept.append(m)
    save_emails(kept)
    return len(kept)


def set_email_fields(msg_id: str, **fields) -> dict | None:
    items = load_emails()
    for m in items:
        if m["id"] == msg_id:
            m.update(fields)
            save_emails(items)
            return m
    return None


def summary(items: list[dict] | None = None) -> dict:
    items = load_emails() if items is None else items
    active = [m for m in items if not m.get("done")]
    return {"total": len(items), "open": len(active),
            "important": [m for m in active if m.get("important")][:8],
            "by_category": {c: sum(1 for m in active if m["category"] == c) for c in planner.TASK_CATEGORIES}}


def web_link(m: dict) -> str:
    """Open-in-webmail link for Gmail accounts (search by Message-ID)."""
    host = (account_config().get("host") or "").lower()
    mid = (m.get("message_id") or "").strip("<>")
    if mid and "gmail" in host:
        from urllib.parse import quote
        return f"https://mail.google.com/mail/u/0/#search/rfc822msgid%3A{quote(mid)}"
    return ""


# ---------------------------------------------------------------------------
# IMAP account (credentials.toml [imap] table or .env)
# ---------------------------------------------------------------------------

def account_config() -> dict:
    from scraper import load_imap_settings
    saved = load_imap_settings()
    cfg = {
        "host": saved.get("host") or os.getenv("IMAP_HOST", ""),
        "port": int(saved.get("port") or os.getenv("IMAP_PORT") or 993),
        "username": saved.get("username") or os.getenv("IMAP_USER", ""),
        "password": saved.get("password") or os.getenv("IMAP_PASSWORD", ""),
        "folder": saved.get("folder") or os.getenv("IMAP_FOLDER", "INBOX"),
        "days": int(saved.get("days") or os.getenv("EMAIL_SYNC_DAYS") or 60),
    }
    cfg["configured"] = bool(cfg["host"] and cfg["username"] and cfg["password"])
    return cfg


HOST_RE = re.compile(r"^[a-z0-9.-]{3,253}$")


def validate_account(form) -> dict:
    host = (form.get("host") or "").strip().lower()
    if not HOST_RE.match(host) or "." not in host:
        raise ConfigError("Enter your mail server's IMAP host, e.g. imap.gmail.com.")
    try:
        port = int(form.get("port") or 993)
        days = int(form.get("days") or 60)
    except ValueError:
        raise ConfigError("Port and days must be numbers.") from None
    if not 1 <= days <= 365:
        raise ConfigError("Sync between 1 and 365 days of mail.")
    username = (form.get("username") or "").strip()
    if not username:
        raise ConfigError("Username (your email address) is required.")
    folder = (form.get("folder") or "INBOX").strip()
    if not re.fullmatch(r"[\w ./\[\]-]{1,100}", folder):
        raise ConfigError("Folder name contains unsupported characters.")
    return {"host": host, "port": port, "username": username, "folder": folder, "days": days,
            "password": form.get("password") or ""}


# ---------------------------------------------------------------------------
# IMAP sync (read-only)
# ---------------------------------------------------------------------------

def _decode(value) -> str:
    if value is None:
        return ""
    return str(value)


def _text_of(msg) -> str:
    """Best-effort plain text from a (possibly truncated) message."""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is None:
        part = msg
    try:
        content = part.get_content()
    except Exception:
        try:
            content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        except Exception:
            content = ""
    if not isinstance(content, str):
        content = ""
    if part.get_content_type() == "text/html" or "<html" in content[:500].lower():
        content = re.sub(r"(?is)<(script|style).*?</\1>", " ", content)
        content = html.unescape(re.sub(r"(?s)<[^>]+>", " ", content))
    return re.sub(r"[ \t\r\f\v]+", " ", content).strip()


def parse_message(raw: bytes, uid: str = "") -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    try:
        date = parsedate_to_datetime(msg["Date"]).astimezone(timezone.utc).isoformat() if msg["Date"] else ""
    except (TypeError, ValueError):
        date = ""
    mid = _decode(msg["Message-ID"]).strip()
    from_ = _decode(msg["From"])
    subject = _decode(msg["Subject"])
    ident = hashlib.sha256((mid or f"{from_}|{subject}|{date}").encode()).hexdigest()[:16]
    return {"id": ident, "uid": uid, "message_id": mid, "from": from_,
            "from_name": parseaddr(from_)[0], "from_domain": _domain_of(from_),
            "subject": subject, "date": date, "body": _text_of(msg)}


def _imap_since(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")


def sync(progress=None, imap_factory=None) -> dict:
    """Fetch recent mail read-only, keep messages from tracked colleges."""
    note = progress or (lambda m: None)
    cfg = account_config()
    if not cfg["configured"]:
        raise ConfigError("Email isn't set up. Add your IMAP account on the Email page.")
    colleges = [c for c in planner.load()["colleges"] if c.get("email_domains")]
    if not colleges:
        raise ConfigError("Add email domains to at least one college first.")
    rules = load_rules()
    existing = {m["id"]: m for m in load_emails()}
    seen_before = set(existing)
    note(f"Connecting to {cfg['host']}…")
    factory = imap_factory or (lambda: imaplib.IMAP4_SSL(cfg["host"], cfg["port"],
                                                          ssl_context=ssl.create_default_context(), timeout=30))
    added, scanned, matched = 0, 0, 0
    try:
        conn = factory()
    except Exception as exc:
        raise ConfigError(f"Could not connect to {cfg['host']}: {exc.__class__.__name__}") from None
    try:
        try:
            conn.login(cfg["username"], cfg["password"])
        except imaplib.IMAP4.error:
            raise ConfigError("The mail server rejected the login. For Gmail, use an app password "
                              "(Google Account → Security → App passwords).") from None
        typ, _ = conn.select(_quote_folder(cfg["folder"]), readonly=True)
        if typ != "OK":
            raise ConfigError(f"Folder {cfg['folder']!r} not found.")
        # Ask the server for mail from tracked domains only, in the date window.
        domains = sorted({d for c in colleges for d in c["email_domains"]})
        uids: list[bytes] = []
        for d in domains:
            typ, data = conn.uid("SEARCH", None, "SINCE", _imap_since(cfg["days"]), "FROM", f'"{d}"')
            if typ == "OK" and data and data[0]:
                uids.extend(data[0].split())
        uids = sorted(set(uids), key=lambda u: int(u))[-500:]
        note(f"Reading {len(uids)} message(s)…")
        for i, uid in enumerate(uids, 1):
            typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[]<0.65536>)")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            scanned += 1
            msg = parse_message(data[0][1], uid.decode())
            item = classify(msg, colleges, rules)
            if item is None:
                continue  # not from a tracked college: discarded, never stored
            matched += 1
            if item["id"] in existing:
                prev = existing[item["id"]]
                for keep in ("done", "task_id", "category_locked", "category", "notes"):
                    if keep in prev and (keep != "category" or prev.get("category_locked")):
                        item[keep] = prev[keep]
            else:
                added += 1
            existing[item["id"]] = item
            if i % 25 == 0:
                note(f"Read {i}/{len(uids)}…")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    save_emails(list(existing.values()))

    import history
    names = {c["id"]: c["name"] for c in colleges}
    for m in existing.values():
        if m["id"] not in seen_before and m.get("important") and not m.get("done"):
            history.add_alert("email", f"College email: {m['subject'][:120]}",
                              f"{names.get(m['college_id'], '')} — {', '.join(m['important'][:3])}",
                              f"/email?college={m['college_id']}", key=f"email:{m['id']}")
    note(f"Done: {added} new, {matched} college message(s).")
    return {"scanned": scanned, "matched": matched, "added": added}


def _quote_folder(folder: str) -> str:
    return f'"{folder}"' if " " in folder else folder


def test_connection(cfg: dict, imap_factory=None) -> str:
    factory = imap_factory or (lambda: imaplib.IMAP4_SSL(cfg["host"], cfg["port"],
                                                          ssl_context=ssl.create_default_context(), timeout=20))
    try:
        conn = factory()
        conn.login(cfg["username"], cfg["password"])
        typ, _ = conn.select(_quote_folder(cfg["folder"]), readonly=True)
        conn.logout()
    except imaplib.IMAP4.error:
        raise ConfigError("Login rejected. For Gmail use an app password, and make sure IMAP is enabled.") from None
    except OSError as exc:
        raise ConfigError(f"Could not reach {cfg['host']}:{cfg['port']} ({redact(str(exc), [cfg['password']])}).") from None
    if typ != "OK":
        raise ConfigError(f"Connected, but folder {cfg['folder']!r} was not found.")
    return "Connected and opened the folder read-only."


# ---------------------------------------------------------------------------
# Filter export (Gmail XML + generic search queries)
# ---------------------------------------------------------------------------

def _from_clause(domains: list[str]) -> str:
    return "(" + " OR ".join(domains) + ")"


def _words_clause(words: list[str]) -> str:
    return "(" + " OR ".join(f'"{w}"' if " " in w else w for w in words) + ")"


def label_name(prefix: str, name: str) -> str:
    clean = re.sub(r"[\"<>\\]", "", name).strip()
    return f"{prefix}/{clean}" if prefix else clean


def filter_rules(prefix: str = "College", by_college: bool = True, by_category: bool = True) -> list[dict]:
    colleges = [c for c in planner.load()["colleges"] if c.get("email_domains")]
    all_domains = sorted({d for c in colleges for d in c["email_domains"]})
    rules = load_rules()
    out = []
    if by_college:
        for c in sorted(colleges, key=lambda c: c["name"].lower()):
            out.append({"kind": "college", "name": c["name"], "label": label_name(prefix, c["name"]),
                        "from": _from_clause(c["email_domains"]), "has": "",
                        "query": f"from:{_from_clause(c['email_domains'])}"})
    if by_category and all_domains:
        for cat, words in rules.items():
            if not words:
                continue
            label = label_name(prefix, planner.TASK_CATEGORIES.get(cat, cat))
            out.append({"kind": "category", "name": planner.TASK_CATEGORIES.get(cat, cat), "label": label,
                        "from": _from_clause(all_domains), "has": _words_clause(words),
                        "query": f"from:{_from_clause(all_domains)} {_words_clause(words)}"})
    return out


def gmail_filters_xml(prefix: str = "College", by_college: bool = True, by_category: bool = True,
                      never_spam: bool = True) -> str:
    """Gmail 'Import filters' file. Filters only add labels; nothing is deleted or forwarded."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = ["<?xml version='1.0' encoding='UTF-8'?>",
             "<feed xmlns='http://www.w3.org/2005/Atom' xmlns:apps='http://schemas.google.com/apps/2006'>",
             "<title>College Tracker mail filters</title>", f"<updated>{stamp}</updated>"]
    for i, r in enumerate(filter_rules(prefix, by_college, by_category), 1):
        lines += ["<entry>", "<category term='filter'></category>",
                  f"<title>Mail Filter</title><id>tag:college-tracker,filter:{i}</id>",
                  f"<updated>{stamp}</updated><content></content>",
                  f"<apps:property name='from' value={quoteattr(r['from'])}/>"]
        if r["has"]:
            lines.append(f"<apps:property name='hasTheWord' value={quoteattr(r['has'])}/>")
        lines.append(f"<apps:property name='label' value={quoteattr(r['label'])}/>")
        if never_spam:
            lines.append("<apps:property name='shouldNeverSpam' value='true'/>")
        lines.append("</entry>")
    lines.append("</feed>")
    return "\n".join(lines) + "\n"
