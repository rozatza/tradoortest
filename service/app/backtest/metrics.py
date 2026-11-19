"""Backtest metrics. Formulas and edge cases are in ``docs/metrics.md``.

Ratios are half-even to 1e-10. A ratio that the definition leaves undefined
(zero variance, no losses, no drawdown, no closed trades) is 0. Nothing
here is NaN or infinite.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, localcontext
from fractions import Fraction

from app.backtest.quality import QualityCounts

_RATIO = Decimal("1e-10")
_ZERO = Decimal("0")
_SECONDS_PER_YEAR = 365 * 24 * 60 * 60


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
    """The run's ratios, streaks, and data-quality counts. All decimals are finite."""

    initial_equity: Decimal
    final_equity: Decimal
    net_return: Decimal
    annualized_return: Decimal
    bars_per_year: int
    bar_count: int
    max_drawdown: Decimal
    sharpe: Decimal
    sortino: Decimal
    calmar: Decimal
    win_rate: Decimal
    payoff_ratio: Decimal
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
    """Ratios from the cent equity curve and the closed-trade pnl, in exit order."""
    per_year = bars_per_year(bar_seconds)
    returns = _returns(initial, curve)
    net = _net(initial, curve[-1] if curve else initial)
    annualized = _annualized(net, len(curve), per_year)
    drawdown = _drawdown(initial, curve)
    sharpe, sortino = _risk_ratios(returns, per_year)
    calmar = _ratio(Decimal(0)) if drawdown == 0 else _ratio(annualized / drawdown)
    wins = [pnl for pnl in closed_pnls if pnl > 0]
    losses = [pnl for pnl in closed_pnls if pnl < 0]
    win_streak, loss_streak = _streaks(closed_pnls)
    return Metrics(
        initial_equity=initial,
        final_equity=curve[-1] if curve else initial,
        net_return=_ratio_fraction(net),
        annualized_return=annualized,
        bars_per_year=per_year,
        bar_count=len(curve),
        max_drawdown=drawdown,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        win_rate=_rate(len(wins), len(closed_pnls)),
        payoff_ratio=_payoff(wins, losses),
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


def _returns(initial: Decimal, curve: Sequence[Decimal]) -> list[Fraction]:
    previous = Fraction(initial)
    returns: list[Fraction] = []
    for equity in curve:
        current = Fraction(equity)
        if previous == 0:
            returns.append(Fraction(0))
        else:
            returns.append((current - previous) / previous)
        previous = current
    return returns


def _net(initial: Decimal, final: Decimal) -> Fraction:
    if initial == 0:
        return Fraction(0)
    return (Fraction(final) - Fraction(initial)) / Fraction(initial)


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
    return _ratio_fraction(worst)


def _risk_ratios(returns: Sequence[Fraction], per_year: int) -> tuple[Decimal, Decimal]:
    count = len(returns)
    if count < 2:
        return _ratio(_ZERO), _ratio(_ZERO)
    mean = sum(returns, Fraction(0)) / count
    variance = sum((item - mean) ** 2 for item in returns)
    downside = sum(min(item, Fraction(0)) ** 2 for item in returns)
    scale = _sqrt(Fraction(per_year))
    mean_dec = _decimal(mean)
    sharpe = _ratio(_ZERO)
    if variance != 0:
        sharpe = _ratio(mean_dec / _sqrt(variance / (count - 1)) * scale)
    sortino = _ratio(_ZERO)
    if downside != 0:
        sortino = _ratio(mean_dec / _sqrt(downside / (count - 1)) * scale)
    return sharpe, sortino


def _annualized(net: Fraction, periods: int, per_year: int) -> Decimal:
    if periods <= 0 or net == 0:
        return _ratio(_ZERO)
    base = 1 + net
    if base <= 0:
        return _ratio(Decimal(-1))
    try:
        with localcontext() as ctx:
            ctx.prec = 50
            ctx.rounding = ROUND_HALF_EVEN
            grown = _decimal(base) ** (Decimal(per_year) / Decimal(periods))
            return _ratio(grown - 1)
    except DecimalException:
        return _ratio(_ZERO)


def _payoff(wins: Sequence[Decimal], losses: Sequence[Decimal]) -> Decimal:
    if not wins or not losses:
        return _ratio(_ZERO)
    average_win = sum((Fraction(pnl) for pnl in wins), Fraction(0)) / len(wins)
    average_loss = sum((Fraction(-pnl) for pnl in losses), Fraction(0)) / len(losses)
    if average_loss == 0:
        return _ratio(_ZERO)
    return _ratio_fraction(average_win / average_loss)


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


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator == 0:
        return _ratio(_ZERO)
    return _ratio_fraction(Fraction(numerator, denominator))


def _sqrt(amount: Fraction) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_HALF_EVEN
        return _decimal(amount).sqrt()


def _decimal(amount: Fraction) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_HALF_EVEN
        return Decimal(amount.numerator) / Decimal(amount.denominator)


def _ratio_fraction(amount: Fraction) -> Decimal:
    return _ratio(_decimal(amount))


def _ratio(amount: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_HALF_EVEN
        return amount.quantize(_RATIO, rounding=ROUND_HALF_EVEN)
