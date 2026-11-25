"""Pure live-order check.

The same inputs always accept or reject the same way. A failure rejects
the order. The size is not reduced. Flatten is an explicit sell: it skips
the entry caps and the halt flags, and it still checks slippage, impact,
fees, stale data, and divergence. The kill path does not call it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal

from app.risk.fees import DayLedger, FeeLines, add_fees, roll_day
from app.risk.limits import DEFAULT_LIMITS, RiskLimits

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HALT_CODES = frozenset({"daily_notional", "daily_loss", "daily_fee"})
# Flatten still runs the checks that are not in this set.
_FLATTEN_SKIPS = frozenset(
    {
        "trading_disabled",
        "killed",
        "dead_man",
        "daily_halt",
        "missing_mark",
        "per_trade",
        "daily_notional",
        "daily_loss",
        "daily_fee",
        "position_cap",
        "asset_open",
        "allowlist",
        "pool_liquidity",
        "pool_age",
        "freeze_authority",
        "mint_authority",
        "sell_route",
        "wallet_ceiling",
    }
)


@dataclass(frozen=True, slots=True)
class LiveOrder:
    """Facts the tick already measured. This object does not fetch them."""

    side: str
    action: str
    mint: str
    notional_usd: Decimal | None
    slippage_bps: int | None
    price_impact: str | None
    pool_liquidity_usd: Decimal | None
    pool_age_seconds: int | None
    freeze_authority_set: bool | None
    mint_authority_set: bool | None
    sell_route_ok: bool | None
    sell_route_impact: str | None
    bar_age_bars: int | None
    bar_price: Decimal | None
    quote_price: Decimal | None
    open_positions: int
    asset_already_open: bool
    fees: FeeLines | None
    wallet_value_usd: Decimal | None
    mark_ok: bool
    trading_enabled: bool
    kill_halted: bool
    dead_man_halted: bool
    out_amount: int
    minimum_out_amount: int


@dataclass(frozen=True, slots=True)
class RiskResult:
    """``resized`` is always false. ``notional_usd`` is the input, unchanged."""

    accepted: bool
    code: str
    ledger: DayLedger
    resized: bool
    notional_usd: Decimal | None


def startup_refuses_wallet(
    marked_usd: Decimal | None,
    limits: RiskLimits | None = None,
) -> str | None:
    """Shared ceiling check. ``None`` means startup may continue.

    Above the ceiling, or a missing mark, refuses. P4-06 calls this before
    the auto signer starts. A later tick uses the same rule for new buys.
    """
    chosen = DEFAULT_LIMITS if limits is None else limits
    if marked_usd is None:
        return "missing_mark"
    if marked_usd > chosen.wallet_ceiling_usd:
        return "above_ceiling"
    return None


def evaluate(
    order: LiveOrder,
    ledger: DayLedger,
    limits: RiskLimits | None = None,
    *,
    today: str,
) -> RiskResult:
    """Accept or reject. Does not resize and does not sign."""
    chosen = DEFAULT_LIMITS if limits is None else limits
    if not isinstance(today, str) or _DAY.fullmatch(today) is None:
        return RiskResult(False, "bad_day", ledger, False, order.notional_usd)
    current = roll_day(ledger, today)
    code = _first(order, current, chosen)
    if order.action == "flatten" and code in _FLATTEN_SKIPS:
        code = _first(order, current, chosen, flatten=True)
    if code is None:
        booked = _book(order, current)
        return RiskResult(True, "ok", booked, False, order.notional_usd)
    halted = current.halted or code in _HALT_CODES
    updated = current if halted == current.halted else replace(current, halted=halted)
    return RiskResult(False, code, updated, False, order.notional_usd)


def _first(
    order: LiveOrder,
    ledger: DayLedger,
    limits: RiskLimits,
    *,
    flatten: bool = False,
) -> str | None:
    checks = (
        _side,
        _flags,
        _mark,
        _notional,
        _loss,
        _positions,
        _allow,
        _slippage,
        _impact,
        _pool,
        _authorities,
        _sell_route,
        _stale,
        _fees,
        _ceiling,
    )
    for check in checks:
        code = check(order, ledger, limits)
        if code is None:
            continue
        if flatten and code in _FLATTEN_SKIPS:
            continue
        return code
    return None


def _side(order: LiveOrder, _ledger: DayLedger, _limits: RiskLimits) -> str | None:
    if order.side not in ("buy", "sell"):
        return "bad_side"
    if order.action == "flatten" and order.side != "sell":
        return "bad_side"
    if order.action not in ("entry", "exit", "flatten"):
        return "bad_side"
    if order.side == "buy" and order.action != "entry":
        return "bad_side"
    if order.side == "sell" and order.action == "entry":
        return "bad_side"
    return None


def _flags(order: LiveOrder, ledger: DayLedger, _limits: RiskLimits) -> str | None:
    if not order.trading_enabled:
        return "trading_disabled"
    if order.kill_halted:
        return "killed"
    if order.dead_man_halted:
        return "dead_man"
    if ledger.halted:
        return "daily_halt"
    return None


def _mark(order: LiveOrder, _ledger: DayLedger, _limits: RiskLimits) -> str | None:
    if not order.mark_ok or order.notional_usd is None:
        return "missing_mark"
    return None


def _notional(order: LiveOrder, ledger: DayLedger, limits: RiskLimits) -> str | None:
    if order.notional_usd is None:
        return "missing_mark"
    if order.notional_usd <= 0:
        return "bad_notional"
    if order.notional_usd > limits.per_trade_usd:
        return "per_trade"
    if ledger.notional_usd + order.notional_usd > limits.daily_notional_usd:
        return "daily_notional"
    return None


def _loss(order: LiveOrder, ledger: DayLedger, limits: RiskLimits) -> str | None:
    del order
    if ledger.realized_loss_usd >= limits.daily_loss_usd:
        return "daily_loss"
    return None


def _positions(order: LiveOrder, _ledger: DayLedger, limits: RiskLimits) -> str | None:
    if order.side != "buy":
        return None
    if order.open_positions >= limits.max_open_positions:
        return "position_cap"
    if order.asset_already_open:
        return "asset_open"
    return None


def _allow(order: LiveOrder, _ledger: DayLedger, limits: RiskLimits) -> str | None:
    if order.side == "buy" and order.mint not in limits.allowlist:
        return "allowlist"
    return None


def _slippage(order: LiveOrder, _ledger: DayLedger, limits: RiskLimits) -> str | None:
    if order.slippage_bps is None:
        return "slippage_param"
    if order.slippage_bps != limits.slippage_bps:
        return "slippage_param"
    if order.slippage_bps > limits.max_slippage_bps:
        return "slippage"
    implied = _implied_bps(order.out_amount, order.minimum_out_amount)
    if implied is None or implied > limits.max_slippage_bps:
        return "slippage"
    return None


def _impact(order: LiveOrder, _ledger: DayLedger, limits: RiskLimits) -> str | None:
    if _impact_over(order.price_impact, limits):
        return "price_impact"
    return None


def _pool(order: LiveOrder, _ledger: DayLedger, limits: RiskLimits) -> str | None:
    if order.side != "buy":
        return None
    if order.pool_liquidity_usd is None:
        return "pool_liquidity"
    if order.pool_liquidity_usd < limits.min_pool_liquidity_usd:
        return "pool_liquidity"
    if order.pool_age_seconds is None:
        return "pool_age"
    if order.pool_age_seconds < limits.min_pool_age_seconds:
        return "pool_age"
    return None


def _authorities(order: LiveOrder, _ledger: DayLedger, _limits: RiskLimits) -> str | None:
    if order.side != "buy":
        return None
    if order.freeze_authority_set is None or order.freeze_authority_set:
        return "freeze_authority"
    if order.mint_authority_set is None or order.mint_authority_set:
        return "mint_authority"
    return None


def _sell_route(order: LiveOrder, _ledger: DayLedger, limits: RiskLimits) -> str | None:
    if order.side != "buy":
        return None
    if order.sell_route_ok is not True:
        return "sell_route"
    if _impact_over(order.sell_route_impact, limits):
        return "sell_route"
    return None


def _stale(order: LiveOrder, _ledger: DayLedger, limits: RiskLimits) -> str | None:
    if order.bar_age_bars is None or order.bar_age_bars > limits.stale_bars:
        return "stale_bar"
    if (
        order.bar_price is None
        or order.quote_price is None
        or order.bar_price <= 0
        or order.quote_price <= 0
    ):
        return "divergence"
    gap = abs(order.bar_price - order.quote_price) / order.bar_price
    if gap > limits.max_divergence:
        return "divergence"
    return None


def _fees(order: LiveOrder, ledger: DayLedger, limits: RiskLimits) -> str | None:
    fees = order.fees
    if fees is None:
        return "missing_fee"
    if _negative_fee(fees):
        return "per_tx_fee"
    total = fees.total_lamports()
    if total > limits.max_fee_lamports:
        return "per_tx_fee"
    if ledger.fee_total_lamports() + total > limits.daily_fee_lamports:
        return "daily_fee"
    return None


def _ceiling(order: LiveOrder, _ledger: DayLedger, limits: RiskLimits) -> str | None:
    if order.side != "buy":
        return None
    if order.wallet_value_usd is None:
        return "wallet_ceiling"
    if order.wallet_value_usd > limits.wallet_ceiling_usd:
        return "wallet_ceiling"
    return None


def _book(order: LiveOrder, ledger: DayLedger) -> DayLedger:
    """Add a positive notional. Zero and negative are not applied."""
    notional = order.notional_usd
    if notional is not None and notional <= 0:
        return ledger
    if notional is None:
        notional = Decimal("0")
    if order.fees is None:
        return replace(ledger, notional_usd=ledger.notional_usd + notional)
    if _negative_fee(order.fees):
        return ledger
    booked = add_fees(ledger, order.fees)
    return replace(booked, notional_usd=booked.notional_usd + notional)


def _negative_fee(fees: FeeLines) -> bool:
    parts = (
        fees.network_lamports,
        fees.priority_lamports,
        fees.rent_lamports,
        fees.venue_fee_amount,
    )
    for part in parts:
        if part is None:
            continue
        if isinstance(part, bool) or not isinstance(part, int) or part < 0:
            return True
    return False


def _implied_bps(out_amount: int, minimum_out: int) -> int | None:
    if isinstance(out_amount, bool) or isinstance(minimum_out, bool):
        return None
    if not isinstance(out_amount, int) or not isinstance(minimum_out, int):
        return None
    if out_amount <= 0 or minimum_out <= 0 or minimum_out > out_amount:
        return None
    return (out_amount - minimum_out) * 10_000 // out_amount


def _impact_over(text: str | None, limits: RiskLimits) -> bool:
    if text is None:
        return True
    try:
        number = Decimal(text)
    except Exception:
        return True
    if not number.is_finite():
        return True
    return abs(number) > limits.max_price_impact
