"""In-memory venue for tests and for callers that must not touch the network.

The quote is the configured book. The build compiles the configured
instructions with the same unsigned-transaction codec the Jupiter client
uses, including lookup tables. ``execute`` raises.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.venue.address import require_address
from app.venue.checks import (
    require_block_height,
    require_clock,
    require_fresh,
    require_request,
    require_slippage,
)
from app.venue.errors import (
    VenueExecuteDisabled,
    VenueExpired,
    VenueRejected,
    VenueSchemaError,
)
from app.venue.tx import assemble_unsigned
from app.venue.types import (
    DecodedInstruction,
    QuoteRequest,
    UnsignedTransaction,
    VenueExpiry,
    VenueQuote,
)


class MockVenue:
    """A scripted ``ExecutionVenue``. Amounts that disagree are rejected."""

    def __init__(
        self,
        *,
        input_mint: str,
        output_mint: str,
        in_amount: int,
        out_amount: int,
        minimum_out_amount: int,
        price_impact: str,
        route_label: str,
        router: str,
        last_valid_block_height: int,
        instructions: Sequence[DecodedInstruction],
        lookup_tables: Sequence[tuple[str, tuple[str, ...]]],
        recent_blockhash: bytes,
        request_id: str = "mock-request",
        slippage_bps: int | None = None,
        fee_bps: int | None = None,
        fee_mint: str | None = None,
        fee_amount: int | None = None,
        network_fee_lamports: int | None = None,
        priority_fee_lamports: int | None = None,
        rent_fee_lamports: int | None = None,
    ) -> None:
        if not route_label:
            raise VenueSchemaError("missing_route")
        self._input_mint = require_address(input_mint)
        self._output_mint = require_address(output_mint)
        if self._input_mint == self._output_mint:
            raise VenueRejected("mint_mismatch")
        if (
            isinstance(in_amount, bool)
            or not isinstance(in_amount, int)
            or in_amount <= 0
            or out_amount <= 0
            or minimum_out_amount <= 0
            or minimum_out_amount > out_amount
        ):
            raise VenueRejected("amount_mismatch")
        self._in_amount = in_amount
        self._out_amount = out_amount
        self._minimum = minimum_out_amount
        self._price_impact = price_impact
        self._route_label = route_label
        self._router = router
        self._height = require_block_height(last_valid_block_height)
        if self._height < 1:
            raise VenueSchemaError("bad_block_height")
        self._instructions = tuple(instructions)
        self._tables = tuple((address, tuple(contents)) for address, contents in lookup_tables)
        if not isinstance(recent_blockhash, bytes) or len(recent_blockhash) != 32:
            raise VenueSchemaError("bad_blockhash")
        self._blockhash = recent_blockhash
        self._request_id = request_id
        self._slippage_bps = slippage_bps
        self._fee_bps = fee_bps
        self._fee_mint = fee_mint
        self._fee_amount = fee_amount
        self._network_fee = network_fee_lamports
        self._priority_fee = priority_fee_lamports
        self._rent_fee = rent_fee_lamports
        self.last_quote_slippage: int | None = None
        self.last_build_slippage: int | None = None

    def quote(self, request: QuoteRequest, *, now: int) -> VenueQuote:
        now = require_clock(now)
        require_request(request)
        self.last_quote_slippage = request.slippage_bps
        if request.input_mint != self._input_mint or request.output_mint != self._output_mint:
            raise VenueRejected("mint_mismatch")
        if request.amount != self._in_amount:
            raise VenueRejected("amount_mismatch")
        return VenueQuote(
            input_mint=request.input_mint,
            output_mint=request.output_mint,
            in_amount=request.amount,
            out_amount=self._out_amount,
            minimum_out_amount=self._minimum,
            price_impact=self._price_impact,
            route_label=self._route_label,
            router=self._router,
            expiry=VenueExpiry(self._height, None),
            request_id=self._request_id,
            quoted_at=now,
            side=request.side,
            slippage_bps=self._slippage_bps,
            fee_bps=self._fee_bps,
            fee_mint=self._fee_mint,
            fee_amount=self._fee_amount,
            swap_mode="ExactIn",
            network_fee_lamports=self._network_fee,
            priority_fee_lamports=self._priority_fee,
            rent_fee_lamports=self._rent_fee,
        )

    def build(
        self,
        quote: VenueQuote,
        *,
        taker: str,
        now: int,
        block_height: int | None = None,
        slippage_bps: int | None = None,
    ) -> UnsignedTransaction:
        now = require_clock(now)
        taker = require_address(taker)
        if slippage_bps is not None:
            require_slippage(slippage_bps)
        self.last_build_slippage = slippage_bps
        if block_height is None:
            raise VenueExpired("missing_block_height")
        require_block_height(block_height)
        require_fresh(quote, now, block_height)
        if quote.input_mint != self._input_mint or quote.output_mint != self._output_mint:
            raise VenueRejected("mint_mismatch")
        if (
            quote.in_amount != self._in_amount
            or quote.out_amount != self._out_amount
            or quote.minimum_out_amount != self._minimum
        ):
            raise VenueRejected("amount_mismatch")
        if quote.route_label != self._route_label:
            raise VenueSchemaError("missing_route")
        return assemble_unsigned(
            payer=taker,
            instructions=self._instructions,
            lookup_tables=self._tables,
            recent_blockhash=self._blockhash,
            quote=quote,
            last_valid_block_height=self._height,
        )

    def execute(self, transaction: object = None) -> None:
        raise VenueExecuteDisabled("execute_disabled")
