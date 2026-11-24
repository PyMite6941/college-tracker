"""Regenerate docs/screenshots/*.png from fictional demo data.

    python scripts/screenshots.py

Everything runs in a temporary directory: your real data/, sites.json and
credentials.toml are never read or touched. The demo uses the mock portal
from tests/ and made-up colleges on example.edu domains.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ct-demo-"))
os.environ.update(
    COLLEGE_TRACKER_DATA_DIR=str(TMP / "data"),
    COLLEGE_TRACKER_SITES_FILE=str(TMP / "sites.json"),
    COLLEGE_TRACKER_CREDENTIALS_FILE=str(TMP / "credentials.toml"),
    ALLOW_LOCAL_TARGETS="1",
    NOTIFY_WEBHOOK_URL="",
)
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

import planner  # noqa: E402
import scraper  # noqa: E402
from tests.mock_portal import PASSWORD, STATE, USER, PortalServer  # noqa: E402

OUT = ROOT / "docs" / "screenshots"


def seed(portal_url: str) -> dict:
    d = lambda n: (date.today() + timedelta(days=n)).isoformat()
    data = planner.load()
    for name, loc, url in [("Northfield University", "Northfield, MN", "https://admissions.northfield.example.edu"),
                           ("Lakeside College", "Lakeside, OR", "https://www.lakeside.example.edu/admissions")]:
        data["colleges"].append(planner.college_from_form(
            {"name": name, "location": loc, "admissions_url": url, "visit_url": url + "/visit",
             "email_domains": planner.suggest_domain(url)}, data=data))
    nf, lk = data["colleges"][0]["id"], data["colleges"][1]["id"]
    tour = planner.visit_from_form({
        "college_id": nf, "title": "Campus tour + info session", "kind": "campus_tour", "date": d(6),
        "start_time": "10:00", "end_time": "12:30", "timezone": "America/Chicago",
        "source_url": "https://admissions.northfield.example.edu/visit", "status": "registered",
        "confirmation": "Confirmation #NF-2291", "location": "Welcome Center, 1 College Way",
        "directions_url": "https://maps.example.com/?q=Northfield+Welcome+Center",
        "logistics": "Drive 3h; park in Lot C (free on Saturdays).", "estimated_cost": "45",
        "prep": "Register online\nPrint unofficial transcript\nBook parking",
        "questions": "What is first-year housing like?\nHow big are intro CS classes?",
        "followups": "Email admissions counselor\nRequest alumni interview", "remind": "on", "remind_days": "7"}, data)
    tour["prep"][0]["done"] = True
    fair = planner.visit_from_form({
        "college_id": lk, "title": "Regional college fair", "kind": "college_fair", "date": d(12),
        "start_time": "18:00", "end_time": "20:00", "timezone": "America/Los_Angeles",
        "source_url": "https://www.lakeside.example.edu/admissions/events", "location": "Convention Center Hall B"}, data)
    virt = planner.visit_from_form({
        "college_id": lk, "title": "Virtual engineering info session", "kind": "virtual_tour", "date": d(3),
        "start_time": "16:00", "timezone": "America/Los_Angeles", "status": "confirmed",
        "source_url": "https://www.lakeside.example.edu/admissions/virtual",
        "meeting_url": "https://meet.example.com/lakeside-eng"}, data)
    data["visits"] += [tour, fair, virt]
    for f in [
        {"title": "Request transcript from counselor", "college_id": nf, "category": "transcript", "due_date": d(-2), "priority": "high"},
        {"title": "Submit Early Action application", "college_id": nf, "category": "application", "due_date": d(5),
         "source_url": "https://admissions.northfield.example.edu/apply", "remind": "on", "remind_days": "7"},
        {"title": "Finish supplemental essay", "college_id": lk, "category": "essay", "due_date": d(9), "status": "in_progress"},
        {"title": "CSS Profile", "college_id": lk, "category": "financial_aid", "due_date": d(30)},
        {"title": "Ask Ms. Rivera for a recommendation", "college_id": nf, "category": "recommendation"},
    ]:
        data["tasks"].append(planner.task_from_form(f, data))
    planner.save(data)

    # Fictional college email, classified by the real rules.
    import inbox
    mail = []
    for i, (cid, frm, subj, body, ago) in enumerate([
        (nf, "Northfield Admissions <admissions@northfield.example.edu>", "Action required: transcript missing",
         "Your application is incomplete. Please ask your counselor to send your transcript.", 1),
        (nf, "Northfield Financial Aid <finaid@northfield.example.edu>", "Your CSS Profile deadline is Dec 1",
         "Submit the CSS Profile to be considered for need-based aid.", 2),
        (lk, "Lakeside Visits <visit@lakeside.example.edu>", "Join us for a virtual info session",
         "Meet our admissions team and current students online.", 3),
        (nf, "Northfield Housing <housing@northfield.example.edu>", "Residence hall preview webinar",
         "Learn about first-year housing options.", 5),
        (lk, "Lakeside Scholarships <aid@lakeside.example.edu>", "Merit scholarship consideration",
         "All applicants are considered for merit awards.", 8),
    ]):
        when = (datetime.now(timezone.utc) - timedelta(days=ago)).isoformat()
        cat, hits = inbox.categorize(subj, body)
        mail.append({"id": f"demo{i}", "message_id": "", "from": frm, "from_name": frm.split(" <")[0],
                     "from_domain": frm.split("@")[1].rstrip(">"), "subject": subj, "date": when,
                     "snippet": body, "college_id": cid, "category": cat, "keywords": hits,
                     "important": inbox.is_important(subj, body), "matched_by": "demo"})
    inbox.save_emails(mail)

    scraper.save_sites([{
        "id": "northfield-portal", "name": "Northfield applicant portal", "auth": "credentials",
        "college_id": nf, "login_url": f"{portal_url}/login",
        "checks": [{"id": "checklist", "name": "Application checklist", "url": f"{portal_url}/checklist", "watch": ["hold"]},
                   {"id": "aid", "name": "Financial aid", "url": f"{portal_url}/aid"}]},
        {"id": "lakeside-events", "name": "Lakeside open house page", "auth": "none", "college_id": lk,
         "checks": [{"id": "events", "name": "Open house dates", "url": f"{portal_url}/public"}]}])
    scraper.save_credentials("northfield-portal", USER, PASSWORD)
    return {"nf": nf, "tour": tour["id"]}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with PortalServer() as portal:
        ids = seed(portal.url)
        STATE.update(items=["Submit application", "Send transcript", "Pay application fee"], hold=False)
        scraper.run_checks()
        STATE.update(items=["Submit application", "Send transcript"], hold=True)
        scraper.run_checks()

        import app as app_module
        server = make_server("127.0.0.1", 0, app_module.create_app())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        problems = []
        shots = [("dashboard", "/", 1400), ("tasks", "/tasks", 900), ("visits", "/visits", 800),
                 ("visit", f"/visits/{ids['tour']}", 1000), ("college", f"/colleges/{ids['nf']}", 1100),
                 ("sites", "/sites", 1300), ("site-edit", "/sites/northfield-portal/edit", 1200),
                 ("history", "/history/northfield-portal/checklist", 900), ("visit-form", "/visits/new", 1600),
                 ("email", "/email", 1000), ("email-setup", "/email/setup", 1700), ("settings", "/settings", 1000)]
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for scheme in ("light", "dark"):
                page = browser.new_page(viewport={"width": 1200, "height": 900}, color_scheme=scheme)
                page.on("console", lambda m: problems.append(f"{m.type}: {m.text}") if m.type in {"error", "warning"} else None)
                page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
                for name, path, height in shots:
                    page.set_viewport_size({"width": 1200, "height": height})
                    page.goto(base + path)
                    if page.locator("details").count():
                        for det in page.locator("details").all()[:2]:
                            det.evaluate("d => d.open = true")
                    suffix = "" if scheme == "light" else "-dark"
                    if scheme == "light" or name in {"dashboard", "visit"}:
                        page.screenshot(path=str(OUT / f"{name}{suffix}.png"), full_page=False)
                page.close()
            mobile = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
            mobile.goto(base + "/")
            if mobile.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
                problems.append("mobile: horizontal scroll on dashboard")
            mobile.screenshot(path=str(OUT / "dashboard-mobile.png"))
            browser.close()
        server.shutdown()
    print(f"Wrote screenshots to {OUT}")
    if problems:
        print("Browser problems:\n  " + "\n  ".join(sorted(set(problems))))
        sys.exit(1)


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    main()
