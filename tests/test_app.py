import json
import os
from datetime import date, timedelta
from pathlib import Path

import planner
import scraper


def post(client, path, token, **data):
    return client.post(path, data={"csrf_token": token, **data})


def test_pages_render(client, token):
    for url in ["/", "/sites", "/colleges", "/visits", "/visits/new", "/tasks", "/tasks/new", "/settings"]:
        r = client.get(url)
        assert r.status_code == 200, url
        assert "Content-Security-Policy" in r.headers


def test_post_without_csrf_is_rejected(client):
    r = client.post("/colleges/add", data={"name": "Sneaky U"})
    assert r.status_code == 400
    assert planner.load()["colleges"] == []


def test_foreign_host_header_rejected(client):
    r = client.get("/", headers={"Host": "evil.example.com"})
    assert r.status_code == 400


def test_add_site_validates_urls(client, token):
    r = post(client, "/sites/add", token, name="Bad", auth="manual",
             login_url="javascript:alert(1)", check_url="https://portal.example.edu/c")
    assert r.status_code == 302 and scraper.load_sites() == []
    r = post(client, "/sites/add", token, name="Internal", auth="none",
             check_url="http://169.254.169.254/latest")
    assert scraper.load_sites() == []
    r = post(client, "/sites/add", token, name="State U", id="state-u", auth="manual",
             login_url="https://portal.example.edu/login", check_url="https://portal.example.edu/checklist",
             check_watch="hold, missing")
    sites = scraper.load_sites()
    assert len(sites) == 1 and sites[0]["checks"][0]["watch"] == ["hold", "missing"]


def test_multiple_checks_per_site(client, token):
    post(client, "/sites/add", token, name="State U", id="state-u", auth="none",
         check_url="https://portal.example.edu/checklist")
    post(client, "/sites/state-u/checks/add", token, name="Financial aid", url="https://portal.example.edu/aid",
         selectors="main\n#aid", watch="award")
    r = post(client, "/sites/state-u/checks/add", token, name="Bad", url="file:///etc/passwd")
    site = scraper.get_site("state-u")
    assert [c["id"] for c in site["checks"]] == ["checklist", "financial-aid"]
    assert site["checks"][1]["selectors"] == ["main", "#aid"]
    post(client, "/sites/state-u/checks/financial-aid/delete", token)
    assert len(scraper.get_site("state-u")["checks"]) == 1
    assert client.get("/sites/state-u/edit").status_code == 200


def test_legacy_sites_json_is_upgraded(isolated):
    legacy = [{"id": "old", "name": "Old", "login_url": "https://p.example.edu/login",
               "checklist_url": "https://p.example.edu/check", "user_env": "OLD_USER", "pass_env": "OLD_PASS",
               "checklist_selectors": ["main"]}]
    Path(os.environ["COLLEGE_TRACKER_SITES_FILE"]).write_text(json.dumps(legacy), encoding="utf-8")
    site = scraper.load_sites()[0]
    assert site["auth"] == "credentials"
    assert site["checks"] == [{"id": "checklist", "name": "Registration checklist",
                               "url": "https://p.example.edu/check", "selectors": ["main"], "watch": []}]
    assert scraper.validate_site(site) == []


def test_credentials_never_written_to_sites_json(client, token):
    post(client, "/sites/add", token, name="State U", id="state-u", auth="credentials",
         login_url="https://portal.example.edu/login", check_url="https://portal.example.edu/checklist")
    post(client, "/sites/state-u/credentials", token, username="me@x.edu", password='p"a\\ss\nword')
    assert scraper.get_credentials(scraper.get_site("state-u")) == ("me@x.edu", 'p"a\\ss\nword')
    assert "p\\\"a" not in Path(os.environ["COLLEGE_TRACKER_SITES_FILE"]).read_text()
    assert "me@x.edu" not in Path(os.environ["COLLEGE_TRACKER_SITES_FILE"]).read_text()
    page = client.get("/sites").get_data(as_text=True)
    assert "credentials saved" in page and 'p"a' not in page
    post(client, "/sites/state-u/delete", token)
    assert scraper.load_credentials() == {}


