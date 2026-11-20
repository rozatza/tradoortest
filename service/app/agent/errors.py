"""Failures the agent HTTP routes turn into a JSON ``{error}`` envelope.

Messages are fixed sentences. They do not include a traceback, a filesystem
path, SQL text, an API key, or an upstream body.
"""

from __future__ import annotations

_HTTP = {
    "not_found": 404,
    "forbidden": 403,
    "version_conflict": 409,
    "proposal_already_decided": 409,
    "no_changes": 409,
    "payload_too_large": 413,
    "rate_limited": 429,
    "provider_not_configured": 503,
    "service_unavailable": 503,
    "internal_error": 500,
}


class AgentError(Exception):
    """A failure with a public code and a fixed message."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        status: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.path = path
        self.status = status if status is not None else _HTTP.get(code, 422)
        super().__init__(code)


class StoreUnavailable(Exception):
    """The agent database cannot be opened or used.

    ``open_agent_store`` does not assign ``app.state`` when this is raised,
    so a later request can try again.
    """

    def __init__(self) -> None:
        super().__init__("agent storage is unavailable")
