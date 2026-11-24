import pytest

from security import (
    ConfigError,
    bind_host_ok,
    host_header_ok,
    redact,
    request_allowed,
    safe_next,
    strip_sensitive_query,
    validate_link,
    validate_target_url,
)


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///etc/passwd", "ftp://example.edu/x", "data:text/html,hi",
    "http://127.0.0.1/login", "https://localhost/x", "https://10.0.0.5/", "https://192.168.1.1/",
    "https://169.254.169.254/latest/meta-data", "https://[::1]/", "https://router.local/",
    "http://portal.example.edu/login",  # plain http to a public host
    "https://user:pw@portal.example.edu/",
])
def test_target_url_rejects_unsafe(url):
    with pytest.raises(ConfigError):
        validate_target_url(url)


def test_target_url_accepts_public_https():
    assert validate_target_url("https://portal.example.edu/login") == "https://portal.example.edu/login"


def test_local_targets_need_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_TARGETS", "1")
    assert validate_target_url("http://127.0.0.1:8999/login")
    with pytest.raises(ConfigError):
        validate_target_url("file:///etc/passwd")


def test_request_guard_blocks_redirect_targets():
    assert not request_allowed("http://127.0.0.1:5000/sites/delete/x")
    assert not request_allowed("https://169.254.169.254/")
    assert request_allowed("data:image/png;base64,AAAA")
    assert request_allowed("https://93.184.215.14/")  # public IP literal


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "vbscript:x", "//evil.com", "notaurl"])
def test_display_links_must_be_http(url):
    with pytest.raises(ConfigError):
        validate_link(url, required=True)


def test_display_links_allow_any_http_host():
    assert validate_link("https://maps.example.com/?q=campus")
    assert validate_link("") == ""


def test_redact_hides_secrets():
    out = redact("login failed for student with s3cret! password=hunter2", ["s3cret!", "student"])
    assert "s3cret!" not in out and "student" not in out and "hunter2" not in out


def test_strip_sensitive_query():
    out = strip_sensitive_query("https://x.edu/cb?code=abc&page=2&session_id=zzz")
    assert "abc" not in out and "zzz" not in out and "page=2" in out


@pytest.mark.parametrize("host,ok", [
    ("localhost:5000", True), ("127.0.0.1:5000", True), ("[::1]:5000", True), ("127.0.0.1", True),
    ("evil.com", False), ("evil.com:5000", False), ("localhost.evil.com", False), ("192.168.1.10:5000", False),
])
def test_host_header(host, ok):
    assert host_header_ok(host) is ok


def test_bind_host(monkeypatch):
    assert bind_host_ok("127.0.0.1")
    assert not bind_host_ok("0.0.0.0")
    monkeypatch.setenv("ALLOW_REMOTE", "1")
    assert bind_host_ok("0.0.0.0")


@pytest.mark.parametrize("target,expected", [
    ("/tasks", "/tasks"), ("https://evil.com", "/"), ("//evil.com", "/"), ("/\\evil.com", "/"), (None, "/"),
])
def test_safe_next(target, expected):
    assert safe_next(target) == expected
