from datetime import date, datetime, timedelta, timezone

import pytest

import planner
from security import ConfigError


def setup_college(name="State University"):
    data = planner.load()
    c = planner.college_from_form({"name": name, "admissions_url": "https://admissions.state.edu"}, data=data)
    data["colleges"].append(c)
    planner.save(data)
    return data, c


def visit_form(cid, **kw):
    f = {"college_id": cid, "title": "Fall open house", "kind": "open_house",
         "date": (date.today() + timedelta(days=5)).isoformat(), "start_time": "10:00", "end_time": "12:00",
         "timezone": "America/New_York", "source_url": "https://admissions.state.edu/visit",
         "prep": "Register\nBook parking", "questions": "What is housing like?", "followups": "Thank-you email"}
    f.update(kw)
    return f


def test_college_ids_are_unique_and_links_validated():
    data, c1 = setup_college()
    c2 = planner.college_from_form({"name": "State University"}, data=data)
    assert c1["id"] == "state-university" and c2["id"] == "state-university-2"
    with pytest.raises(ConfigError):
        planner.college_from_form({"name": "X", "admissions_url": "javascript:alert(1)"}, data=data)


def test_visit_lifecycle_and_source_preserved():
    data, c = setup_college()
    v = planner.visit_from_form(visit_form(c["id"]), data)
    assert v["status"] == "planned" and v["source_url"] == "https://admissions.state.edu/visit"
    assert [i["text"] for i in v["prep"]] == ["Register", "Book parking"]
    data["visits"].append(v)

    v["prep"][0]["done"] = True
    edited = planner.visit_from_form(visit_form(c["id"], status="registered", confirmation="ABC123"), data, existing=v)
    assert edited["status"] == "registered" and edited["confirmation"] == "ABC123"
    assert edited["prep"][0]["done"] is True  # done flags survive edits
    assert edited["source_url"] == v["source_url"] and edited["id"] == v["id"]

    planner.set_visit_status(edited, "completed")
    assert edited["status"] == "completed"
    planner.set_visit_status(edited, "cancelled")
    with pytest.raises(ConfigError):
        planner.set_visit_status(edited, "deleted")


def test_visit_requires_official_source_and_valid_fields():
    data, c = setup_college()
    for bad in ({"source_url": ""}, {"source_url": "javascript:x"}, {"date": "next week"},
                {"timezone": "Mars/Base"}, {"start_time": "12:00", "end_time": "11:00"},
                {"college_id": "nope"}, {"meeting_url": "data:text/html,x"}, {"estimated_cost": "-5"}):
        with pytest.raises(ConfigError):
            planner.visit_from_form(visit_form(c["id"], **bad), data)


def test_virtual_visits_and_fairs():
    data, c = setup_college()
    v = planner.visit_from_form(visit_form(c["id"], kind="virtual_tour", meeting_url="https://zoom.us/j/1"), data)
    assert v["format"] == "virtual"
    f = planner.visit_from_form(visit_form(c["id"], kind="college_fair"), data)
    assert f["format"] == "fair"


def test_multiple_visits_per_college_and_summary():
    data, c = setup_college()
    future = planner.visit_from_form(visit_form(c["id"]), data)
    past = planner.visit_from_form(visit_form(c["id"], title="Summer tour",
                                              date=(date.today() - timedelta(days=3)).isoformat()), data)
    cancelled = planner.visit_from_form(visit_form(c["id"], status="cancelled"), data)
    data["visits"] += [future, past, cancelled]
    s = planner.summary(data)
    assert [v["id"] for v in s["upcoming_visits"]] == [future["id"]]
    assert [v["id"] for v in s["needs_wrapup"]] == [past["id"]]


def test_tasks_overdue_upcoming_and_done():
    data, c = setup_college()
    mk = lambda **kw: planner.task_from_form({"title": "T", "college_id": c["id"], **kw}, data)
    overdue = mk(title="Transcript", due_date=(date.today() - timedelta(days=1)).isoformat())
    soon = mk(title="FAFSA", due_date=(date.today() + timedelta(days=3)).isoformat(), source_url="https://studentaid.gov/")
    later = mk(title="Essay", due_date=(date.today() + timedelta(days=60)).isoformat())
    done = mk(title="Done", status="done", due_date=(date.today() - timedelta(days=10)).isoformat())
    data["tasks"] += [overdue, soon, later, done]
    s = planner.summary(data)
    assert [t["title"] for t in s["overdue_tasks"]] == ["Transcript"]
    assert [t["title"] for t in s["upcoming_tasks"]] == ["FAFSA"]
    assert done.get("completed_at")
    planner.set_task_status(done, "todo")
    assert "completed_at" not in done


def test_task_requires_college_and_safe_link():
    data, c = setup_college()
    with pytest.raises(ConfigError):
        planner.task_from_form({"title": "X", "college_id": ""}, data)
    with pytest.raises(ConfigError):
        planner.task_from_form({"title": "X", "college_id": c["id"], "source_url": "javascript:x"}, data)


def test_reminders_are_opt_in_and_sent_once():
    data, c = setup_college()
    due = (date.today() + timedelta(days=2)).isoformat()
    quiet = planner.task_from_form({"title": "Quiet", "college_id": c["id"], "due_date": due}, data)
    loud = planner.task_from_form({"title": "Loud", "college_id": c["id"], "due_date": due,
                                   "remind": "on", "remind_days": "3"}, data)
    v = planner.visit_from_form(visit_form(c["id"], remind="on", remind_days="7"), data)
    data["tasks"] += [quiet, loud]
    data["visits"].append(v)
    planner.save(data)
    titles = [r["title"] for r in planner.due_reminders(planner.load())]
    assert any("Loud" in t for t in titles) and not any("Quiet" in t for t in titles)
    assert any("open house" in t for t in titles)
    assert planner.send_due_reminders() == 2
    assert planner.send_due_reminders() == 0
    import history
    assert sum(1 for a in history.open_alerts() if a["kind"] == "reminder") == 2


def test_delete_college_guarded():
    data, c = setup_college()
    data["tasks"].append(planner.task_from_form({"title": "X", "college_id": c["id"]}, data))
    with pytest.raises(ConfigError):
        planner.delete_college(data, c["id"])


def test_ics_export():
    data, c = setup_college()
    data["visits"].append(planner.visit_from_form(visit_form(c["id"]), data))
    data["tasks"].append(planner.task_from_form({"title": "FAFSA, due", "college_id": c["id"],
                                                 "due_date": "2027-01-15"}, data))
    ics = planner.to_ics(data)
    assert ics.startswith("BEGIN:VCALENDAR") and ics.count("BEGIN:VEVENT") == 2
    assert "URL:https://admissions.state.edu/visit" in ics and "FAFSA\\, due" in ics
