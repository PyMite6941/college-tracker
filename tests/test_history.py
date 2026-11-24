import json
from datetime import datetime, timedelta, timezone

import history


def run(status="ok", excerpt="A\nB", when=None, **kw):
    return {"site_id": "s", "check_id": "c", "status": status, "excerpt": excerpt,
            "checked_at": (when or datetime.now(timezone.utc)).isoformat(), "error": kw.get("error")}


def test_first_run_then_change_with_diff():
    r1 = history.record_run(run(excerpt="Holds: none\nFAFSA: missing"))
    assert r1["change"]["first_run"] and not r1["change"]["changed"]
    r2 = history.record_run(run(excerpt="Holds: none\nFAFSA: missing"))
    assert not r2["change"]["changed"]
    r3 = history.record_run(run(excerpt="Holds: none\nFAFSA: received"))
    assert r3["change"]["changed"]
    ops = {(d["op"], d["text"]) for d in r3["change"]["diff"]}
    assert ("-", "FAFSA: missing") in ops and ("+", "FAFSA: received") in ops
    assert len(history.load_history("s", "c")) == 3


def test_whitespace_only_edits_are_not_changes():
    history.record_run(run(excerpt="A  B\n\nC"))
    assert not history.record_run(run(excerpt="A B\nC  "))["change"]["changed"]


def test_errors_are_not_content_changes():
    history.record_run(run(excerpt="Checklist v1"))
    err = history.record_run(run(status="network_error", excerpt=None, error="timeout"))
    assert not err["change"]["changed"]
    back = history.record_run(run(excerpt="Checklist v1"))
    assert not back["change"]["changed"] and back["change"]["recovered"]


def test_watch_keywords_added_and_removed():
    history.record_run(run(excerpt="All good"), watch=["hold"])
    r = history.record_run(run(excerpt="Registration HOLD: transcript"), watch=["hold"])
    assert r["change"]["watch_added"] == ["hold"]
    r = history.record_run(run(excerpt="All good"), watch=["hold"])
    assert r["change"]["watch_removed"] == ["hold"]


def test_retention_by_count(monkeypatch):
    monkeypatch.setenv("HISTORY_MAX_RUNS", "3")
    for i in range(6):
        history.record_run(run(excerpt=f"v{i}"))
    runs = history.load_history("s", "c")
    assert [r["excerpt"] for r in runs] == ["v3", "v4", "v5"]


def test_retention_by_age(monkeypatch):
    monkeypatch.setenv("HISTORY_MAX_DAYS", "30")
    old = datetime.now(timezone.utc) - timedelta(days=60)
    history.record_run(run(excerpt="old", when=old))
    history.record_run(run(excerpt="new"))
    assert [r["excerpt"] for r in history.load_history("s", "c")] == ["new"]


def test_alerts_classify_and_dedupe():
    r = history.record_run(run(status="reauth_required", excerpt=None, error="Session expired"))
    history.alerts_for_result(r, "Checklist", "Portal")
    history.alerts_for_result(r, "Checklist", "Portal")
    open_ = history.open_alerts()
    assert len(open_) == 1 and open_[0]["kind"] == "reauth"
    history.dismiss_key("reauth:s")
    assert history.open_alerts() == []


def test_notifications_are_off_by_default(monkeypatch):
    called = []
    monkeypatch.setattr(history.urllib.request, "urlopen", lambda *a, **k: called.append(a))
    history.add_alert("change", "t")
    assert called == []


def test_notifications_require_https(monkeypatch):
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "http://example.com/hook")
    cfg = history.notify_config()
    assert not cfg["enabled"] and cfg["error"]


def test_notification_payload_formats(monkeypatch):
    sent = []

    class Resp:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout):
        sent.append((req.full_url, req.data, req.get_header("Content-type")))
        return Resp()

    monkeypatch.setattr(history.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks.example.com/x")
    monkeypatch.setenv("NOTIFY_FORMAT", "discord")
    monkeypatch.setenv("NOTIFY_ON", "change")
    history.add_alert("change", "Page changed", "2 lines")
    history.add_alert("error", "ignored kind")
    assert len(sent) == 1
    assert json.loads(sent[0][1])["content"].startswith("Page changed")
