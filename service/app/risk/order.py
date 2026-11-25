"""Build a live quote and an unsigned transaction. Does not sign or send."""

from __future__ import annotations

from app.risk.limits import DEFAULT_LIMITS, RiskLimits, build_slippage_bps, order_slippage_bps
from app.risk.rpc import ChainRpc, chain_block_height
from app.venue.types import QuoteRequest, UnsignedTransaction, VenueQuote


def quote_and_build(
    venue: object,
    *,
    input_mint: str,
    output_mint: str,
    amount: int,
    side: str,
    taker: str,
    now: int,
    rpc: ChainRpc,
    limits: RiskLimits | None = None,
) -> tuple[VenueQuote, UnsignedTransaction]:
    """Send the risk ``slippageBps`` on both ``/order`` and ``/build``.

    The block height comes from ``rpc``, not from the quote.
    """
    chosen = DEFAULT_LIMITS if limits is None else limits
    slippage = order_slippage_bps(chosen)
    if build_slippage_bps(chosen) != slippage:
        raise RuntimeError("slippage_param")
    request = QuoteRequest(input_mint, output_mint, amount, side, slippage)
    quote = venue.quote(request, now=now)  # type: ignore[attr-defined]
    height = chain_block_height(rpc)
    built = venue.build(  # type: ignore[attr-defined]
        quote,
        taker=taker,
        now=now,
        block_height=height,
        slippage_bps=slippage,
    )
    return quote, built
