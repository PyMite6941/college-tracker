"""Security boundaries for a local, single-user app.

Trust model (see SECURITY.md):

* The web UI is for the person sitting at this computer. It binds to a
  loopback address, rejects requests whose Host header is not a loopback
  name (DNS-rebinding protection), and requires a CSRF token on every
  state-changing request.
* Sites and checks are configured by that user, for accounts they are
  authorised to use. Configured URLs must be http(s), and by default must
  not point at loopback, private, link-local or otherwise internal network
  addresses. ``ALLOW_LOCAL_TARGETS=1`` lifts that for local testing.
* Secrets (passwords, session state) never go into results, history,
  alerts, logs or Git. ``redact()`` scrubs known secrets out of any error
  text before it is stored or shown.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import re
import secrets
import socket
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


class ConfigError(ValueError):
    """A user-supplied configuration value is invalid."""


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def allow_local_targets() -> bool:
    return env_flag("ALLOW_LOCAL_TARGETS")


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def _ip_is_internal(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


@lru_cache(maxsize=512)
def _resolve(host: str) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return ()
    return tuple(sorted({info[4][0] for info in infos}))


def host_is_internal(host: str, resolve_dns: bool = False) -> bool:
    """True if ``host`` is loopback/private/link-local (or resolves to one)."""
    h = (host or "").strip("[]").lower().rstrip(".")
    if not h:
        return True
    if h in {"localhost"} or h.endswith(".localhost") or h.endswith(".local") \
            or h.endswith(".internal") or h.endswith(".lan"):
        return True
    try:
        return _ip_is_internal(ipaddress.ip_address(h))
    except ValueError:
        pass
    if resolve_dns:
        for addr in _resolve(h):
            try:
                if _ip_is_internal(ipaddress.ip_address(addr.split("%")[0])):
                    return True
            except ValueError:
                continue
    return False


def validate_target_url(url: str, field: str = "URL", required: bool = True,
                        resolve_dns: bool = False) -> str:
    """Validate a URL the scraper will visit. Returns the cleaned URL.

    Rejects non-http(s) schemes, embedded credentials and (unless
    ALLOW_LOCAL_TARGETS is set) internal network hosts.
    """
    url = (url or "").strip()
    if not url:
        if required:
            raise ConfigError(f"{field} is required.")
        return ""
    if len(url) > 2048:
        raise ConfigError(f"{field} is too long.")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ConfigError(f"{field} must start with https:// (or http://).")
    if not parts.hostname:
        raise ConfigError(f"{field} has no host name.")
    if parts.username or parts.password:
        raise ConfigError(f"{field} must not contain a username or password; "
                          "save credentials on the Sites page instead.")
    if not allow_local_targets():
        if host_is_internal(parts.hostname, resolve_dns=resolve_dns):
            raise ConfigError(
                f"{field} points at a local or private network address "
                f"({parts.hostname}). Set ALLOW_LOCAL_TARGETS=1 in .env only if "
                "you really mean to check a page on your own network."
            )
        if parts.scheme != "https":
            raise ConfigError(f"{field} must use https://.")
    return url


def validate_link(url: str, field: str = "Link", required: bool = False) -> str:
    """Validate a user-supplied link that is only displayed (never fetched).

    Only http(s) links are allowed so the UI can never render a
    ``javascript:`` or ``data:`` URL.
    """
    url = (url or "").strip()
    if not url:
        if required:
            raise ConfigError(f"{field} is required.")
        return ""
    if len(url) > 2048:
        raise ConfigError(f"{field} is too long.")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError(f"{field} must be a full http(s):// link.")
    if parts.username or parts.password:
        raise ConfigError(f"{field} must not contain a username or password.")
    return url


def request_allowed(url: str) -> bool:
    """Used by the Playwright route guard for every request and redirect."""
    parts = urlsplit(url)
    if parts.scheme in {"data", "blob", "about"}:
        return True
    if parts.scheme not in {"http", "https"}:
        return False
    if allow_local_targets():
        return True
    return not host_is_internal(parts.hostname or "", resolve_dns=True)


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

SENSITIVE_QUERY_KEYS = re.compile(
    r"(pass|pwd|token|secret|session|auth|code|key|sig|ticket|sid)", re.I)


def strip_sensitive_query(url: str) -> str:
    """Remove query parameters that look like tokens from a URL for display."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    kept = [(k, v if not SENSITIVE_QUERY_KEYS.search(k) else "REDACTED")
            for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def redact(text: str | None, secrets_to_hide: list[str] | tuple[str, ...] = ()) -> str | None:
    if text is None:
        return None
    out = str(text)
    for s in secrets_to_hide:
        if s and len(s) >= 3:
            out = out.replace(s, "[redacted]")
    out = re.sub(r"(?i)(password|passwd|pwd|token|secret)(\s*[=:]\s*)\S+", r"\1\2[redacted]", out)
    return out


# ---------------------------------------------------------------------------
# Request guards for the Flask app
# ---------------------------------------------------------------------------

def host_header_ok(host_header: str, extra_hosts: set[str] | None = None) -> bool:
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        name = host.split("]")[0] + "]"
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    allowed = set(LOOPBACK_HOSTS) | (extra_hosts or set())
    return name in allowed


def bind_host_ok(host: str) -> bool:
    return (host or "").strip().lower() in LOOPBACK_HOSTS or env_flag("ALLOW_REMOTE")


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_ok(expected: str | None, supplied: str | None) -> bool:
    if not expected or not supplied:
        return False
    return hmac.compare_digest(expected, supplied)


def safe_next(target: str | None, default: str = "/") -> str:
    """Only allow same-site relative redirects (no open redirects)."""
    t = (target or "").strip()
    if not t.startswith("/") or t.startswith("//") or t.startswith("/\\"):
        return default
    return t
