"""Backtest metrics. Formulas and edge cases are in ``docs/metrics.md``.

Ratios are half-even to 1e-10 inside a fresh 50-digit context. That context
is not a copy of the caller's: full exponents, and only ``InvalidOperation``,
``DivisionByZero``, and ``Overflow`` trap. A ratio the definition leaves
undefined is ``None``, and ``undefined`` names the reason. A defined zero
stays 0. Nothing here is NaN or infinite, and a ratio that the 1e-10
quantize cannot hold does not raise.

``annualization_overflow`` is only the annualized return and Calmar. A net
return, Sharpe, Sortino, win rate, or payoff that will not quantize is
``overflow``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from fractions import Fraction
from types import MappingProxyType

from app.backtest.quality import QualityCounts

_RATIO = Decimal("1e-10")
_ZERO = Decimal("0")
_DAY = 24 * 60 * 60
_SECONDS_PER_YEAR = 365 * _DAY


@dataclass(frozen=True, slots=True)
class Trade:
    """One long, from the open through the flat close, or still open at the end.

    Pyramids and partial sells stay on the same trade. ``qty`` is the closed
    quantity, or the quantity still open. Money fields are USDC cents.
    Prices and ``qty`` are 50-significant-digit decimals, not re-rounded to
    the cent.
    """

    trade_id: int
    asset_id: str
    status: str
    entry_time: int
    exit_time: int | None
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal | None
    entry_notional: Decimal
    exit_notional: Decimal
    commission: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    limit_below_low: bool
    overlaps_suspect_outage: bool
    synthetic_bars: int
    missing_bars: int
    long_no_trade_bars: int
    long_no_trade_minutes: int
    suspect_outage_bars: int
    suspect_outage_minutes: int


@dataclass(frozen=True, slots=True)
class Metrics:
    """The run's ratios, streaks, and data-quality counts.

    A ratio is ``None`` when it is undefined. ``undefined`` maps that field
    to a reason. Decimals that are present are finite.
    """

    initial_equity: Decimal
    final_equity: Decimal
    net_return: Decimal | None
    annualized_return: Decimal | None
    bars_per_year: int
    bar_count: int
    max_drawdown: Decimal
    sharpe: Decimal | None
    sortino: Decimal | None
    calmar: Decimal | None
    win_rate: Decimal | None
    payoff_ratio: Decimal | None
    undefined: Mapping[str, str]
    win_streak: int
    loss_streak: int
    closed_trades: int
    open_trades: int
    take_profit_limit_below_low: int
    grid_bars: int
    missing_bars: int
    synthetic_bars: int
    long_no_trade_bars: int
    long_no_trade_minutes: int
    long_no_trade_threshold: int
    suspect_outage: bool
    suspect_outage_bars: int
    suspect_outage_minutes: int
    missing_bars_in_position: int
    synthetic_bars_in_position: int
    long_no_trade_bars_in_position: int
    long_no_trade_minutes_in_position: int
    suspect_outage_bars_in_position: int
    suspect_outage_minutes_in_position: int


def bars_per_year(bar_seconds: int) -> int:
    """How many bars of this size fit in a 365-day year."""
    return _SECONDS_PER_YEAR // bar_seconds


def _fresh_context() -> Context:
    """50-digit half-even context with the module's full exponent range.

    Built from scratch, not copied from the caller. A caller's traps, precision,
    and ``Emax`` cannot turn a ratio into NaN or into a false overflow.
    """
    return Context(
        prec=50,
        rounding=ROUND_HALF_EVEN,
        Emax=MAX_EMAX,
        Emin=MIN_EMIN,
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


@contextmanager
def _local_ratio() -> Iterator[None]:
    """Run the block in :func:`_fresh_context`."""
    with localcontext(_fresh_context()):
        yield


def compute_metrics(
    *,
    initial: Decimal,
    curve: Sequence[Decimal],
    closed_pnls: Sequence[Decimal],
    open_trades: int,
    bar_seconds: int,
    take_profit_limit_below_low: int,
    quality: QualityCounts,
) -> Metrics:
    """Ratios from the cent equity curve and the closed-trade pnl, in exit order.

    The arithmetic runs in a fresh 50-digit context, so the caller's precision,
    traps, and exponent range do not change a ratio. Bar-return sums for
    Sharpe and Sortino are 50-digit decimals, not exact rationals. An overflow
    becomes a null ratio. It does not raise.
    """
    with _local_ratio():
        return _compute(
            initial=initial,
            curve=curve,
            closed_pnls=closed_pnls,
            open_trades=open_trades,
            bar_seconds=bar_seconds,
            take_profit_limit_below_low=take_profit_limit_below_low,
            quality=quality,
        )


def _compute(
    *,
    initial: Decimal,
    curve: Sequence[Decimal],
    closed_pnls: Sequence[Decimal],
    open_trades: int,
    bar_seconds: int,
    take_profit_limit_below_low: int,
    quality: QualityCounts,
) -> Metrics:
    per_year = bars_per_year(bar_seconds)
    periods = len(curve)
    final = curve[-1] if curve else initial
    undefined: dict[str, str] = {}
    blank = _blank_reason(periods, initial)
    returns = _returns(initial, curve)
    net_value, net_reason = _net(initial, final, blank)
    _put(undefined, "net_return", net_reason)
    annualized, annual_reason = _annualized(
        _net_fraction(initial, final), periods, per_year, bar_seconds, blank, net_reason
    )
    _put(undefined, "annualized_return", annual_reason)
    drawdown = _drawdown(initial, curve)
    sharpe, sharpe_reason, sortino, sortino_reason = _risk_ratios(returns, per_year, blank)
    _put(undefined, "sharpe", sharpe_reason)
    _put(undefined, "sortino", sortino_reason)
    calmar, calmar_reason = _calmar(annualized, annual_reason, drawdown)
    _put(undefined, "calmar", calmar_reason)
    wins = [pnl for pnl in closed_pnls if pnl > 0]
    losses = [pnl for pnl in closed_pnls if pnl < 0]
    win_rate, win_reason = _rate(len(wins), len(closed_pnls))
    _put(undefined, "win_rate", win_reason)
    payoff, payoff_reason = _payoff(wins, losses, len(closed_pnls))
    _put(undefined, "payoff_ratio", payoff_reason)
    win_streak, loss_streak = _streaks(closed_pnls)
    return Metrics(
        initial_equity=initial,
        final_equity=final,
        net_return=net_value,
        annualized_return=annualized,
        bars_per_year=per_year,
        bar_count=periods,
        max_drawdown=drawdown,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        win_rate=win_rate,
        payoff_ratio=payoff,
        undefined=MappingProxyType(undefined),
        win_streak=win_streak,
        loss_streak=loss_streak,
        closed_trades=len(closed_pnls),
        open_trades=open_trades,
        take_profit_limit_below_low=take_profit_limit_below_low,
        grid_bars=quality.grid_bars,
        missing_bars=quality.missing_bars,
        synthetic_bars=quality.synthetic_bars,
        long_no_trade_bars=quality.long_no_trade_bars,
        long_no_trade_minutes=quality.long_no_trade_minutes,
        long_no_trade_threshold=quality.long_no_trade_threshold,
        suspect_outage=quality.suspect_outage,
        suspect_outage_bars=quality.long_no_trade_bars,
        suspect_outage_minutes=quality.long_no_trade_minutes,
        missing_bars_in_position=quality.missing_bars_in_position,
        synthetic_bars_in_position=quality.synthetic_bars_in_position,
        long_no_trade_bars_in_position=quality.long_no_trade_bars_in_position,
        long_no_trade_minutes_in_position=quality.long_no_trade_minutes_in_position,
        suspect_outage_bars_in_position=quality.long_no_trade_bars_in_position,
        suspect_outage_minutes_in_position=quality.long_no_trade_minutes_in_position,
    )


def _put(undefined: dict[str, str], name: str, reason: str | None) -> None:
    if reason is not None:
        undefined[name] = reason


def _blank_reason(periods: int, initial: Decimal) -> str | None:
    if periods == 0:
        return "no_bars"
    if initial == 0:
        return "zero_initial"
    return None


def _returns(initial: Decimal, curve: Sequence[Decimal]) -> list[Decimal]:
    """One return per curve point, at the active 50-digit precision.

    A previous equity of 0 contributes a 0 return. The division is not done.
    """
    previous = initial
    returns: list[Decimal] = []
    for equity in curve:
        if previous == 0:
            returns.append(_ZERO)
        else:
            returns.append((equity - previous) / previous)
        previous = equity
    return returns


def _net_fraction(initial: Decimal, final: Decimal) -> Fraction:
    if initial == 0:
        return Fraction(0)
    return (Fraction(final) - Fraction(initial)) / Fraction(initial)


def _net(
    initial: Decimal, final: Decimal, blank: str | None
) -> tuple[Decimal | None, str | None]:
    if blank is not None:
        return None, blank
    value = _ratio_fraction(_net_fraction(initial, final))
    if value is None:
        return None, "overflow"
    return value, None


def _drawdown(initial: Decimal, curve: Sequence[Decimal]) -> Decimal:
    peak = Fraction(initial)
    worst = Fraction(0)
    for equity in curve:
        current = Fraction(equity)
        if current > peak:
            peak = current
        if peak > 0:
            drop = (peak - current) / peak
            if drop > worst:
                worst = drop
    value = _ratio_fraction(worst)
    return Decimal(0) if value is None else value


def _risk_ratios(
    returns: Sequence[Decimal], per_year: int, blank: str | None
) -> tuple[Decimal | None, str | None, Decimal | None, str | None]:
    if blank is not None:
        return None, blank, None, blank
    count = len(returns)
    if count < 2:
        return None, "too_few_returns", None, "too_few_returns"
    # 50-digit running sums. Exact rationals make the denominators grow with
    # the curve, and a long run spends its time in gcd.
    counted = Decimal(count)
    mean = sum(returns, _ZERO) / counted
    variance = sum(((item - mean) ** 2 for item in returns), _ZERO)
    downside = sum((min(item, _ZERO) ** 2 for item in returns), _ZERO)
    try:
        scale = _sqrt(Fraction(per_year))
    except DecimalException:
        return None, "overflow", None, "overflow"
    divisor = Decimal(count - 1)
    if variance == 0:
        sharpe, sharpe_reason = None, "zero_variance"
    else:
        sharpe = _scale(mean, variance / divisor, scale)
        sharpe_reason = None if sharpe is not None else "overflow"
    if downside == 0:
        sortino, sortino_reason = None, "no_downside"
    else:
        # Every return is in the sum. A positive return contributes 0.
        # The divisor is N - 1, the same sample divisor as Sharpe, not the
        # count of negative returns and not N.
        sortino = _scale(mean, downside / divisor, scale)
        sortino_reason = None if sortino is not None else "overflow"
    return sharpe, sharpe_reason, sortino, sortino_reason


def _scale(mean: Decimal, second: Decimal, per_year_sqrt: Decimal) -> Decimal | None:
    try:
        with _local_ratio():
            if second == 0:
                return None
            deviation = second.sqrt()
            if deviation == 0:
                return None
            return _ratio(mean / deviation * per_year_sqrt)
    except DecimalException:
        return None


def _annualized(
    net: Fraction,
    periods: int,
    per_year: int,
    bar_seconds: int,
    blank: str | None,
    net_reason: str | None,
) -> tuple[Decimal | None, str | None]:
    if blank is not None:
        return None, blank
    if periods * bar_seconds < _DAY:
        return None, "too_short"
    if net_reason == "overflow":
        return None, "overflow"
    if net == 0:
        return _ratio(_ZERO), None
    base = 1 + net
    if base <= 0:
        return _ratio(Decimal(-1)), None
    try:
        with _local_ratio():
            grown = _decimal(base) ** (Decimal(per_year) / Decimal(periods))
            value = _ratio(grown - 1)
    except DecimalException:
        value = None
    if value is None:
        return None, "annualization_overflow"
    return value, None


def _calmar(
    annualized: Decimal | None, annual_reason: str | None, drawdown: Decimal
) -> tuple[Decimal | None, str | None]:
    if annualized is None:
        return None, annual_reason
    if drawdown == 0:
        return None, "no_drawdown"
    try:
        with _local_ratio():
            value = _ratio(annualized / drawdown)
    except DecimalException:
        value = None
    if value is None:
        return None, "annualization_overflow"
    return value, None


def _payoff(
    wins: Sequence[Decimal], losses: Sequence[Decimal], closed: int
) -> tuple[Decimal | None, str | None]:
    if closed == 0:
        return None, "no_closed_trades"
    if not wins:
        return None, "no_wins"
    if not losses:
        return None, "no_losses"
    average_win = sum((Fraction(pnl) for pnl in wins), Fraction(0)) / len(wins)
    average_loss = sum((Fraction(-pnl) for pnl in losses), Fraction(0)) / len(losses)
    if average_loss == 0:
        return None, "no_losses"
    value = _ratio_fraction(average_win / average_loss)
    if value is None:
        return None, "overflow"
    return value, None


def _streaks(pnls: Sequence[Decimal]) -> tuple[int, int]:
    wins = losses = best_wins = best_losses = 0
    for pnl in pnls:
        if pnl > 0:
            wins += 1
            losses = 0
        elif pnl < 0:
            losses += 1
            wins = 0
        else:
            wins = 0
            losses = 0
        best_wins = max(best_wins, wins)
        best_losses = max(best_losses, losses)
    return best_wins, best_losses


def _rate(numerator: int, denominator: int) -> tuple[Decimal | None, str | None]:
    if denominator == 0:
        return None, "no_closed_trades"
    value = _ratio_fraction(Fraction(numerator, denominator))
    if value is None:
        return None, "overflow"
    return value, None


def _sqrt(amount: Fraction) -> Decimal:
    with _local_ratio():
        return _decimal(amount).sqrt()


def _decimal(amount: Fraction) -> Decimal:
    with _local_ratio():
        return Decimal(amount.numerator) / Decimal(amount.denominator)


def _ratio_fraction(amount: Fraction) -> Decimal | None:
    try:
        return _ratio(_decimal(amount))
    except DecimalException:
        return None


def _ratio(amount: Decimal) -> Decimal | None:
    with _local_ratio():
        return amount.quantize(_RATIO, rounding=ROUND_HALF_EVEN)
