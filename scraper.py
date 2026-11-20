"""
College tracker scraper.

Reads sites.json (the list of websites to log into and check) and
.env (stored credentials), then uses Playwright to log into each site,
navigate to its registration-checklist page, and save whatever it finds
to data/results.json so the Flask site can display it.

Password-manager integration:
- Chrome profile mode: launch Playwright with the user's existing Chrome
  profile (user_data_dir) so Chrome's saved passwords/autofill are
  available. Set CHROME_USER_DATA_DIR in .env and chrome_profile in
  sites.json for a site.
- CSV import: import_chrome_passwords_csv() reads Chrome's exported
  passwords CSV (name,url,username_value,password_value,...) and maps
  it to sites.json credentials.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
SITES_FILE = BASE_DIR / "sites.json"
ENV_FILE = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
RESULTS_FILE = DATA_DIR / "results.json"

# Selectors tried (in order) when a site does not supply its own.
DEFAULT_USERNAME_SELECTORS = 'input[type="email"], input[name="username"], input[name="email"]'
DEFAULT_PASSWORD_SELECTORS = 'input[type="password"]'
DEFAULT_SUBMIT_SELECTORS = 'button[type="submit"], input[type="submit"]'


def load_sites() -> list[dict]:
    if not SITES_FILE.exists():
        return []
    with open(SITES_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve(value: str | None) -> str | None:
    """Replace ${VAR} tokens with values from the environment (.env)."""
    if value is None:
        return None
    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", lambda m: os.getenv(m.group(1), ""), value)


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


def scrape_site(browser, site: dict) -> dict:
    result = {
        "id": site.get("id"),
        "name": site.get("name"),
        "checklist_url": site.get("checklist_url"),
        "status": "pending",
        "checked_at": None,
        "title": None,
        "excerpt": None,
        "error": None,
    }

    login_url = resolve(site.get("login_url"))
    checklist_url = resolve(site.get("checklist_url"))
    user, password = get_credentials(site)
    selectors = site.get("selectors", {}) or {}

    context = None
    try:
        context = browser.new_context(accept_downloads=False)
        page = context.new_page()
        page.set_default_timeout(30000)

        # ---- login ---------------------------------------------------------
        if login_url and user and password:
            page.goto(login_url, wait_until="domcontentloaded")

            user_sel = selectors.get("username") or DEFAULT_USERNAME_SELECTORS
            pass_sel = selectors.get("password") or DEFAULT_PASSWORD_SELECTORS
            submit_sel = selectors.get("submit") or DEFAULT_SUBMIT_SELECTORS

            username_box = first_visible(page, user_sel)
            password_box = first_visible(page, pass_sel)
            submit = first_visible(page, submit_sel)

            if username_box is None or password_box is None:
                raise RuntimeError("could not find username/password fields on login page")

            username_box.fill(user)
            password_box.fill(password)
            if submit is not None:
                submit.click()
            else:
                page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle")
        elif login_url:
            page.goto(login_url, wait_until="domcontentloaded")

        # ---- registration checklist ----------------------------------------
        if checklist_url:
            page.goto(checklist_url, wait_until="domcontentloaded")

        result["title"] = page.title()
        result["status"] = "ok"

        # Grab the main checklist area (fall back to whole body).
        for sel in (site.get("checklist_selectors") or []):
            locator = page.locator(sel)
            if locator.count() > 0 and locator.first.is_visible():
                result["excerpt"] = locator.first.inner_text()[:4000]
                break
        if not result["excerpt"]:
            result["excerpt"] = page.locator("body").inner_text()[:4000]

    except Exception as exc:  # one site failing must not kill the whole run
        result["status"] = "error"
        result["error"] = str(exc)[:500]
    finally:
        if context is not None:
            context.close()
        result["checked_at"] = datetime.now(timezone.utc).isoformat()

    return result


def _scrape_in_context(context, site: dict) -> dict:
    """Run a single site scrape inside an existing persistent context."""
    result = {
        "id": site.get("id"),
        "name": site.get("name"),
        "checklist_url": site.get("checklist_url"),
        "status": "pending",
        "checked_at": None,
        "title": None,
        "excerpt": None,
        "error": None,
    }

    login_url = resolve(site.get("login_url"))
    checklist_url = resolve(site.get("checklist_url"))
    user, password = get_credentials(site)
    selectors = site.get("selectors", {}) or {}

    page = None
    try:
        page = context.new_page()
        page.set_default_timeout(30000)

        if login_url and user and password:
            page.goto(login_url, wait_until="domcontentloaded")
            user_sel = selectors.get("username") or DEFAULT_USERNAME_SELECTORS
            pass_sel = selectors.get("password") or DEFAULT_PASSWORD_SELECTORS
            submit_sel = selectors.get("submit") or DEFAULT_SUBMIT_SELECTORS

            username_box = first_visible(page, user_sel)
            password_box = first_visible(page, pass_sel)
            submit = first_visible(page, submit_sel)

            if username_box is None or password_box is None:
                raise RuntimeError("could not find username/password fields on login page")

            username_box.fill(user)
            password_box.fill(password)
            if submit is not None:
                submit.click()
            else:
                page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle")
        elif login_url:
            page.goto(login_url, wait_until="domcontentloaded")

        if checklist_url:
            page.goto(checklist_url, wait_until="domcontentloaded")

        result["title"] = page.title()
        result["status"] = "ok"

        for sel in (site.get("checklist_selectors") or []):
            locator = page.locator(sel)
            if locator.count() > 0 and locator.first.is_visible():
                result["excerpt"] = locator.first.inner_text()[:4000]
                break
        if not result["excerpt"]:
            result["excerpt"] = page.locator("body").inner_text()[:4000]

    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)[:500]
    finally:
        if page is not None:
            page.close()
        result["checked_at"] = datetime.now(timezone.utc).isoformat()

    return result


def run_scrape(headless: bool = True) -> list[dict]:
    load_dotenv(ENV_FILE)
    sites = load_sites()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    chrome_user_data_dir = os.getenv("CHROME_USER_DATA_DIR")
    global_profile = os.getenv("CHROME_PROFILE", "").strip() or None
    use_persistent = bool(chrome_user_data_dir)

    def effective_profile_dir(site: dict) -> str | None:
        """Return the absolute path to the Chrome profile directory for a site."""
        if not chrome_user_data_dir:
            return None
        # Per-site chrome_profile overrides the global CHROME_PROFILE.
        profile = site.get("chrome_profile") or global_profile
        if not profile:
            return chrome_user_data_dir  # use the User Data dir itself (Default profile)
        return str(Path(chrome_user_data_dir) / profile)

    results = []
    with sync_playwright() as p:
        if use_persistent:
            # Group sites by their effective profile directory so we can
            # launch one persistent context per profile (Chrome profiles are
            # isolated — you can't switch profiles inside one context).
            groups: dict[str | None, list[dict]] = {}
            for site in sites:
                d = effective_profile_dir(site)
                groups.setdefault(d, []).append(site)

            for profile_dir, group_sites in groups.items():
                if profile_dir is None:
                    # No Chrome profile configured: use a regular browser.
                    browser = p.chromium.launch(headless=headless)
                    try:
                        for site in group_sites:
                            results.append(scrape_site(browser, site))
                    finally:
                        browser.close()
                    continue

                # Validate the profile directory exists before launching.
                if not Path(profile_dir).exists():
                    for site in group_sites:
                        results.append({
                            "id": site.get("id"),
                            "name": site.get("name"),
                            "checklist_url": site.get("checklist_url"),
                            "status": "error",
                            "checked_at": None,
                            "title": None,
                            "excerpt": None,
                            "error": f"Chrome profile directory not found: {profile_dir}",
                        })
                    continue

                browser_context = p.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=headless,
                    accept_downloads=False,
                )
                try:
                    for site in group_sites:
                        results.append(_scrape_in_context(browser_context, site))
                finally:
                    browser_context.close()
        else:
            browser = p.chromium.launch(headless=headless)
            try:
                for site in sites:
                    results.append(scrape_site(browser, site))
            finally:
                browser.close()

    with open(RESULTS_FILE, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)

    return results


def save_sites(sites: list[dict]) -> None:
    """Persist the sites list to sites.json (used by the web UI)."""
    with open(SITES_FILE, "w", encoding="utf-8") as fh:
        json.dump(sites, fh, indent=2)


def import_chrome_passwords_csv(csv_path: str | os.PathLike) -> list[dict]:
    """Import Chrome's exported passwords CSV and return site entries.

    Chrome's password export CSV columns:
        name, url, username_value, password_value, ...

    Returns a list of site dicts suitable for sites.json, with
    user_env / pass_env set to new .env variable names derived from
    the site origin. Callers should write those variables to .env.
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
            if not url or not username:
                continue

            # Derive a site id from the URL host.
            host = re.sub(r"^https?://", "", url).split("/")[0]
            site_id = re.sub(r"[^a-z0-9-]", "-", host.lower()).strip("-")
            if site_id in seen:
                continue
            seen.add(site_id)

            user_env = f"{site_id.upper().replace('-', '_')}_USER"
            pass_env = f"{site_id.upper().replace('-', '_')}_PASS"

            sites.append({
                "id": site_id,
                "name": (row.get("name") or host),
                "login_url": url,
                "checklist_url": "",  # user must fill in
                "user_env": user_env,
                "pass_env": pass_env,
                "selectors": {
                    "username": DEFAULT_USERNAME_SELECTORS,
                    "password": DEFAULT_PASSWORD_SELECTORS,
                    "submit": DEFAULT_SUBMIT_SELECTORS,
                },
                "checklist_selectors": ["main", "[role=\"main\"]", ".checklist", ".registration-checklist", "body"],
            })

    return sites


