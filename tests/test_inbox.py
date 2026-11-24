"""Email filtering: domains, categorisation, Gmail export, read-only IMAP sync, scheduler."""

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest

import history
import inbox
import planner
import scheduler
import scraper
from security import ConfigError


def post(client, path, token, **data):
    return client.post(path, data={"csrf_token": token, **data})


def add_college(name, domains, **kw):
    data = planner.load()
    c = planner.college_from_form({"name": name, "email_domains": domains, **kw}, data=data)
    data["colleges"].append(c)
    planner.save(data)
    return c


def raw_mail(frm, subject, body, mid):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "me@example.com", subject
    m["Date"] = "Mon, 23 Nov 2026 10:00:00 +0000"
    m["Message-ID"] = mid
    m.set_content(body)
    return m.as_bytes()


class FakeIMAP:
    """Minimal IMAP4 stand-in that records every command it receives."""

    def __init__(self, messages, password="app-pass"):
        self.messages = messages  # uid -> (from, raw bytes)
        self.password = password
        self.calls = []

    def login(self, user, password):
        self.calls.append(("login", user))
        if password != self.password:
            import imaplib
            raise imaplib.IMAP4.error("auth failed")

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"3"]

    def uid(self, cmd, *args):
        self.calls.append(("uid", cmd) + args)
        if cmd == "SEARCH":
            domain = args[-1].strip('"')
            hits = [u for u, (frm, _) in self.messages.items() if frm.endswith(domain + ">") or domain in frm]
            return "OK", [b" ".join(hits)]
        if cmd == "FETCH":
            assert "PEEK" in args[1], "sync must never mark mail as read"
            return "OK", [(b"1 (BODY[]", self.messages[args[0]][1]), b")"]
        raise AssertionError(f"unexpected IMAP command {cmd}")

    def logout(self):
        self.calls.append(("logout",))


# --------------------------------------------------------------------- domains

def test_parse_domains_normalises_and_rejects_junk():
    assert planner.parse_domains("Northfield.EDU, @mail.northfield.edu; x@lakeside.edu") == [
        "northfield.edu", "mail.northfield.edu", "lakeside.edu"]
    with pytest.raises(ConfigError):
        planner.parse_domains("not a domain!")
    assert planner.suggest_domain("https://admissions.northfield.edu/visit") == "northfield.edu"


def test_match_college_by_domain_and_subdomain():
    c = add_college("Northfield University", "northfield.edu")
    colleges = planner.load()["colleges"]
    assert inbox.match_college("Admissions <admit@northfield.edu>", colleges)[0]["id"] == c["id"]
    assert inbox.match_college("aid@finaid.northfield.edu", colleges)[0]["id"] == c["id"]
    assert inbox.match_college("phish@northfield.edu.evil.com", colleges)[0] is None
    assert inbox.match_college("x@notnorthfield.edu", colleges)[0] is None


# --------------------------------------------------------------------- categories

@pytest.mark.parametrize("subject,body,expected", [
    ("Your FAFSA is missing information", "", "financial_aid"),
    ("Join us for an Open House", "Campus tour at 10am", "visit"),
    ("Transcript received", "We received your school report.", "transcript"),
    ("Housing application now open", "Choose a residence hall", "housing"),
    ("Hello", "Just saying hi", "other"),
])
def test_categorize(subject, body, expected):
    assert inbox.categorize(subject, body)[0] == expected


def test_keyword_match_is_whole_word():
    # "sat" must not match "Saturday".
    assert inbox.categorize("Saturday plans", "")[0] == "other"


def test_important_flags():
    assert "action required" in inbox.is_important("Action required: missing document", "")


def test_custom_rules_saved_and_reclassify(client, token):
    add_college("Northfield University", "northfield.edu")
    inbox.save_emails([{"id": "a1", "from": "x@northfield.edu", "subject": "Robotics club invite",
                        "snippet": "", "college_id": "northfield-university", "category": "other"}])
    r = post(client, "/email/rules", token, rule_visit="robotics club")
    assert r.status_code == 302
    assert inbox.load_emails()[0]["category"] == "visit"
    post(client, "/email/rules", token, reset="1")
    assert inbox.load_rules() == inbox.DEFAULT_RULES


