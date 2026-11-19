"""Errors from historical bar ingest.

Messages name the failure. They do not include provider response headers,
tokens, or response bodies.
"""

from __future__ import annotations


class SourceError(RuntimeError):
    """The bar source could not return a page."""


class UnknownMint(SourceError):
    """This source has no pool for that mint.

    Another source can accept the same ``fetch_bars`` call. Callers do not
    change.
    """


class UnknownSymbol(SourceError):
    """The CLI symbol is not in the SOL majors registry."""


class StoreError(RuntimeError):
    """The local bar store refused a write."""
