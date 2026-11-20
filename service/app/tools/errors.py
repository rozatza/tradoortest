"""Structured tool failures.

Messages are short and fixed. They do not include a traceback, a filesystem
path, SQL text, or a secret.
"""

from __future__ import annotations

import re
from typing import Any

_LEAK = re.compile(
    r"(traceback|sqlite|operationalerror|\.py\b|/tmp/|/home/|\\\\|"
    r"file \"|\.env|api[_-]?key|secret|\.sqlite|\b[A-Z][A-Za-z]+Error\b)",
    re.IGNORECASE,
)

_HTTP = {
    "unknown_tool": 404,
    "not_found": 404,
    "strategy_archived": 404,
    "version_conflict": 409,
    "no_changes": 409,
    "conflict": 409,
    "expired": 410,
    "payload_too_large": 413,
    "rate_limited": 429,
    "limit_reached": 429,
    "service_unavailable": 503,
    "internal_error": 500,
    "forbidden": 403,
}


class ToolError(Exception):
    """A failure the dispatcher turns into an ``ok: false`` envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.path = path
        self.retryable = retryable
        self.details = details
        super().__init__(code)


def http_status(code: str) -> int:
    """HTTP status for a tool code. Schema and domain failures are 422."""
    return _HTTP.get(code, 422)


def safe_text(text: object, fallback: str, *, limit: int = 200) -> str:
    """Return ``text`` when it cannot leak a path, SQL line, or secret."""
    flat = " ".join(str(text).split())
    if flat == "" or len(flat) > limit or "\x00" in flat or _LEAK.search(flat):
        return fallback
    return flat


def success_envelope(tool: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "tool": tool, "data": data}


def error_envelope(
    tool: str,
    code: str,
    message: str,
    *,
    path: str | None,
    retryable: bool,
    details: dict[str, Any] | list[Any] | None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "tool": tool,
        "error": {
            "code": code,
            "message": message,
            "path": path,
            "retryable": retryable,
            "details": details,
        },
    }