# --------------------------------------------------------------------- Gmail export

def test_gmail_filter_xml_is_valid_and_label_only(client, token):
    add_college("Northfield University", "northfield.edu, mail.northfield.edu")
    add_college("Lakeside College", "lakeside.edu")
    r = client.get("/email/gmail-filters.xml?prefix=College")
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    root = ET.fromstring(r.data)
    ns = {"a": "http://www.w3.org/2005/Atom", "apps": "http://schemas.google.com/apps/2006"}
    entries = root.findall("a:entry", ns)
    assert len(entries) == 2 + len([k for k, v in inbox.DEFAULT_RULES.items() if v])
    props = [{p.get("name"): p.get("value") for p in e.findall("apps:property", ns)} for e in entries]
    labels = {p["label"] for p in props}
    assert "College/Northfield University" in labels and "College/Financial aid" in labels
    allowed = {"from", "hasTheWord", "label", "shouldNeverSpam"}
    assert all(set(p) <= allowed for p in props), "filters must never delete, archive or forward"
    assert props[0]["from"] in ("(lakeside.edu)", "(northfield.edu OR mail.northfield.edu)")


def test_filter_queries_without_domains_are_empty():
    add_college("No Domain U", "")
    assert inbox.filter_rules() == []


# --------------------------------------------------------------------- IMAP sync

def configure_account():
    scraper.save_imap_settings({"host": "imap.example.com", "port": 993, "username": "me@example.com",
                                "password": "app-pass", "folder": "INBOX", "days": 30})


def test_imap_settings_coexist_with_site_credentials():
    scraper.save_credentials("state-u", "student", "pw1")
    configure_account()
    scraper.save_credentials("other", "u2", "pw2")
    assert scraper.load_imap_settings()["host"] == "imap.example.com"
    assert set(scraper.load_credentials()) == {"state-u", "other"}
    scraper.save_imap_settings(None)
    assert scraper.load_imap_settings() == {} and "state-u" in scraper.load_credentials()


def test_sync_is_read_only_and_keeps_only_college_mail():
    add_college("Northfield University", "northfield.edu")
    configure_account()
    fake = FakeIMAP({
        b"1": ("<admit@northfield.edu>", raw_mail("Northfield Admissions <admit@northfield.edu>",
                                                  "Action required: transcript missing",
                                                  "Please send your final transcript.", "<m1@northfield.edu>")),
        b"2": ("<news@northfield.edu>", raw_mail("news@northfield.edu", "Open House this Saturday",
                                                 "Campus tour and info session.", "<m2@northfield.edu>")),
    })
    out = inbox.sync(imap_factory=lambda: fake)
    assert out == {"scanned": 2, "matched": 2, "added": 2}
    assert ("select", "INBOX", True) in fake.calls  # folder opened read-only
    assert not any(c[0] == "uid" and c[1] in ("STORE", "COPY", "MOVE", "EXPUNGE") for c in fake.calls)
    stored = {m["subject"]: m for m in inbox.load_emails()}
    assert stored["Action required: transcript missing"]["category"] == "transcript"
    assert stored["Action required: transcript missing"]["important"]
    assert stored["Open House this Saturday"]["category"] == "visit"
    assert all("body" not in m for m in stored.values())
    assert any(a["kind"] == "email" for a in history.open_alerts())

    # Second sync: no duplicates, no new alerts, manual category kept.
    mid = stored["Open House this Saturday"]["id"]
    inbox.set_email_fields(mid, category="housing", category_locked=True, done=True)
    out = inbox.sync(imap_factory=lambda: fake)
    assert out["added"] == 0 and len(inbox.load_emails()) == 2
    again = {m["id"]: m for m in inbox.load_emails()}[mid]
    assert again["category"] == "housing" and again["done"]


def test_sync_ignores_lookalike_domains():
    add_college("Northfield University", "northfield.edu")
    configure_account()
    fake = FakeIMAP({b"9": ("northfield.edu", raw_mail("x@northfield.edu.evil.com", "Scholarship!",
                                                        "click", "<p@evil>"))})
    assert inbox.sync(imap_factory=lambda: fake)["matched"] == 0
    assert inbox.load_emails() == []


