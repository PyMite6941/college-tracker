"""
College tracker checker.

Reads ``sites.json`` (the sites and the read-only checks to run on each),
signs in the way each site is configured to, opens every check page with
Playwright, and records what it finds:

* ``data/results.json``  latest result per check (shown on the dashboard)
* ``data/history/...``   timestamped history with diffs (see history.py)
* ``data/alerts.json``   changes, watched text, sign-in and error alerts

Sign-in modes (``auth`` in sites.json):

    none         public page, no sign-in
    manual       you sign in yourself in a visible browser ("Sign in" on the
                 Sites page). The session is saved to data/auth/<site>.json
                 and reused until the site expires it.
    credentials  the checker fills in a saved username/password. If the site
                 then asks for MFA, a passkey or a CAPTCHA, the check stops
                 with "reauth_required" so you can finish in a visible browser.

The checker never submits anything except a login form, never tries to get
past MFA/CAPTCHA, and only visits pages you configured.

Password-manager integration (unchanged):
- Chrome profile mode: ``CHROME_USER_DATA_DIR`` (+ optional ``CHROME_PROFILE``
  or a per-site ``chrome_profile``) launches Playwright with that profile.
- CSV import: ``import_chrome_passwords_csv()`` reads Chrome's exported CSV.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import history
from security import (
    ConfigError,
    redact,
    request_allowed,
    strip_sensitive_query,
    validate_target_url,
)
from storage import (
    BASE_DIR,
    data_dir,
    is_slug,
    read_json,
    restrict_permissions,
    slugify,
    write_json,
)

SITES_FILE = Path(os.getenv("COLLEGE_TRACKER_SITES_FILE") or (BASE_DIR / "sites.json"))
ENV_FILE = BASE_DIR / ".env"

AUTH_MODES = {
    "none": "No sign-in (public page)",
    "manual": "I sign in myself in a visible browser",
    "credentials": "Fill in my saved username/password",
}

# Selectors tried (in order) when a site does not supply its own.
DEFAULT_USERNAME_SELECTORS = ('input[type="email"], input[name="username"], input[name="email"], '
                              'input[autocomplete="username"], input[name="user"], input[id*="user" i]')
DEFAULT_PASSWORD_SELECTORS = 'input[type="password"]'
DEFAULT_SUBMIT_SELECTORS = 'button[type="submit"], input[type="submit"]'
DEFAULT_CHECK_SELECTORS = ["main", '[role="main"]', ".checklist", ".registration-checklist", "body"]

# Signs that a site wants a human: MFA, passkeys, CAPTCHAs.
CHALLENGE_SELECTORS = [
    'iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]', 'iframe[src*="challenges.cloudflare.com"]',
    ".g-recaptcha", ".h-captcha", ".cf-turnstile",
    'input[autocomplete="one-time-code"]', 'input[name*="otp" i]', 'input[name*="mfa" i]',
    'input[name*="totp" i]', 'input[name*="passcode" i]',
]
CHALLENGE_TEXT = re.compile(
    r"(verification code|two-step|two-factor|2-step|multi-factor|authenticator app|"
    r"use your passkey|security key|approve (the|this) sign[- ]in|i'?m not a robot|verify you are human)",
    re.I,
)

MAX_EXCERPT = int(os.getenv("CHECK_MAX_EXCERPT", "8000"))
PAGE_TIMEOUT_MS = int(os.getenv("CHECK_TIMEOUT_MS", "30000"))
SIGN_IN_TIMEOUT_S = int(os.getenv("SIGN_IN_TIMEOUT_S", "600"))


def results_file() -> Path:
    return data_dir() / "results.json"


RESULTS_FILE = results_file()


def sites_file() -> Path:
    return Path(os.getenv("COLLEGE_TRACKER_SITES_FILE") or SITES_FILE)


# ---------------------------------------------------------------------------
# Site configuration
# ---------------------------------------------------------------------------

def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in re.split(r"[\n,]", value) if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def normalize_site(site: dict) -> dict:
    """Upgrade a legacy single-checklist site to the multi-check format."""
    s = dict(site)
    checks = s.get("checks")
    if not checks and s.get("checklist_url"):
        checks = [{
            "id": "checklist",
            "name": "Registration checklist",
            "url": s.get("checklist_url"),
            "selectors": s.get("checklist_selectors") or DEFAULT_CHECK_SELECTORS,
        }]
    s["checks"] = [dict(c) for c in (checks or [])]
    for c in s["checks"]:
        c.setdefault("selectors", list(DEFAULT_CHECK_SELECTORS))
        c["selectors"] = _as_list(c.get("selectors")) or list(DEFAULT_CHECK_SELECTORS)
        c["watch"] = _as_list(c.get("watch"))
    if not isinstance(s.get("selectors"), dict):
        s["selectors"] = {}
    if "auth" not in s:
        s["auth"] = "credentials" if (s.get("user_env") or s.get("pass_env") or s.get("login_url")) else "none"
    s.pop("checklist_url", None)
    s.pop("checklist_selectors", None)
    return s


def load_sites() -> list[dict]:
    raw = read_json(sites_file(), [])
    if not isinstance(raw, list):
        return []
    return [normalize_site(s) for s in raw if isinstance(s, dict)]


def save_sites(sites: list[dict]) -> None:
    """Persist the sites list to sites.json. Never writes secrets."""
    clean = []
    for s in sites:
        s = {k: v for k, v in normalize_site(s).items()
             if k not in {"creds_saved", "session", "errors", "password", "username"}}
        clean.append(s)
    write_json(sites_file(), clean)


def get_site(site_id: str) -> dict | None:
    return next((s for s in load_sites() if s.get("id") == site_id), None)


def resolve(value: str | None) -> str | None:
    """Replace ${VAR} tokens with values from the environment (.env)."""
    if value is None:
        return None
    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", lambda m: os.getenv(m.group(1), ""), value)


def validate_site(site: dict, resolve_dns: bool = False) -> list[str]:
    """Return a list of human-readable problems (empty if the site is valid)."""
    errors = []
    if not is_slug(site.get("id", "")):
        errors.append("Site id must be lowercase letters, digits, '-' or '_'.")
    if not (site.get("name") or "").strip():
        errors.append("Site name is required.")
    auth = site.get("auth", "none")
    if auth not in AUTH_MODES:
        errors.append(f"Unknown sign-in mode {auth!r}; use one of {', '.join(AUTH_MODES)}.")
    if auth in {"manual", "credentials"}:
        try:
            validate_target_url(resolve(site.get("login_url")), "Login URL", resolve_dns=resolve_dns)
        except ConfigError as exc:
            errors.append(str(exc))
    elif site.get("login_url"):
        try:
            validate_target_url(resolve(site.get("login_url")), "Login URL", resolve_dns=resolve_dns)
        except ConfigError as exc:
            errors.append(str(exc))
    if not site.get("checks"):
        errors.append("Add at least one check (a page to read).")
    seen = set()
    for c in site.get("checks", []):
        label = c.get("name") or c.get("id") or "check"
        if not is_slug(c.get("id", "")):
            errors.append(f"Check {label!r}: id must be lowercase letters, digits, '-' or '_'.")
        if c.get("id") in seen:
            errors.append(f"Check id {c.get('id')!r} is used twice.")
        seen.add(c.get("id"))
        try:
            validate_target_url(resolve(c.get("url")), f"Check {label!r} URL", resolve_dns=resolve_dns)
        except ConfigError as exc:
            errors.append(str(exc))
        for sel in c.get("selectors", []):
            if len(sel) > 300:
                errors.append(f"Check {label!r}: selector too long.")
    return errors


def site_from_form(form, existing: dict | None = None) -> dict:
    """Build a site dict from the add/edit form. Raises ConfigError."""
    site = dict(existing or {})
    sid = (form.get("id") or site.get("id") or slugify(form.get("name") or "")).strip().lower()
    site.update({
        "id": sid,
        "name": (form.get("name") or "").strip()[:200],
        "auth": (form.get("auth") or "credentials").strip(),
        "login_url": (form.get("login_url") or "").strip(),
        "college_id": (form.get("college_id") or "").strip()[:64],
    })
    for key in ("user_env", "pass_env", "chrome_profile"):
        v = (form.get(key) or "").strip()
        if v:
            if key != "chrome_profile" and not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", v):
                raise ConfigError(f"{key} must be an UPPER_CASE environment variable name.")
            site[key] = v
        else:
            site.pop(key, None)
    sel = site.get("selectors") or {}
    for key in ("username", "password", "submit"):
        v = (form.get(f"sel_{key}") or "").strip()
        if v:
            sel[key] = v[:300]
        else:
            sel.pop(key, None)
    site["selectors"] = sel
    try:
        site["session_max_age_days"] = max(0, int(form.get("session_max_age_days") or 0)) or None
    except ValueError:
        raise ConfigError("Session max age must be a whole number of days.") from None
    if not site["session_max_age_days"]:
        site.pop("session_max_age_days", None)
    if site.get("auth") == "none" and not site.get("login_url"):
        site.pop("login_url", None)

    # A first check can be supplied with the add form.
    check_url = (form.get("check_url") or form.get("checklist_url") or "").strip()
    if not existing and check_url:
        site["checks"] = [{
            "id": slugify(form.get("check_name") or "checklist")[:40],
            "name": (form.get("check_name") or "Registration checklist").strip()[:200],
            "url": check_url,
            "selectors": _as_list(form.get("check_selectors")) or list(DEFAULT_CHECK_SELECTORS),
            "watch": _as_list(form.get("check_watch")),
        }]
    site = normalize_site(site)
    errors = validate_site(site, resolve_dns=False)
    if errors:
        raise ConfigError(" ".join(errors))
    return site


def check_from_form(form, site: dict, existing: dict | None = None) -> dict:
    name = (form.get("name") or "").strip()[:200]
    if not name:
        raise ConfigError("Check name is required.")
    cid = (existing or {}).get("id") or slugify(form.get("id") or name)[:40]
    if not existing and any(c["id"] == cid for c in site.get("checks", [])):
        raise ConfigError(f"Check id {cid!r} already exists on this site.")
    url = validate_target_url(form.get("url"), "Check URL")
    check = {
        "id": cid,
        "name": name,
        "url": url,
        "selectors": _as_list(form.get("selectors")) or list(DEFAULT_CHECK_SELECTORS),
        "watch": _as_list(form.get("watch")),
    }
    wait_for = (form.get("wait_for") or "").strip()
    if wait_for:
        check["wait_for"] = wait_for[:300]
    return check


# ---------------------------------------------------------------------------
# Credentials (credentials.toml, gitignored) and .env fallback
# ---------------------------------------------------------------------------

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None


def credentials_file() -> Path:
    return Path(os.getenv("COLLEGE_TRACKER_CREDENTIALS_FILE") or (BASE_DIR / "credentials.toml"))


CREDENTIALS_FILE = credentials_file()


def _encode_toml_string(value: str) -> str:
    """Escape a string for use as a TOML basic string."""
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{escaped}"'


def load_credentials() -> dict[str, dict]:
    """Load saved credentials keyed by site id: {username, password, profile}."""
    path = credentials_file()
    if not path.exists() or tomllib is None:
        return {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    creds = {k: v for k, v in (data.get("sites") or {}).items() if isinstance(v, dict)}
    # Files written by older versions put each site in a top-level table.
    for k, v in data.items():
        if k not in ("sites", "imap") and isinstance(v, dict) and "password" in v:
            creds.setdefault(k, v)
    return creds


def load_imap_settings() -> dict:
    """The [imap] table of credentials.toml (email sync account), or {}."""
    path = credentials_file()
    if not path.exists() or tomllib is None:
        return {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    imap = data.get("imap")
    return dict(imap) if isinstance(imap, dict) else {}


def save_imap_settings(settings: dict | None) -> None:
    """Replace (or with None, remove) the [imap] table, keeping site credentials."""
    _write_credentials(load_credentials(), imap=settings or {})


def _write_credentials(creds: dict, imap: dict | None = None) -> None:
    if imap is None:
        imap = load_imap_settings()
    lines = ["# College Tracker saved credentials. Plain text, local only, gitignored."]
    if imap:
        lines.append("[imap]")
        for key in ("host", "port", "username", "password", "folder", "days"):
            if key in imap and imap[key] not in (None, ""):
                val = imap[key]
                lines.append(f"  {key} = {val}" if isinstance(val, int) else f"  {key} = {_encode_toml_string(str(val))}")
        lines.append("")
    lines.append("[sites]")
    for sid, entry in sorted(creds.items()):
        lines.append(f"  [sites.{_encode_toml_string(sid)}]")
        lines.append(f"    username = {_encode_toml_string(entry.get('username', ''))}")
        lines.append(f"    password = {_encode_toml_string(entry.get('password', ''))}")
        if entry.get("profile"):
            lines.append(f"    profile = {_encode_toml_string(entry['profile'])}")
        lines.append("")
    path = credentials_file()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    restrict_permissions(path)


def save_credentials(site_id: str, username: str, password: str, profile: str | None = None) -> None:
    creds = load_credentials()
    entry: dict = {"username": username, "password": password}
    if profile:
        entry["profile"] = profile
    creds[site_id] = entry
    _write_credentials(creds)


def delete_credentials(site_id: str) -> bool:
    creds = load_credentials()
    if site_id not in creds:
        return False
    del creds[site_id]
    _write_credentials(creds)
    return True


def get_credentials(site: dict) -> tuple[str, str]:
    """(username, password): credentials.toml first, then .env variables."""
    creds = load_credentials()
    site_id = site.get("id", "")
    if site_id and site_id in creds:
        entry = creds[site_id]
        return entry.get("username", ""), entry.get("password", "")
    user = os.getenv(site.get("user_env", "") or "", "") if site.get("user_env") else ""
    password = os.getenv(site.get("pass_env", "") or "", "") if site.get("pass_env") else ""
    return user, password


def has_credentials(site: dict) -> bool:
    u, p = get_credentials(site)
    return bool(u and p)


# ---------------------------------------------------------------------------
# Saved sessions (Playwright storage state, per site, gitignored)
# ---------------------------------------------------------------------------

def auth_state_path(site_id: str) -> Path:
    if not is_slug(site_id):
        raise ConfigError("Invalid site id.")
    return data_dir() / "auth" / f"{site_id}.json"


def session_max_age_days(site: dict) -> int:
    try:
        default = int(os.getenv("SESSION_MAX_AGE_DAYS", "14"))
    except ValueError:
        default = 14
    return int(site.get("session_max_age_days") or default)


def session_info(site: dict) -> dict:
    """Describe the saved session for a site (never its contents)."""
    path = auth_state_path(site["id"])
    if not path.exists():
        return {"saved": False, "saved_at": None, "age_days": None, "expired": False}
    saved_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    age = (datetime.now(timezone.utc) - saved_at).total_seconds() / 86400
    max_age = session_max_age_days(site)
    return {"saved": True, "saved_at": saved_at.isoformat(timespec="minutes"),
            "age_days": round(age, 1), "expired": bool(max_age) and age > max_age}


def forget_session(site_id: str) -> bool:
    path = auth_state_path(site_id)
    if path.exists():
        path.unlink()
        return True
    return False


def _save_state(context, site_id: str) -> None:
    path = auth_state_path(site_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = context.storage_state()
    write_json(path, state, private=True)


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------

class ReauthRequired(Exception):
    pass


class LoginFailed(Exception):
    pass


class BlockedRequest(Exception):
    pass


def first_visible(page, selectors: str):
    """Return the first locator from a comma list that is visible on the page."""
    for part in selectors.split(","):
        part = part.strip()
        if not part:
            continue
        locator = page.locator(part)
        try:
            if locator.count() > 0 and locator.first.is_visible():
                return locator.first
        except Exception:
            continue
    return None


def _same_page(a: str | None, b: str | None) -> bool:
    from urllib.parse import urlsplit
    if not a or not b:
        return False
    pa, pb = urlsplit(a), urlsplit(b)
    return pa.hostname == pb.hostname and pa.path.rstrip("/") == pb.path.rstrip("/")


def looks_like_login(page, site: dict) -> bool:
    """Heuristic: are we looking at a sign-in page instead of the content?"""
    if first_visible(page, DEFAULT_PASSWORD_SELECTORS) is not None:
        return True
    if site.get("login_url") and _same_page(page.url, resolve(site.get("login_url"))) \
            and not any(_same_page(page.url, resolve(c["url"])) for c in site.get("checks", [])):
        return True
    for marker in _as_list(site.get("login_markers")):
        try:
            if marker.lower() in page.locator("body").inner_text(timeout=2000).lower():
                return True
        except Exception:
            pass
    return False


def challenge_detected(page) -> bool:
    for sel in CHALLENGE_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    try:
        return bool(CHALLENGE_TEXT.search(page.locator("body").inner_text(timeout=2000)))
    except Exception:
        return False


def _install_guard(context, blocked: list[str]) -> None:
    """Abort any request (including redirects) to a disallowed host."""
    def handler(route):
        url = route.request.url
        if request_allowed(url):
            route.continue_()
        else:
            blocked.append(strip_sensitive_query(url))
            route.abort("blockedbyclient")
    context.route("**/*", handler)


def _classify(exc: Exception, blocked: list[str]) -> tuple[str, str]:
    msg = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    if isinstance(exc, ReauthRequired):
        return "reauth_required", str(exc)
    if isinstance(exc, LoginFailed):
        return "login_failed", str(exc)
    if isinstance(exc, ConfigError):
        return "config_error", str(exc)
    if blocked:
        return "config_error", ("Blocked a request to a local/private address: "
                                f"{blocked[0]}. Set ALLOW_LOCAL_TARGETS=1 only if intended.")
    name = exc.__class__.__name__
    if name == "TimeoutError" or "Timeout" in msg:
        return "network_error", f"Timed out loading the page ({msg[:200]})."
    if "net::" in msg or "NS_ERROR" in msg or "ECONN" in msg:
        return "network_error", msg[:300]
    return "error", msg[:300]


def _blank_result(site: dict, check: dict) -> dict:
    return {
        "site_id": site.get("id"),
        "site_name": site.get("name"),
        "check_id": check.get("id"),
        "check_name": check.get("name"),
        "url": strip_sensitive_query(resolve(check.get("url")) or ""),
        "status": "pending",
        "checked_at": None,
        "title": None,
        "excerpt": None,
        "final_url": None,
        "error": None,
    }


def _do_login(page, site: dict, user: str, password: str) -> None:
    """Fill and submit a login form. Raises ReauthRequired / LoginFailed."""
    login_url = resolve(site.get("login_url"))
    page.goto(login_url, wait_until="domcontentloaded")
    selectors = site.get("selectors") or {}
    user_box = first_visible(page, selectors.get("username") or DEFAULT_USERNAME_SELECTORS)
    pass_box = first_visible(page, selectors.get("password") or DEFAULT_PASSWORD_SELECTORS)
    if challenge_detected(page):
        raise ReauthRequired("The sign-in page shows a CAPTCHA or verification step. "
                             "Use “Sign in” on the Sites page to finish it yourself.")
    if pass_box is None:
        if user_box is not None:
            # Two-step login (username first). Fill, continue, then look again.
            user_box.fill(user)
            nxt = first_visible(page, selectors.get("submit") or DEFAULT_SUBMIT_SELECTORS)
            (nxt.click() if nxt else page.keyboard.press("Enter"))
            page.wait_for_load_state("domcontentloaded")
            try:
                page.locator(selectors.get("password") or DEFAULT_PASSWORD_SELECTORS).first.wait_for(
                    state="visible", timeout=10000)
            except Exception:
                pass
            pass_box = first_visible(page, selectors.get("password") or DEFAULT_PASSWORD_SELECTORS)
            user_box = None
        if pass_box is None:
            if challenge_detected(page):
                raise ReauthRequired("The site asked for a passkey, code or other verification. "
                                     "Use “Sign in” on the Sites page to finish it yourself.")
            raise LoginFailed("Could not find the username/password fields on the login page. "
                              "Set custom selectors or switch this site to manual sign-in.")
    if user_box is not None:
        user_box.fill(user)
    pass_box.fill(password)
    submit = first_visible(page, selectors.get("submit") or DEFAULT_SUBMIT_SELECTORS)
    (submit.click() if submit else page.keyboard.press("Enter"))
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        page.wait_for_load_state("domcontentloaded")
    if challenge_detected(page):
        raise ReauthRequired("The site asked for MFA, a passkey or a CAPTCHA after the password. "
                             "Use “Sign in” on the Sites page to finish it yourself.")
    if first_visible(page, DEFAULT_PASSWORD_SELECTORS) is not None:
        raise LoginFailed("Still on the sign-in page after submitting; the saved username or "
                          "password may be wrong.")


def _read_check(page, check: dict, result: dict) -> None:
    url = resolve(check.get("url"))
    page.goto(url, wait_until="domcontentloaded")
    if check.get("wait_for"):
        page.locator(check["wait_for"]).first.wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)
    else:
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
    result["title"] = page.title()
    result["final_url"] = strip_sensitive_query(page.url)
    excerpt = None
    for sel in check.get("selectors") or DEFAULT_CHECK_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                excerpt = loc.first.inner_text()
                break
        except Exception:
            continue
    if excerpt is None:
        excerpt = page.locator("body").inner_text()
    result["excerpt"] = excerpt[:MAX_EXCERPT]


def check_site(context, site: dict, check_ids: set[str] | None = None) -> list[dict]:
    """Run every (selected) check of one site inside an existing context."""
    blocked: list[str] = []
    _install_guard(context, blocked)
    user, password = get_credentials(site)
    hide = [password, user] if password else []
    auth = site.get("auth", "none")
    checks = [c for c in site.get("checks", []) if not check_ids or c["id"] in check_ids]
    results = []
    page = context.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)
    logged_in = False
    session_problem: tuple[str, str] | None = None
    try:
        for check in checks:
            result = _blank_result(site, check)
            try:
                if session_problem:
                    result["status"], result["error"] = session_problem
                    continue
                errors = validate_site({**site, "checks": [check]}, resolve_dns=False)
                if errors:
                    raise ConfigError(" ".join(errors))
                _read_check(page, check, result)
                if auth != "none" and looks_like_login(page, site):
                    if auth == "credentials" and not logged_in:
                        if not (user and password):
                            raise ReauthRequired("No saved username/password for this site. Save them "
                                                 "on the Sites page or use “Sign in”.")
                        _do_login(page, site, user, password)
                        logged_in = True
                        _read_check(page, check, result)
                    if looks_like_login(page, site):
                        if challenge_detected(page) or auth == "manual":
                            raise ReauthRequired("Your session has expired or you are not signed in. "
                                                 "Use “Sign in” on the Sites page.")
                        raise LoginFailed("Reached the sign-in page instead of the check page.")
                result["status"] = "ok"
            except (ReauthRequired, LoginFailed) as exc:
                result["status"], result["error"] = _classify(exc, blocked)
                session_problem = (result["status"], result["error"])
            except Exception as exc:  # one check failing must not stop the rest
                result["status"], result["error"] = _classify(exc, blocked)
            finally:
                result["checked_at"] = datetime.now(timezone.utc).isoformat()
                result["error"] = redact(result["error"], hide)
                if result["excerpt"]:
                    result["excerpt"] = redact(result["excerpt"], hide)
                results.append(result)
        if logged_in or (auth != "none" and any(r["status"] == "ok" for r in results)):
            try:
                _save_state(context, site["id"])
            except Exception:
                pass
    finally:
        try:
            page.close()
        except Exception:
            pass
    return results


# ---------------------------------------------------------------------------
# Running checks
# ---------------------------------------------------------------------------

def _chrome_profile_dir(site: dict) -> str | None:
    root = os.getenv("CHROME_USER_DATA_DIR", "").strip()
    if not root:
        return None
    profile = site.get("chrome_profile") or os.getenv("CHROME_PROFILE", "").strip()
    return str(Path(root) / profile) if profile else root


def load_results() -> list[dict]:
    raw = read_json(results_file(), [])
    return [r for r in raw if isinstance(r, dict) and r.get("site_id") and r.get("check_id")]


def _merge_results(new: list[dict]) -> list[dict]:
    """Keep the latest result per check for checks that still exist."""
    sites = {s["id"]: s for s in load_sites()}
    latest = {(r["site_id"], r["check_id"]): r for r in load_results()}
    for r in new:
        latest[(r["site_id"], r["check_id"])] = r
    keep = []
    for (sid, cid), r in latest.items():
        site = sites.get(sid)
        if site and any(c["id"] == cid for c in site["checks"]):
            keep.append(r)
    order = {(s["id"], c["id"]): i for i, s in enumerate(sites.values()) for c in s["checks"]}
    keep.sort(key=lambda r: order.get((r["site_id"], r["check_id"]), 1 << 30))
    write_json(results_file(), keep)
    return keep


def run_checks(headless: bool = True, site_ids: list[str] | None = None,
               progress=None) -> list[dict]:
    """Run checks for all (or the given) sites; record history and alerts."""
    from playwright.sync_api import sync_playwright

    load_dotenv(ENV_FILE)
    sites = [s for s in load_sites() if not site_ids or s["id"] in site_ids]
    all_results: list[dict] = []

    def note(msg: str):
        if progress:
            progress(msg)

    with sync_playwright() as p:
        browser = None
        try:
            for site in sites:
                note(f"Checking {site['name']}…")
                errors = validate_site(site, resolve_dns=False)
                if errors:
                    for c in site["checks"] or [{"id": "config", "name": "Configuration"}]:
                        r = _blank_result(site, c)
                        r.update(status="config_error", error=" ".join(errors),
                                 checked_at=datetime.now(timezone.utc).isoformat())
                        all_results.append(r)
                    continue
                if site.get("auth") == "manual" and not session_info(site)["saved"] \
                        and not _chrome_profile_dir(site):
                    for c in site["checks"]:
                        r = _blank_result(site, c)
                        r.update(status="reauth_required", checked_at=datetime.now(timezone.utc).isoformat(),
                                 error="Not signed in yet. Use “Sign in” on the Sites page.")
                        all_results.append(r)
                    continue

                profile_dir = _chrome_profile_dir(site)
                if profile_dir:
                    if not Path(profile_dir).exists():
                        for c in site["checks"]:
                            r = _blank_result(site, c)
                            r.update(status="config_error", checked_at=datetime.now(timezone.utc).isoformat(),
                                     error=f"Chrome profile directory not found: {profile_dir}")
                            all_results.append(r)
                        continue
                    ctx = p.chromium.launch_persistent_context(
                        user_data_dir=profile_dir, headless=headless, accept_downloads=False)
                else:
                    if browser is None:
                        browser = p.chromium.launch(headless=headless)
                    state = auth_state_path(site["id"])
                    ctx = browser.new_context(
                        accept_downloads=False,
                        storage_state=str(state) if state.exists() and site.get("auth") != "none" else None,
                    )
                try:
                    all_results.extend(check_site(ctx, site))
                finally:
                    ctx.close()
        finally:
            if browser is not None:
                browser.close()

    # History, change detection and alerts.
    by_site = {s["id"]: s for s in sites}
    for r in all_results:
        site = by_site.get(r["site_id"], {})
        check = next((c for c in site.get("checks", []) if c["id"] == r["check_id"]), {})
        history.record_run(r, watch=check.get("watch"))
        history.alerts_for_result(r, r.get("check_name") or r["check_id"], r.get("site_name") or r["site_id"])
    _merge_results(all_results)

    try:
        import planner
        planner.send_due_reminders()
    except Exception as exc:
        print(f"[reminders] {redact(str(exc))}")
    note("Done.")
    return all_results


def run_scrape(headless: bool = True) -> list[dict]:
    """Backwards-compatible alias for run_checks()."""
    return run_checks(headless=headless)


# ---------------------------------------------------------------------------
# Human-in-the-loop sign-in
# ---------------------------------------------------------------------------

def interactive_sign_in(site_id: str, stop: threading.Event | None = None,
                        progress=None, timeout_s: int | None = None, headless: bool = False) -> dict:
    """Open a visible browser at the site's login page and let the user sign in.

    The user completes MFA, passkeys or CAPTCHAs themselves. The session is
    saved when they click "I've signed in" in the dashboard (``stop`` set)
    or close the browser window. Nothing is submitted on their behalf.
    """
    from playwright.sync_api import sync_playwright

    load_dotenv(ENV_FILE)
    site = get_site(site_id)
    if not site:
        raise ConfigError(f"Site {site_id!r} not found.")
    start_url = resolve(site.get("login_url")) or resolve((site.get("checks") or [{}])[0].get("url"))
    validate_target_url(start_url, "Login URL")
    stop = stop or threading.Event()
    timeout_s = timeout_s or SIGN_IN_TIMEOUT_S
    note = progress or (lambda m: None)
    profile_dir = _chrome_profile_dir(site)

    with sync_playwright() as p:
        browser = None
        if profile_dir:
            ctx = p.chromium.launch_persistent_context(user_data_dir=profile_dir, headless=headless)
        else:
            browser = p.chromium.launch(headless=headless)
            state = auth_state_path(site_id)
            ctx = browser.new_context(storage_state=str(state) if state.exists() else None)
        blocked: list[str] = []
        _install_guard(ctx, blocked)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(start_url, wait_until="domcontentloaded")
        user, _ = get_credentials(site)
        if user:  # convenience only: prefill the username, never submit
            box = first_visible(page, (site.get("selectors") or {}).get("username") or DEFAULT_USERNAME_SELECTORS)
            if box is not None:
                try:
                    box.fill(user)
                except Exception:
                    pass
        note("Browser open — finish signing in, then click “I've signed in”.")
        last_state = None
        deadline = _time.monotonic() + timeout_s
        reason = "timeout"
        while _time.monotonic() < deadline:
            if stop.is_set():
                reason = "done"
                break
            try:
                if not ctx.pages:
                    reason = "closed"
                    break
                last_state = ctx.storage_state()
                ctx.pages[0].wait_for_timeout(1000)
            except Exception:
                reason = "closed"
                break
        try:
            last_state = ctx.storage_state()
        except Exception:
            pass
        saved = False
        if last_state is not None and not profile_dir:
            write_json(auth_state_path(site_id), last_state, private=True)
            saved = True
        try:
            ctx.close()
        except Exception:
            pass
        if browser is not None:
            browser.close()
    history.dismiss_key(f"reauth:{site_id}")
    return {"saved": saved or bool(profile_dir), "reason": reason}


# ---------------------------------------------------------------------------
# Chrome password CSV import and profile discovery
# ---------------------------------------------------------------------------

def import_chrome_passwords_csv(csv_path: str | os.PathLike, with_secrets: bool = False) -> list[dict]:
    """Read Chrome's exported passwords CSV and return site entries.

    Columns: name, url, username_value, password_value, ...
    With ``with_secrets`` each entry carries ``username``/``password`` keys so
    the caller can store them in credentials.toml; save_sites() strips them.
    Only http(s) login URLs are imported.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    sites = []
    seen = set()
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            url = (row.get("url") or "").strip()
            username = (row.get("username_value") or "").strip()
            if not url or not username or not re.match(r"^https?://", url, re.I):
                continue
            host = re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].split(":")[0]
            site_id = re.sub(r"[^a-z0-9-]", "-", host.lower()).strip("-")[:64]
            if not site_id or site_id in seen:
                continue
            seen.add(site_id)
            env_base = site_id.upper().replace("-", "_")
            entry = {
                "id": site_id,
                "name": (row.get("name") or host)[:200],
                "auth": "credentials",
                "login_url": url,
                "user_env": f"{env_base}_USER",
                "pass_env": f"{env_base}_PASS",
                "selectors": {},
                "checks": [],
            }
            if with_secrets:
                entry["username"] = username
                entry["password"] = row.get("password_value") or ""
            sites.append(entry)
    return sites