def _default_chrome_user_data_dir() -> str | None:
    """Return the platform default Chrome User Data directory, if it exists."""
    home = Path.home()
    if sys.platform == "win32":
        candidates = [
            Path(os.getenv("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data",
            home / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
        ]
    elif sys.platform == "darwin":
        candidates = [
            home / "Library" / "Application Support" / "Google" / "Chrome",
        ]
    else:  # linux
        candidates = [
            home / ".config" / "google-chrome",
            home / ".config" / "chromium",
        ]
    for c in candidates:
        if c.exists() and (c / "Local State").exists():
            return str(c)
    return None


def detect_chrome_profiles(user_data_dir: str | None = None) -> list[dict]:
    """Discover Chrome profiles by reading the Local State file.

    Returns a list of dicts with keys: dir_name, name, is_default.
    Sorted so "Default" comes first, then by last_used order.
    """
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
        profile_dir = Path(user_data_dir) / dir_name
        if not profile_dir.exists():
            continue
        profiles.append({
            "dir_name": dir_name,
            "name": info.get("name", dir_name),
            "is_default": dir_name == "Default",
            "user_name": info.get("user_name", ""),
            "last_used_index": (
                last_used.index(dir_name) if dir_name in last_used else 9999
            ),
        })

    # Sort: Default first, then by last_used order, then alphabetically.
    profiles.sort(key=lambda p: (0 if p["is_default"] else 1, p["last_used_index"], p["dir_name"]))
    return profiles


def is_chrome_running(user_data_dir: str | None = None) -> bool:
    """Best-effort check: True if a lockfile exists in the user data dir."""
    if not user_data_dir:
        user_data_dir = _default_chrome_user_data_dir()
    if not user_data_dir:
        return False
    return (Path(user_data_dir) / "lockfile").exists()


# ---------------------------------------------------------------------------
# Credential persistence (credentials.toml, gitignored)
# ---------------------------------------------------------------------------

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None


CREDENTIALS_FILE = BASE_DIR / "credentials.toml"


def _encode_toml_string(value: str) -> str:
    """Escape a string for use as a TOML basic string."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def load_credentials() -> dict[str, dict]:
    """Load all saved credentials from credentials.toml.

    Returns a dict keyed by site id with keys: username, password, profile.
    """
    if not CREDENTIALS_FILE.exists() or tomllib is None:
        return {}
    try:
        with open(CREDENTIALS_FILE, "rb") as fh:
            data = tomllib.load(fh)
        return data.get("sites", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def save_credentials(site_id: str, username: str, password: str, profile: str | None = None) -> None:
    """Save (or update) credentials for a site to credentials.toml."""
    creds = load_credentials()
    entry: dict = {"username": username, "password": password}
    if profile:
        entry["profile"] = profile
    creds[site_id] = entry

    lines = ["[sites]"]
    for sid, entry in sorted(creds.items()):
        lines.append(f"  [{_encode_toml_string(sid)}]")
        lines.append(f"    username = {_encode_toml_string(entry['username'])}")
        lines.append(f"    password = {_encode_toml_string(entry['password'])}")
        if entry.get("profile"):
            lines.append(f"    profile = {_encode_toml_string(entry['profile'])}")
        lines.append("")
    with open(CREDENTIALS_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def get_credentials(site: dict) -> tuple[str, str]:
    """Get (username, password) for a site.

    Priority:
      1. credentials.toml (saved via web UI)
      2. .env variables (user_env / pass_env)
    """
    creds = load_credentials()
    site_id = site.get("id", "")
    if site_id and site_id in creds:
        entry = creds[site_id]
        return entry.get("username", ""), entry.get("password", "")

    # Fall back to .env
    user = os.getenv(site.get("user_env", ""), "")
    password = os.getenv(site.get("pass_env", ""), "")
    return user, password
