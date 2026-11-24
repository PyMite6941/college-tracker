"""One-at-a-time background jobs (check runs and visible sign-ins).

Playwright's sync API is used from a worker thread so the web UI stays
responsive. Only one job runs at a time; the dashboard polls /jobs/status.
"""

from __future__ import annotations

import threading
import traceback
from datetime import datetime, timezone

from security import redact

_lock = threading.Lock()
_state: dict = {"running": False, "kind": None, "label": None, "message": "",
                "started_at": None, "finished_at": None, "error": None, "result": None,
                "site_id": None}
_stop = threading.Event()


def status() -> dict:
    with _lock:
        return dict(_state)


def is_running() -> bool:
    with _lock:
        return bool(_state["running"])


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def start(kind: str, label: str, target, *args, site_id: str | None = None, **kwargs) -> bool:
    """Start ``target(*args, progress=..., **kwargs)`` in the background.

    Returns False if another job is already running.
    """
    with _lock:
        if _state["running"]:
            return False
        _stop.clear()
        _state.update(running=True, kind=kind, label=label, message="Starting…",
                      started_at=datetime.now(timezone.utc).isoformat(), finished_at=None,
                      error=None, result=None, site_id=site_id)

    def runner():
        try:
            result = target(*args, progress=lambda m: _set(message=m), **kwargs)
            _set(result=_summarise(result), message="Finished.")
        except Exception as exc:
            traceback.print_exc()
            _set(error=redact(str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__),
                 message="Failed.")
        finally:
            _set(running=False, finished_at=datetime.now(timezone.utc).isoformat())

    threading.Thread(target=runner, name=f"job-{kind}", daemon=True).start()
    return True


def stop_event() -> threading.Event:
    return _stop


def request_stop() -> None:
    _stop.set()


def _summarise(result):
    if isinstance(result, list):
        counts: dict[str, int] = {}
        for r in result:
            counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
        return {"checks": len(result), "by_status": counts}
    if isinstance(result, dict):
        return result
    return None
