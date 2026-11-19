"""
College tracker scraper.

Reads sites.json (the list of websites to log into and check) and
.env (stored credentials), then uses Playwright to log into each site,
navigate to its registration-checklist page, and save whatever it finds
to data/results.json so the Flask site can display it.
"""

from __future__ import annotations

import json
import os
import re
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
    user = os.getenv(site.get("user_env", ""), "")
    password = os.getenv(site.get("pass_env", ""), "")
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


def run_scrape(headless: bool = True) -> list[dict]:
    load_dotenv(ENV_FILE)
    sites = load_sites()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            for site in sites:
                results.append(scrape_site(browser, site))
        finally:
            browser.close()

    with open(RESULTS_FILE, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)

    return results
