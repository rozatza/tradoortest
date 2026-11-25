"""Host, Origin, and header-token checks for kill, flatten, and submit.

The local-user seam is not this token. A missing token, a missing
Origin, or a host outside the loopback allowlist is rejected. The token
is not logged.
"""

from __future__ import annotations

import os
import secrets
from urllib.parse import urlparse

from fastapi import Request

_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_CLIENTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ControlRejected(Exception):
    """The control route refused the caller. The code is not the token."""

    def __init__(self, code: str = "control_forbidden") -> None:
        self.code = code
        super().__init__(code)


def require_control(request: Request) -> None:
    """Fail closed unless host, origin, client, and token all match."""
    if not host_allowed(request.headers.get("host", "")):
        raise ControlRejected()
    if not origin_allowed(request.headers.get("origin")):
        raise ControlRejected()
    client = "" if request.client is None else request.client.host
    if client not in _CLIENTS:
        raise ControlRejected()
    expected = os.environ.get("LIVE_CONTROL_TOKEN", "")
    provided = request.headers.get("x-live-token", "")
    if expected == "" or provided == "" or not _same(provided, expected):
        raise ControlRejected()


def host_allowed(value: str) -> bool:
    text = value.strip().lower()
    if text.startswith("[::1]"):
        rest = text[len("[::1]") :]
        return rest == "" or _port(rest)
    name, sep, port = text.rpartition(":")
    if sep and _port(":" + port):
        text = name
    return text in _HOSTS


def origin_allowed(value: str | None) -> bool:
    if value is None or value == "":
        return False
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.path not in ("", "/"):
        return False
    host = parsed.hostname
    return host in _HOSTS


def _port(text: str) -> bool:
    if not text.startswith(":"):
        return False
    number = text[1:]
    return number.isdigit() and 0 < int(number) < 65536


def _same(provided: str, expected: str) -> bool:
    try:
        return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
    except UnicodeError:
        return False
