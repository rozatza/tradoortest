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


class IngestExpired(StoreError):
    """A stored ingest result reached the 24-hour TTL and was deleted."""


class IngestError(SourceError):
    """A coded ingest failure. ``status`` is the HTTP status for the API."""

    code = "source_error"
    status = 422


class InvalidMint(IngestError):
    """The mint is not a Solana address. Raised before any provider request."""

    code = "invalid_mint"
    status = 400


class NoPool(IngestError):
    """The mint has no DEX pool on the primary provider."""

    code = "no_pool"
    status = 404


class NoDexPoolYet(IngestError):
    """The mint is still on a bonding curve, so there is no DEX pool yet."""

    code = "no_dex_pool_yet"
    status = 422


class RateLimitExhausted(IngestError):
    """Retries or the configured call cap ran out."""

    code = "rate_limit"
    status = 429


class ComputeBudgetExceeded(IngestError):
    """The Birdeye compute-unit budget cannot pay for the next request."""

    code = "cu_budget"
    status = 429
