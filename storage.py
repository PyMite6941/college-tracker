"""Local JSON storage helpers.

Everything the app writes at runtime lives under ``data/`` (gitignored):

    data/results.json        latest result per site/check
    data/history/<site>/<check>.jsonl   timestamped check history
    data/alerts.json         in-app alerts (changes, reauth, reminders)
    data/planner.json        colleges, visits, tasks
    data/auth/<site>.json    Playwright storage state for a signed-in site
    data/.secret_key         random Flask secret for this install

Writes are atomic (temp file + replace) so a crash never leaves half a file.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("COLLEGE_TRACKER_DATA_DIR") or (BASE_DIR / "data"))


def data_dir() -> Path:
    """Return the data directory (re-read from env so tests can redirect it)."""
    d = Path(os.getenv("COLLEGE_TRACKER_DATA_DIR") or DATA_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def restrict_permissions(path: Path) -> None:
    """Make a file owner-read/write only where the OS supports it."""
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2, ensure_ascii=False)
        if private:
            restrict_permissions(Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path: Path, text: str, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if private:
            restrict_permissions(Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def is_slug(value: str) -> bool:
    return bool(SLUG_RE.match(value or ""))


def slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return s[:64] or "item"


def new_id(prefix: str = "") -> str:
    return f"{prefix}{secrets.token_hex(4)}"


def secret_key() -> str:
    """Return FLASK_SECRET_KEY, or a random key persisted for this install."""
    env = os.getenv("FLASK_SECRET_KEY", "").strip()
    if env and env != "dev-secret-change-me":
        return env
    path = data_dir() / ".secret_key"
    try:
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    write_text(path, key, private=True)
    return key