def _default_chrome_user_data_dir() -> str | None:
    home = Path.home()
    if sys.platform == "win32":
        candidates = [Path(os.getenv("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data",
                      home / "AppData" / "Local" / "Google" / "Chrome" / "User Data"]
    elif sys.platform == "darwin":
        candidates = [home / "Library" / "Application Support" / "Google" / "Chrome"]
    else:
        candidates = [home / ".config" / "google-chrome", home / ".config" / "chromium"]
    for c in candidates:
        if c.exists() and (c / "Local State").exists():
            return str(c)
    return None


def detect_chrome_profiles(user_data_dir: str | None = None) -> list[dict]:
    """Discover Chrome profiles from the Local State file (Default first)."""
    if user_data_dir is None:
        user_data_dir = _default_chrome_user_data_dir()
    if not user_data_dir:
        return []
    local_state = Path(user_data_dir) / "Local State"
    if not local_state.exists():
        return []
    try:
        with open(local_state, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    info_cache = state.get("profile", {}).get("info_cache", {})
    last_used = state.get("profile", {}).get("last_used_profiles", [])
    profiles = []
    for dir_name, info in info_cache.items():
        if not (Path(user_data_dir) / dir_name).exists():
            continue
        profiles.append({
            "dir_name": dir_name,
            "name": info.get("name", dir_name),
            "is_default": dir_name == "Default",
            "user_name": info.get("user_name", ""),
            "last_used_index": last_used.index(dir_name) if dir_name in last_used else 9999,
        })
    profiles.sort(key=lambda p: (0 if p["is_default"] else 1, p["last_used_index"], p["dir_name"]))
    return profiles


def is_chrome_running(user_data_dir: str | None = None) -> bool:
    """Best-effort check: True if a lockfile exists in the user data dir."""
    if not user_data_dir:
        user_data_dir = _default_chrome_user_data_dir()
    if not user_data_dir:
        return False
    return (Path(user_data_dir) / "lockfile").exists()


if __name__ == "__main__":
    load_dotenv(ENV_FILE)
    headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
    for r in run_checks(headless=headless, progress=print):
        print(f"{r['site_name']} / {r['check_name']}: {r['status']}" + (f" — {r['error']}" if r["error"] else ""))
