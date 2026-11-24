import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point every runtime file at a throwaway directory *before* importing the app.
_SESSION_TMP = tempfile.mkdtemp(prefix="ct-test-")
os.environ["COLLEGE_TRACKER_DATA_DIR"] = os.path.join(_SESSION_TMP, "data")
os.environ["COLLEGE_TRACKER_SITES_FILE"] = os.path.join(_SESSION_TMP, "sites.json")
os.environ["COLLEGE_TRACKER_CREDENTIALS_FILE"] = os.path.join(_SESSION_TMP, "credentials.toml")
for key in ("NOTIFY_WEBHOOK_URL", "CHROME_USER_DATA_DIR", "CHROME_PROFILE", "ALLOW_LOCAL_TARGETS",
            "ALLOW_REMOTE", "HISTORY_MAX_RUNS", "HISTORY_MAX_DAYS"):
    os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Fresh data dir, sites.json and credentials per test."""
    monkeypatch.setenv("COLLEGE_TRACKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COLLEGE_TRACKER_SITES_FILE", str(tmp_path / "sites.json"))
    monkeypatch.setenv("COLLEGE_TRACKER_CREDENTIALS_FILE", str(tmp_path / "credentials.toml"))
    for key in ("NOTIFY_WEBHOOK_URL", "CHROME_USER_DATA_DIR", "CHROME_PROFILE", "ALLOW_LOCAL_TARGETS"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "sites.json").write_text("[]", encoding="utf-8")
    yield tmp_path


@pytest.fixture
def app():
    import app as app_module
    a = app_module.create_app(testing=True)
    return a


@pytest.fixture
def client(app):
    c = app.test_client()
    return c


def csrf(client):
    """Load a page so the session has a CSRF token, and return it."""
    client.get("/")
    with client.session_transaction() as s:
        return s["csrf_token"]


@pytest.fixture
def token(client):
    return csrf(client)


try:
    from keyring.backend import KeyringBackend as _Base
except ImportError:  # keyring is optional
    _Base = object


class MemoryKeyring(_Base):
    """In-memory keyring so tests never touch the real OS credential vault."""
    priority = 1

    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, password):
        self.store[(service, name)] = password

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch):
    kr = MemoryKeyring()
    monkeypatch.delenv("CREDENTIAL_STORE", raising=False)
    try:
        import keyring
        import keyring.core
    except ImportError:
        yield kr
        return
    previous = keyring.get_keyring()
    keyring.core.set_keyring(kr)
    yield kr
    keyring.core.set_keyring(previous)