def test_legacy_credentials_toml_still_loads(isolated):
    Path(os.environ["COLLEGE_TRACKER_CREDENTIALS_FILE"]).write_text(
        '[sites]\n  ["old"]\n    username = "u"\n    password = "p"\n', encoding="utf-8")
    assert scraper.load_credentials()["old"]["password"] == "p"


def test_chrome_csv_import(client, token, tmp_path):
    csv_text = ("name,url,username_value,password_value\n"
                "Portal,https://portal.example.edu/login,me,pw1\n"
                "Bad,javascript:alert(1),me,pw2\n")
    from io import BytesIO
    r = client.post("/sites/import-chrome-passwords", data={
        "csrf_token": token, "save_credentials": "on",
        "csv_file": (BytesIO(csv_text.encode()), "passwords.csv")}, content_type="multipart/form-data")
    assert r.status_code == 200
    sites = scraper.load_sites()
    assert [s["id"] for s in sites] == ["portal-example-edu"]
    assert "password" not in sites[0] and scraper.load_credentials()["portal-example-edu"]["password"] == "pw1"


def test_college_visit_task_flow(client, token):
    post(client, "/colleges/add", token, name="State University", admissions_url="https://admissions.state.edu")
    cid = planner.load()["colleges"][0]["id"]
    day = (date.today() + timedelta(days=4)).isoformat()
    r = post(client, "/visits/new", token, college_id=cid, title="Campus tour", kind="campus_tour",
             date=day, start_time="10:00", timezone="America/Chicago",
             source_url="https://admissions.state.edu/visit", prep="Register online", followups="Email counselor")
    assert r.status_code == 302
    vid = planner.load()["visits"][0]["id"]
    assert "Campus tour" in client.get("/").get_data(as_text=True)

    r = post(client, "/visits/new", token, college_id=cid, title="No source", date=day, source_url="")
    assert r.status_code == 400 and len(planner.load()["visits"]) == 1

    post(client, f"/visits/{vid}/status", token, status="registered")
    post(client, f"/visits/{vid}/toggle", token, field="prep", index="0")
    v = planner.load()["visits"][0]
    assert v["status"] == "registered" and v["prep"][0]["done"]

    r = post(client, f"/visits/{vid}/followup-task", token, index="0")
    assert "/tasks/new" in r.headers["Location"]
    post(client, "/tasks/new", token, title="Email counselor", college_id=cid, category="visit", visit_id=vid,
         due_date=(date.today() - timedelta(days=1)).isoformat(), source_url="https://admissions.state.edu/visit")
    t = planner.load()["tasks"][0]
    page = client.get("/").get_data(as_text=True)
    assert "Email counselor" in page and "Overdue" in page
    post(client, f"/tasks/{t['id']}/status", token, status="done")
    assert planner.load()["tasks"][0]["status"] == "done"

    post(client, f"/visits/{vid}/status", token, status="cancelled")
    assert planner.load()["visits"][0]["status"] == "cancelled"
    assert client.get(f"/visits/{vid}").status_code == 200
    assert client.get("/calendar.ics").mimetype == "text/calendar"


def test_redirects_are_local_only(client, token):
    r = post(client, "/alerts/all/dismiss", token, next="https://evil.example.com/")
    assert r.headers["Location"] == "/"


def test_check_history_page(client, token):
    post(client, "/sites/add", token, name="State U", id="state-u", auth="none",
         check_url="https://portal.example.edu/checklist")
    import history
    history.record_run({"site_id": "state-u", "check_id": "checklist", "status": "ok", "excerpt": "one",
                        "checked_at": "2026-11-01T00:00:00+00:00"})
    history.record_run({"site_id": "state-u", "check_id": "checklist", "status": "ok", "excerpt": "two",
                        "checked_at": "2026-11-02T00:00:00+00:00"})
    page = client.get("/history/state-u/checklist").get_data(as_text=True)
    assert "Changed" in page and "two" in page


def test_hand_written_site_without_selectors_renders(client, isolated):
    Path(os.environ["COLLEGE_TRACKER_SITES_FILE"]).write_text(json.dumps([
        {"id": "hand", "name": "Hand", "auth": "none",
         "checks": [{"id": "c", "name": "C", "url": "https://portal.example.edu/c"}]}]), encoding="utf-8")
    assert client.get("/sites/hand/edit").status_code == 200
    assert client.get("/sites").status_code == 200
