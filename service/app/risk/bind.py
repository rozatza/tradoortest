"""The runtime listens on loopback only."""

from __future__ import annotations

import os

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def require_loopback(host: str | None = None) -> str:
    """Return the bind host, or raise when it is not loopback.

    Unset means ``127.0.0.1``. ``0.0.0.0`` and every other name are refused.
    """
    if host is None:
        host = os.environ.get("HOST", "127.0.0.1")
    text = host.strip()
    if text not in _LOOPBACK:
        raise RuntimeError("runtime must bind to loopback")
    return text
