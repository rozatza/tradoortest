"""Quote and unsigned-transaction types.

These are the only fields a later store may keep. There is no raw
provider body on any of them. A quote record ages out at 24 hours,
the same cutoff the bar cache uses (``stored_at <= now - 86400``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.venue.pin import QUOTE_TTL_SECONDS


@dataclass(frozen=True, slots=True)
class QuoteRequest:
    """One exact-in spot quote. ``side`` is ``buy`` or ``sell``.

    ``slippage_bps`` is sent on the quote when it is set. It is a request
    parameter. The risk engine does not read it here.
    """

    input_mint: str
    output_mint: str
    amount: int
    side: str
    slippage_bps: int | None = None


@dataclass(frozen=True, slots=True)
class VenueExpiry:
    """Aggregator routes carry a block height. RFQ routes carry a timestamp."""

    last_valid_block_height: int | None
    expire_at_unix: int | None


@dataclass(frozen=True, slots=True)
class VenueQuote:
    """A parsed quote. ``quoted_at`` is the caller's clock, in unix seconds."""

    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    minimum_out_amount: int
    price_impact: str
    route_label: str
    router: str
    expiry: VenueExpiry
    request_id: str
    quoted_at: int
    side: str
    slippage_bps: int | None
    fee_bps: int | None
    fee_mint: str | None
    fee_amount: int | None
    swap_mode: str
    network_fee_lamports: int | None = None
    priority_fee_lamports: int | None = None
    rent_fee_lamports: int | None = None

    def as_record(self) -> dict[str, object]:
        """Fields a store may persist. No provider body and no transaction."""
        return {
            "input_mint": self.input_mint,
            "output_mint": self.output_mint,
            "in_amount": self.in_amount,
            "out_amount": self.out_amount,
            "minimum_out_amount": self.minimum_out_amount,
            "price_impact": self.price_impact,
            "route_label": self.route_label,
            "router": self.router,
            "last_valid_block_height": self.expiry.last_valid_block_height,
            "expire_at_unix": self.expiry.expire_at_unix,
            "request_id": self.request_id,
            "quoted_at": self.quoted_at,
            "side": self.side,
            "slippage_bps": self.slippage_bps,
            "fee_bps": self.fee_bps,
            "fee_mint": self.fee_mint,
            "fee_amount": self.fee_amount,
            "swap_mode": self.swap_mode,
            "network_fee_lamports": self.network_fee_lamports,
            "priority_fee_lamports": self.priority_fee_lamports,
            "rent_fee_lamports": self.rent_fee_lamports,
        }


@dataclass(frozen=True, slots=True)
class AccountRole:
    """One account, after lookup tables are resolved."""

    pubkey: str
    signer: bool
    writable: bool


@dataclass(frozen=True, slots=True)
class DecodedInstruction:
    """One instruction the pre-sign guard can read without a chain."""

    program_id: str
    accounts: tuple[AccountRole, ...]
    data: bytes


@dataclass(frozen=True, slots=True)
class ResolvedLookupTable:
    """A lookup table whose addresses were applied to the message."""

    address: str
    addresses: tuple[str, ...]
    writable_indexes: tuple[int, ...]
    readonly_indexes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class UnsignedTransaction:
    """Version-0 bytes that have not been signed, plus the resolved view.

    ``account_keys`` is the full list: static keys, then writable loaded
    addresses, then readonly loaded addresses. ``fee_payer`` is
    ``account_keys[0]``.
    """

    transaction: bytes
    instructions: tuple[DecodedInstruction, ...]
    fee_payer: str
    taker: str
    account_keys: tuple[str, ...]
    static_account_count: int
    lookup_tables: tuple[ResolvedLookupTable, ...]
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    minimum_out_amount: int
    last_valid_block_height: int
    transaction_version: int


@runtime_checkable
class ExecutionVenue(Protocol):
    """The only surface that talks to a swap venue.

    ``quote`` does not return a signable transaction. ``build`` returns
    an unsigned one. ``execute`` does not send.
    """

    def quote(self, request: QuoteRequest, *, now: int) -> VenueQuote:
        """Fetch a quote. ``now`` is the caller's unix clock."""

    def build(
        self,
        quote: VenueQuote,
        *,
        taker: str,
        now: int,
        block_height: int | None = None,
        slippage_bps: int | None = None,
    ) -> UnsignedTransaction:
        """Build an unsigned transaction for a quote the caller accepted.

        ``slippage_bps`` is the same request parameter as on the quote.
        """

    def execute(self, transaction: object = None) -> None:
        """Send is not implemented."""


def quote_is_within_ttl(quoted_at: int, now: int) -> bool:
    """True while the quote is strictly younger than 24 hours."""
    return now - quoted_at < QUOTE_TTL_SECONDS


def purge_quote_records(
    records: Sequence[Mapping[str, object]], now: int
) -> tuple[dict[str, object], ...]:
    """Drop records at the 24-hour mark, and drop any that is not a quote record.

    A row that carries a provider body is dropped. The kept rows are copies
    of the allowlisted fields only.
    """
    kept: list[dict[str, object]] = []
    allowed = _RECORD_KEYS
    for record in records:
        if set(record) - allowed:
            continue
        if set(record) != allowed:
            continue
        quoted_at = record.get("quoted_at")
        if isinstance(quoted_at, bool) or not isinstance(quoted_at, int):
            continue
        if not quote_is_within_ttl(quoted_at, now):
            continue
        kept.append({key: record[key] for key in _RECORD_KEY_ORDER})
    return tuple(kept)


_RECORD_KEY_ORDER = (
    "input_mint",
    "output_mint",
    "in_amount",
    "out_amount",
    "minimum_out_amount",
    "price_impact",
    "route_label",
    "router",
    "last_valid_block_height",
    "expire_at_unix",
    "request_id",
    "quoted_at",
    "side",
    "slippage_bps",
    "fee_bps",
    "fee_mint",
    "fee_amount",
    "swap_mode",
    "network_fee_lamports",
    "priority_fee_lamports",
    "rent_fee_lamports",
)
_RECORD_KEYS = frozenset(_RECORD_KEY_ORDER)
