"""Checks shared by the mock and the Jupiter client.

A mismatch is a reject. The size is not rewritten and the quote is not
replaced with the venue's new numbers.
"""

from __future__ import annotations

from app.venue.address import require_address
from app.venue.errors import VenueError, VenueExpired, VenueRejected
from app.venue.types import QuoteRequest, VenueQuote, quote_is_within_ttl


def require_clock(now: object) -> int:
    """Unix seconds. A bool is not a clock."""
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise VenueError("bad_clock")
    return now


def require_block_height(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VenueError("bad_block_height")
    return value


def require_request(request: QuoteRequest) -> None:
    """Long-only spot, exact-in. The caller picks the mints."""
    if not isinstance(request, QuoteRequest):
        raise VenueError("bad_request")
    if request.side not in ("buy", "sell"):
        raise VenueRejected("bad_side")
    require_address(request.input_mint)
    require_address(request.output_mint)
    if request.input_mint == request.output_mint:
        raise VenueRejected("mint_mismatch")
    if isinstance(request.amount, bool) or not isinstance(request.amount, int):
        raise VenueRejected("amount_mismatch")
    if request.amount <= 0 or request.amount >= 2**64:
        raise VenueRejected("amount_mismatch")


def require_fresh(quote: VenueQuote, now: int, block_height: int | None) -> None:
    """Refuse a quote the 24-hour purge would already have dropped.

    A block-height expiry cannot be judged without the height the caller
    passes in. Missing that height is a reject, not a guess.
    """
    if not isinstance(quote, VenueQuote):
        raise VenueError("bad_quote")
    if (
        isinstance(quote.quoted_at, bool)
        or not isinstance(quote.quoted_at, int)
        or quote.quoted_at > now
        or not quote_is_within_ttl(quote.quoted_at, now)
    ):
        raise VenueExpired("quote_expired")
    height = quote.expiry.last_valid_block_height
    stamp = quote.expiry.expire_at_unix
    if height is None and stamp is None:
        raise VenueExpired("missing_expiry")
    if stamp is not None and now >= stamp:
        raise VenueExpired("quote_expired")
    if height is not None:
        if block_height is None:
            raise VenueExpired("missing_block_height")
        current = require_block_height(block_height)
        if current > height:
            raise VenueExpired("quote_expired")


def require_build_matches(
    quote: VenueQuote,
    *,
    input_mint: str,
    output_mint: str,
    in_amount: int,
    out_amount: int,
    minimum_out_amount: int,
) -> None:
    """The built amounts and mints are the ones the quote already named."""
    if input_mint != quote.input_mint or output_mint != quote.output_mint:
        raise VenueRejected("mint_mismatch")
    if (
        in_amount != quote.in_amount
        or out_amount != quote.out_amount
        or minimum_out_amount != quote.minimum_out_amount
    ):
        raise VenueRejected("amount_mismatch")
