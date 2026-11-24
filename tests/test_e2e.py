"""End-to-end checks against a local mock portal with real Playwright Chromium.

Skipped automatically when Playwright's Chromium is not installed.
"""

import json
import os

import pytest

import history
import scraper
from tests.mock_portal import PASSWORD, STATE, USER, PortalServer

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def portal():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Playwright Chromium unavailable: {exc}")
    with PortalServer() as srv:
        yield srv


@pytest.fixture(autouse=True)
def local_targets(monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_TARGETS", "1")
    STATE.update(items=["Complete FAFSA application", "Meet with academic advisor"], hold=False)


def write_sites(sites):
    with open(os.environ["COLLEGE_TRACKER_SITES_FILE"], "w", encoding="utf-8") as fh:
        json.dump(sites, fh)


def portal_site(url, auth="credentials", **kw):
    return {"id": "mock", "name": "Mock Portal", "auth": auth, "login_url": f"{url}/login",
            "checks": [{"id": "checklist", "name": "Checklist", "url": f"{url}/checklist", "watch": ["hold"]},
                       {"id": "aid", "name": "Aid", "url": f"{url}/aid"}], **kw}


def test_credentials_login_multiple_checks_history_and_session(portal):
    write_sites([portal_site(portal.url)])
    scraper.save_credentials("mock", USER, PASSWORD)
    results = scraper.run_checks(headless=True)
    assert [r["status"] for r in results] == ["ok", "ok"]
    assert "Complete FAFSA application" in results[0]["excerpt"]
    assert "Award letter" in results[1]["excerpt"]
    assert scraper.session_info(scraper.get_site("mock"))["saved"]
    assert all(PASSWORD not in json.dumps(r) for r in scraper.load_results())

    # Content change + watched keyword appear.
    STATE.update(items=["Complete FAFSA application"], hold=True)
    results = scraper.run_checks(headless=True)
    change = results[0]["change"]
    assert change["changed"] and change["watch_added"] == ["hold"]
    kinds = {a["kind"] for a in history.open_alerts()}
    assert "watch" in kinds
    assert len(history.load_history("mock", "checklist")) == 2


def test_saved_session_reused_without_credentials(portal):
    write_sites([portal_site(portal.url)])
    scraper.save_credentials("mock", USER, PASSWORD)
    scraper.run_checks(headless=True)
    scraper.delete_credentials("mock")
    results = scraper.run_checks(headless=True)
    assert [r["status"] for r in results] == ["ok", "ok"]


def test_wrong_password_is_login_failed_not_change(portal):
    write_sites([portal_site(portal.url)])
    scraper.save_credentials("mock", USER, "wrong-password")
    results = scraper.run_checks(headless=True)
    assert {r["status"] for r in results} == {"login_failed"}
    assert all("wrong-password" not in (r["error"] or "") for r in results)
    assert not results[0]["change"]["changed"]


def test_manual_mode_without_session_requires_sign_in(portal):
    write_sites([portal_site(portal.url, auth="manual")])
    results = scraper.run_checks(headless=True)
    assert {r["status"] for r in results} == {"reauth_required"}
    assert any(a["kind"] == "reauth" for a in history.open_alerts())


def test_expired_manual_session_reports_reauth(portal):
    write_sites([portal_site(portal.url, auth="manual")])
    path = scraper.auth_state_path("mock")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    results = scraper.run_checks(headless=True)
    assert {r["status"] for r in results} == {"reauth_required"}


def test_mfa_challenge_stops_for_human(portal):
    site = {"id": "mock", "name": "MFA Portal", "auth": "credentials", "login_url": f"{portal.url}/mfa/login",
            "checks": [{"id": "home", "name": "Home", "url": f"{portal.url}/mfa/home"}]}
    write_sites([site])
    scraper.save_credentials("mock", USER, PASSWORD)
    results = scraper.run_checks(headless=True)
    assert results[0]["status"] == "reauth_required"
    assert "MFA" in results[0]["error"] or "verification" in results[0]["error"]


def test_public_check_and_local_targets_blocked_by_default(portal, monkeypatch):
    write_sites([{"id": "pub", "name": "Public", "auth": "none",
                  "checks": [{"id": "open", "name": "Open house", "url": f"{portal.url}/public"}]}])
    assert scraper.run_checks(headless=True)[0]["status"] == "ok"
    monkeypatch.delenv("ALLOW_LOCAL_TARGETS")
    r = scraper.run_checks(headless=True)[0]
    assert r["status"] == "config_error" and "local or private" in r["error"]


def test_interactive_sign_in_saves_session_and_clears_alert(portal):
    import threading
    write_sites([portal_site(portal.url, auth="manual")])
    scraper.run_checks(headless=True)
    assert any(a["kind"] == "reauth" for a in history.open_alerts())
    stop = threading.Event()
    timer = threading.Timer(3, stop.set)  # stands in for "I've signed in"
    timer.start()
    out = scraper.interactive_sign_in("mock", stop=stop, headless=True, timeout_s=30)
    assert out == {"saved": True, "reason": "done"}
    assert scraper.session_info(scraper.get_site("mock"))["saved"]
    assert not any(a["kind"] == "reauth" for a in history.open_alerts())