def test_sync_bad_password_is_friendly():
    add_college("Northfield University", "northfield.edu")
    configure_account()
    with pytest.raises(ConfigError, match="app password"):
        inbox.sync(imap_factory=lambda: FakeIMAP({}, password="other"))


def test_sync_requires_setup():
    with pytest.raises(ConfigError, match="isn't set up"):
        inbox.sync()


# --------------------------------------------------------------------- web UI

def test_email_pages_render_and_filter(client, token):
    c = add_college("Northfield University", "northfield.edu")
    inbox.save_emails([
        {"id": "e1", "from": "a@northfield.edu", "from_domain": "northfield.edu", "subject": "FAFSA reminder",
         "snippet": "Complete it", "date": "2026-11-20T10:00:00+00:00", "college_id": c["id"],
         "category": "financial_aid", "keywords": ["fafsa"], "important": ["deadline"]},
        {"id": "e2", "from": "b@northfield.edu", "from_domain": "northfield.edu", "subject": "Campus tour",
         "snippet": "", "date": "2026-11-21T10:00:00+00:00", "college_id": c["id"],
         "category": "visit", "keywords": [], "important": []},
    ])
    for url in ["/email", "/email/setup", "/email?category=visit", "/email?show=important", "/settings", "/"]:
        assert client.get(url).status_code == 200, url
    page = client.get("/email?category=financial_aid").get_data(as_text=True)
    assert "FAFSA reminder" in page and "Campus tour" not in page

    post(client, "/email/e2/update", token, category="housing")
    assert {m["id"]: m for m in inbox.load_emails()}["e2"]["category_locked"]
    post(client, "/email/e1/update", token, done="1")
    assert "FAFSA reminder" not in client.get("/email").get_data(as_text=True)
    assert post(client, "/email/e1/update", token, category="nope").status_code == 400


def test_email_account_form_keeps_password(client, token):
    post(client, "/email/account", token, host="imap.gmail.com", port="993", username="me@example.com",
         password="secret-app-pw", folder="INBOX", days="60")
    post(client, "/email/account", token, host="imap.gmail.com", port="993", username="me@example.com",
         password="", folder="INBOX", days="30")
    cfg = inbox.account_config()
    assert cfg["password"] == "secret-app-pw" and cfg["days"] == 30
    assert "secret-app-pw" not in client.get("/email/setup").get_data(as_text=True)
    post(client, "/email/account", token, host="bad host", username="x")
    assert inbox.account_config()["host"] == "imap.gmail.com"


def test_college_form_saves_email_domains(client, token):
    post(client, "/colleges/add", token, name="Lakeside College", email_domains="lakeside.edu, @apply.lakeside.edu")
    assert planner.load()["colleges"][0]["email_domains"] == ["lakeside.edu", "apply.lakeside.edu"]


# --------------------------------------------------------------------- scheduler

def test_schedule_due_logic(client, token):
    assert scheduler.load()["check_hours"] == 0
    assert not scheduler.due("checks", 0)
    post(client, "/settings/schedule", token, check_hours="6", email_hours="0", reminders="on")
    s = scheduler.load()
    assert s["check_hours"] == 6 and scheduler.due("checks", 6, s=s)
    scheduler.mark_run("checks")
    s = scheduler.load()
    assert not scheduler.due("checks", 6, s=s)
    later = datetime.now(timezone.utc) + timedelta(hours=7)
    assert scheduler.due("checks", 6, now=later, s=s)
    post(client, "/settings/schedule", token, check_hours="0.1")
    assert scheduler.load()["check_hours"] == 6  # rejected


def test_tick_skips_unconfigured_email(monkeypatch):
    import jobs
    scheduler.save_from_form({"check_hours": "0", "email_hours": "2", "reminders": "on"})
    started = []
    monkeypatch.setattr(jobs, "start", lambda *a, **k: started.append(a) or True)
    assert scheduler.tick() is None and started == []
    assert scheduler.load()["last"].get("email")  # marked so it doesn't retry every minute
