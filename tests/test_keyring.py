"""Optional OS keyring storage for saved passwords (CREDENTIAL_STORE=keyring)."""

import pytest

import scraper

pytest.importorskip("keyring")


def file_text():
    return scraper.credentials_file().read_text(encoding="utf-8")


def test_file_mode_is_default_and_plain(memory_keyring):
    scraper.save_credentials("state-u", "student", "pw-file")
    assert 'password = "pw-file"' in file_text()
    assert memory_keyring.store == {}


def test_keyring_mode_keeps_passwords_out_of_file(monkeypatch, memory_keyring):
    monkeypatch.setenv("CREDENTIAL_STORE", "keyring")
    scraper.save_credentials("state-u", "student", "pw-site")
    scraper.save_imap_settings({"host": "imap.example.com", "port": 993, "username": "me@example.com",
                                "password": "pw-imap", "folder": "INBOX", "days": 30})
    text = file_text()
    assert "pw-site" not in text and "pw-imap" not in text and 'password_store = "keyring"' in text
    assert scraper.get_credentials({"id": "state-u"}) == ("student", "pw-site")
    assert scraper.load_imap_settings()["password"] == "pw-imap"
    assert memory_keyring.store[("college-tracker", "site:state-u")] == "pw-site"

    scraper.delete_credentials("state-u")
    assert ("college-tracker", "site:state-u") not in memory_keyring.store
    scraper.save_imap_settings(None)
    assert memory_keyring.store == {}


def test_migrate_both_directions(monkeypatch, memory_keyring):
    scraper.save_credentials("a", "u", "secret-a")
    monkeypatch.setenv("CREDENTIAL_STORE", "keyring")
    out = scraper.migrate_credentials()
    assert out["store"] == "keyring" and out["sites"] == 1
    assert "secret-a" not in file_text()
    assert scraper.load_credentials()["a"]["password"] == "secret-a"

    monkeypatch.setenv("CREDENTIAL_STORE", "file")
    scraper.migrate_credentials()
    assert 'password = "secret-a"' in file_text()
    assert memory_keyring.store == {}


def test_falls_back_to_file_when_vault_fails(monkeypatch, memory_keyring):
    monkeypatch.setenv("CREDENTIAL_STORE", "keyring")

    def boom(*a):
        raise RuntimeError("locked")
    monkeypatch.setattr(memory_keyring, "set_password", boom)
    scraper.save_credentials("a", "u", "kept")
    assert 'password = "kept"' in file_text()
    assert scraper.load_credentials()["a"]["password"] == "kept"


def test_unreadable_file_is_never_overwritten():
    from security import ConfigError
    path = scraper.credentials_file()
    path.write_text("[sites\nbroken = ", encoding="utf-8")
    with pytest.raises(ConfigError, match="left untouched"):
        scraper.save_credentials("a", "u", "p")
    assert path.read_text(encoding="utf-8") == "[sites\nbroken = "


def test_migrate_with_nothing_saved_creates_no_file():
    assert scraper.migrate_credentials()["sites"] == 0
    assert not scraper.credentials_file().exists()


def test_broken_credentials_file_shows_error_not_500(client, token):
    scraper.save_sites([{"id": "s", "name": "S", "auth": "credentials", "login_url": "https://p.example.edu/login",
                         "checks": [{"id": "c", "name": "C", "url": "https://p.example.edu/c"}]}])
    scraper.credentials_file().write_text("[sites\n", encoding="utf-8")
    r = client.post("/sites/s/credentials", data={"csrf_token": token, "username": "u", "password": "p", "next": "/sites"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/sites")
    assert "left untouched" in client.get("/sites").get_data(as_text=True)
