"""Venue failures.

The message is a short code or a status. It does not include a response
body, a request URL, a header, or an API key.
"""

from __future__ import annotations


class VenueError(Exception):
    """The venue could not quote or build."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class VenueSchemaError(VenueError):
    """The response is not the pinned shape."""


class VenueRejected(VenueError):
    """The quote or the built transaction disagrees with what was asked."""


class VenueExpired(VenueError):
    """The quote is past the venue expiry or the 24-hour limit."""


class VenueTransportError(VenueError):
    """The HTTP call failed. The status is ours. The body is not."""


class VenueExecuteDisabled(VenueError):
    """Signing and sending are not this package."""
