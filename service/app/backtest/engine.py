"""Run canonical IR on bars. One user, one book, no look-ahead.

The clock is the bar grid from the first open time through the last. A
missing slot is a hole: indicators see NaN there, rules do not fill, and
nothing is copied forward from the previous close. A bar flagged
``synthetic`` is a no-trade minute. Its close may feed indicators, and a
market order on that bar is skipped. It is not queued for a later real bar.

Fills happen at that bar's close (``fill_price_mode=on_close``) after
slippage. The book is long-only spot: cash plus open longs, in USDC cents.
An entry's OCO pair is attached at the fill and is not read on that bar.
Take-profit fills at its limit. Stop-loss fills at the stop, or at the open
when the bar gaps through the stop. A synthetic bar cannot touch a leg, the
same as a missing bar. The next real bar is read on its own.

The decision for bar ``i`` reads bars ``0..i`` only. ``app.runner`` makes
that decision from a window that ends at ``i``. This module applies it to
the book. Indicator functions are the causal library in ``app.indicators``,
called on the grid with holes left as NaN. A later bar cannot change an
earlier fill.

Metrics and the trade log are ``docs/metrics.md``. A fill quantity is rounded
down to ``1e-12``, a stand-in for a token's base units, so a buy never takes
more than its notional. USDC cents are half-even of that grid quantity times
the fill price.
"""

from __future__ import annotations

import contextvars
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)
from fractions import Fraction
from typing import Any

from app.backtest.errors import BacktestError
from app.backtest.metrics import Metrics, Trade, compute_metrics
from app.backtest.money import decimal_ratio, floor_qty, quantize_cents
from app.backtest.quality import AssetFlags, QualityCounts, asset_flags, outage_lints
from app.indicators import atr, bollinger, ema, macd, rsi, sma, stochastic
from app.indicators.errors import IndicatorError
from app.indicators.lints import collect_lints
from app.ir.validate import format_issues, validate_ir
from app.ohlcv.bars import MAX_ABS_VALUE, Bar
from app.runner import (
    BarDecision,
    BookView,
    OcoDrop,
    OcoLevels,
    config_from_document,
    initial_state,
    make_window,
)
from app.runner import step as decide_bar

# IR bar sizes. 3m, 30m, and the multi-hour sizes are not fetched as native
# OHLCV; the engine still runs them when the caller supplies those bars.
_BAR_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
}

_BPS = Decimal(10000)
_BUILTINS = ("open", "high", "low", "close", "volume")
# Two 1m bars a year apart build about half a million slots. That grid is
# rejected before it is allocated. 200_000 covers a 90-day 1m series
# (129_600 bars) and refuses the year-long 1m span.
MAX_GRID_BARS = 200_000


@dataclass(frozen=True, slots=True)
class Fill:
    """One simulated fill. ``side`` is buy to open or sell to close a long.

    ``leg`` is ``market`` for a rule, or ``take_profit`` / ``stop_loss`` for
    the OCO pair attached at the entry fill.
    """

    open_time: int
    asset_id: str
    side: str
    qty: Decimal
    price: Decimal
    notional: Decimal
    commission: Decimal
    equity_after: Decimal
    leg: str = "market"
    limit_below_low: bool = False


@dataclass(frozen=True, slots=True)
class SkippedOrder:
    """A true signal that did not fill. The order is not retried on a later bar."""

    open_time: int
    asset_id: str
    action_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """Book value at a bar close, after that bar's fills. Quantized to the cent."""

    open_time: int
    equity: Decimal
    cash: Decimal


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """A long still open at the end of the run. ``entry_price`` is the average fill."""

    asset_id: str
    qty: Decimal
    entry_price: Decimal


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Fills, skips, and the cent equity curve. Same inputs always produce this."""

    fills: tuple[Fill, ...]
    skipped: tuple[SkippedOrder, ...]
    equity_curve: tuple[EquityPoint, ...]
    final_equity: Decimal
    final_cash: Decimal
    final_positions: tuple[OpenPosition, ...]
    lints: tuple[str, ...]
    metrics: Metrics
    trades: tuple[Trade, ...]


@dataclass
class _Position:
    """``qty`` is a multiple of ``1e-12``. ``cost`` is the open quote notional.

    ``entry`` is ``cost / qty`` from the last fill. Bars that do not trade
    read this stored average. They do not divide the position again.
    """

    qty: Decimal
    cost: Decimal
    entry: Decimal = Decimal("0")

    @property
    def entry_price(self) -> Decimal:
        return self.entry


def _remember(position: _Position) -> None:
    """Store the average fill. The next bars read ``entry`` and do not divide."""
    if position.qty == 0:
        position.entry = Decimal("0")
        return
    position.entry = position.cost / position.qty


@dataclass
class _Acc:
    """One open long while the walk is still running."""

    asset_id: str
    entry_time: int
    qty: Decimal = Decimal(0)
    qty_entry: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)
    entry_value: Decimal = Decimal(0)
    exit_value: Decimal = Decimal(0)
    entry_notional: Decimal = Decimal("0.00")
    exit_notional: Decimal = Decimal("0.00")
    buy_commission: Decimal = Decimal("0.00")
    sell_commission: Decimal = Decimal("0.00")
    limit_below_low: bool = False
    exit_time: int | None = None
    # Open cost including the buy commission still attached to the open quantity.
    # A partial sale removes its share of this, half-even on a 1e-30 grid.
    basis: Fraction = Fraction(0)
    # Booked at each sell from the basis then open. Later buys do not rewrite it.
    realized_exact: Fraction = Fraction(0)


@dataclass
class _Oco:
    """Levels anchored to the first entry fill."""

    take_profit: Decimal
    stop_loss: Decimal
    attached_at: int
    anchor: Decimal


@dataclass
class _Book:
    cash: Decimal
    positions: dict[str, _Position]
    ocos: dict[str, _Oco]


@dataclass
class _Frame:
    """One asset aligned to the grid. Missing slots are NaN and ``present`` is false."""

    by_time: dict[int, Bar]
    series: dict[str, list[float]]
    present: list[bool]


def move_oco(
    take_profit: Decimal,
    stop_loss: Decimal,
    new_take_profit: Decimal,
    new_stop_loss: Decimal,
) -> tuple[Decimal, Decimal]:
    """Return tightened long levels, or raise when a leg would loosen.

    The stop may stay or move up. The target may stay or move down. Moving
    either the other way rejects the whole change: the caller keeps both
    previous prices. A target that is not strictly above the stop is rejected
    too. A NaN or an infinity raises ``BacktestError``, not ``InvalidOperation``.
    """
    try:
        levels = (take_profit, stop_loss, new_take_profit, new_stop_loss)
        if not all(_finite_price(level) for level in levels):
            raise BacktestError("OCO level is not a finite price")
        looser: list[str] = []
        if new_stop_loss < stop_loss:
            looser.append("stop_loss")
        if new_take_profit > take_profit:
            looser.append("take_profit")
        if looser:
            names = " and ".join(looser)
            raise BacktestError(f"tighten_only rejected a looser {names}")
        if new_stop_loss <= 0 or new_take_profit <= new_stop_loss:
            raise BacktestError("tighten_only rejected a take-profit that is not above the stop")
        return new_take_profit, new_stop_loss
    except DecimalException as exc:
        raise BacktestError("OCO level is not a finite price") from exc


def _finite_price(level: object) -> bool:
    return isinstance(level, Decimal) and level.is_finite()


def _engine_context() -> Context:
    """50-digit half-even context. Built from scratch, not copied.

    The caller's traps, precision, and exponent range do not apply. Inexact
    is not trapped. ``Emin`` is the full decimal minimum, not a caller's
    narrow bound.
    """
    ctx = Context(
        prec=50,
        rounding=ROUND_HALF_EVEN,
        Emax=MAX_EMAX,
        Emin=MIN_EMIN,
        traps=[InvalidOperation, DivisionByZero, Overflow],
        clamp=0,
    )
    ctx.traps[InvalidOperation] = True
    ctx.traps[DivisionByZero] = True
    ctx.traps[Overflow] = True
    ctx.traps[Inexact] = False
    return ctx


def grid_bar_count(bar_size: str, open_times: Sequence[int]) -> int:
    """How many slots ``open_times`` would place on ``bar_size``.

    An empty list is 0. This does not allocate the grid.
    """
    if bar_size not in _BAR_SECONDS:
        raise BacktestError(f"unsupported bar size {bar_size}", code="invalid_bar_size")
    if not open_times:
        return 0
    step = _BAR_SECONDS[bar_size]
    return (max(open_times) - min(open_times)) // step + 1


def run_backtest(
    document: Mapping[str, Any],
    bars: Mapping[str, Sequence[Bar]],
    *,
    long_no_trade_minutes: int = 120,
) -> BacktestResult:
    """Simulate ``document`` on ``bars`` keyed by asset id.

    ``document`` must be valid canonical IR. Bars are that asset's candles
    for ``engine.bar_size``. The run does not fetch data and does not place
    a live order. ``long_no_trade_minutes`` is the quiet-run cut from
    ``docs/ohlcv.md``. A longer run is a suspect outage.
    """
    if isinstance(long_no_trade_minutes, bool) or not isinstance(long_no_trade_minutes, int):
        raise BacktestError("long_no_trade_minutes must be a whole number of minutes")
    if long_no_trade_minutes < 1:
        raise BacktestError("long_no_trade_minutes must be at least 1")
    _between_slots.set(0)
    _basis_denominator.set(1)
    try:
        # A fresh context, not a copy of the caller's. Copying kept the
        # caller's traps and Emin: Inexact then aborted the run, and Emin -5
        # changed a fill quantity. Precision, rounding, exponents, and traps
        # are fixed here.
        with localcontext(_engine_context()):
            return _run_backtest(document, bars, long_no_trade_minutes)
    except DecimalException as exc:
        raise BacktestError("order amount is outside the range this book can price") from exc


def _run_backtest(
    document: Mapping[str, Any],
    bars: Mapping[str, Sequence[Bar]],
    long_no_trade_minutes: int,
) -> BacktestResult:
    issues = validate_ir(document)
    if issues:
        first = issues[0]
        raise BacktestError(format_issues(issues), code=first.keyword, path=first.path)
    bar_size = document["engine"]["bar_size"]
    step = _BAR_SECONDS[bar_size]
    assets = list(document["assets"])
    asset_ids = [asset["id"] for asset in assets]
    _reject_unknown_bars(asset_ids, bars)
    frames = {
        asset["id"]: _frame(asset["id"], bars[asset["id"]], bar_size, step) for asset in assets
    }
    initial = quantize_cents(Fraction(_dec(document["capital"]["initial"])))
    times = [bar.open_time for frame in frames.values() for bar in frame.by_time.values()]
    if not times:
        return _empty_result(initial, step, long_no_trade_minutes)
    slots = (max(times) - min(times)) // step + 1
    if slots > MAX_GRID_BARS:
        raise BacktestError(
            f"time grid is {slots} bars, above the limit of {MAX_GRID_BARS}",
            code="grid_too_large",
        )
    grid = list(range(min(times), max(times) + step, step))
    _align(frames, grid)
    flags = {
        asset_id: _asset_flags(frame, grid, step, long_no_trade_minutes, bar_size == "1m")
        for asset_id, frame in frames.items()
    }
    indicator_lints = _derive(document["derived_series"], frames)
    lints = list(_series_lints(document["derived_series"], frames, len(grid)))
    lints.extend(indicator_lints)
    lints.extend(
        outage_lints(
            document["derived_series"],
            {asset_id: frame.series for asset_id, frame in frames.items()},
            {asset_id: item.outage for asset_id, item in flags.items()},
        )
    )
    return _walk(
        document,
        frames,
        grid,
        initial,
        lints,
        flags,
        step,
        long_no_trade_minutes,
    )


def _reject_unknown_bars(asset_ids: list[str], bars: Mapping[str, Sequence[Bar]]) -> None:
    missing = [asset_id for asset_id in asset_ids if asset_id not in bars]
    if missing:
        raise BacktestError(f"no bars for asset {missing[0]}")
    known = set(asset_ids)
    extra = [key for key in bars if key not in known]
    if extra:
        raise BacktestError(f"bars for unknown asset {extra[0]}")


def _frame(asset_id: str, bars: Sequence[Bar], bar_size: str, step: int) -> _Frame:
    by_time: dict[int, Bar] = {}
    for bar in bars:
        if not isinstance(bar, Bar):
            raise BacktestError(f"bars for asset {asset_id} must be Bar values")
        _check_bar(bar, asset_id, bar_size, step)
        if bar.open_time in by_time:
            raise BacktestError(f"duplicate bar for asset {asset_id}")
        by_time[bar.open_time] = bar
    if not by_time:
        raise BacktestError(f"no bars for asset {asset_id}")
    return _Frame(by_time=by_time, series={}, present=[])


def _check_bar(bar: Bar, asset_id: str, bar_size: str, step: int) -> None:
    if bar.timeframe != bar_size:
        raise BacktestError(f"bar timeframe for asset {asset_id} does not match {bar_size}")
    if isinstance(bar.open_time, bool) or not isinstance(bar.open_time, int):
        raise BacktestError(f"bar open_time for asset {asset_id} is not an integer second")
    if bar.open_time % step != 0:
        raise BacktestError(f"bar open_time for asset {asset_id} is not aligned to {bar_size}")
    if not isinstance(bar.synthetic, bool):
        raise BacktestError(f"synthetic flag for asset {asset_id} is not a boolean")
    values: dict[str, float] = {}
    for name in _BUILTINS:
        value = getattr(bar, name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise BacktestError(f"bar {name} for asset {asset_id} is not finite")
        if value < 0 or value > MAX_ABS_VALUE or (name != "volume" and value == 0):
            raise BacktestError(f"bar {name} for asset {asset_id} is out of range")
        values[name] = float(value)
    if values["high"] < max(values["open"], values["close"]):
        raise BacktestError(f"bar high for asset {asset_id} is below the open or close")
    if values["low"] > min(values["open"], values["close"]):
        raise BacktestError(f"bar low for asset {asset_id} is above the open or close")
    if bar.synthetic and (
        values["volume"] != 0.0 or any(values[name] != values["open"] for name in _BUILTINS[:4])
    ):
        raise BacktestError(f"synthetic bar for asset {asset_id} must be flat with volume 0")


def _align(frames: dict[str, _Frame], grid: list[int]) -> None:
    for frame in frames.values():
        columns = {name: [] for name in _BUILTINS}
        present: list[bool] = []
        for open_time in grid:
            bar = frame.by_time.get(open_time)
            present.append(bar is not None)
            for name in _BUILTINS:
                columns[name].append(math.nan if bar is None else float(getattr(bar, name)))
        frame.series = columns
        frame.present = present


def _derive(specs: Sequence[Mapping[str, Any]], frames: dict[str, _Frame]) -> tuple[str, ...]:
    """Write derived series and return this run's indicator lints.

    Lints are collected on a context-local list. The warnings registry is
    not touched, so a second run in another thread cannot take these texts
    or leave a filter installed.
    """
    notes: list[str] = []
    seen: set[str] = set()
    with collect_lints() as caught:
        for index, spec in enumerate(specs):
            series_id = str(spec["id"])
            path = f"$.derived_series[{index}]"
            for asset_id, frame in frames.items():
                source = frame.series.get(spec["source"])
                if source is None:
                    raise BacktestError(f"unknown series {spec['source']}", path=path)
                caught.clear()
                frame.series[series_id] = _indicator(spec, source, frame, path=path)
                for text in caught:
                    cleaned = text.strip()
                    if not cleaned:
                        continue
                    note = f"{series_id} on {asset_id}: {cleaned}"
                    if note not in seen:
                        seen.add(note)
                        notes.append(note)
    return tuple(notes)


def _indicator(
    spec: Mapping[str, Any],
    source: Sequence[float],
    frame: _Frame,
    *,
    path: str,
) -> list[float]:
    name = spec["indicator"]
    params = spec["params"]
    high = frame.series["high"]
    low = frame.series["low"]
    close = frame.series["close"]
    try:
        if name == "sma":
            values = sma(source, int(params["period"]))
        elif name == "ema":
            values = ema(source, int(params["period"]))
        elif name == "rsi":
            values = rsi(source, int(params["period"]))
        elif name == "atr":
            values = atr(high, low, close, int(params["period"]))
        elif name == "macd":
            result = macd(
                source, int(params["fast"]), int(params["slow"]), int(params["signal"])
            )
            values = list(getattr(result, spec["output"]))
        elif name == "bollinger":
            result = bollinger(source, int(params["period"]), float(params["stddev"]))
            values = list(getattr(result, spec["output"]))
        elif name == "stochastic":
            result = stochastic(
                high, low, close, int(params["k_period"]), int(params["d_period"])
            )
            values = list(getattr(result, spec["output"]))
        else:
            raise BacktestError(f"unknown indicator {name}", path=path)
    except OverflowError as exc:
        raise BacktestError(
            f"indicator {name} overflowed",
            code="indicator_overflow",
            path=path,
        ) from exc
    except IndicatorError as exc:
        raise BacktestError(str(exc), code=exc.code, path=path) from exc
    for index, value in enumerate(values):
        if isinstance(value, float) and math.isinf(value):
            raise BacktestError(
                f"indicator {name} result at index {index} is not finite",
                code="indicator_non_finite",
                path=path,
            )
    return values


def _series_lints(
    specs: Sequence[Mapping[str, Any]], frames: dict[str, _Frame], total: int
) -> tuple[str, ...]:
    """Warn when a derived series is empty on most of the grid.

    Sparse native buckets leave SMA and the other seeded indicators NaN.
    The run still completes. The lint says how many bars were defined so a
    strategy that never trades is not silent.
    """
    if total == 0:
        return ()
    notes: list[str] = []
    for spec in specs:
        series_id = str(spec["id"])
        for asset_id, frame in frames.items():
            values = frame.series.get(series_id, [])
            defined = sum(
                1 for value in values if isinstance(value, float) and math.isfinite(value)
            )
            if defined * 2 < total:
                notes.append(
                    f"derived series {series_id} on {asset_id} is defined on "
                    f"{defined} of {total} bars"
                )
    return tuple(notes)


def _asset_flags(
    frame: _Frame,
    grid: Sequence[int],
    step: int,
    threshold: int,
    one_minute: bool,
) -> AssetFlags:
    present: list[bool] = []
    synthetic: list[bool] = []
    for open_time in grid:
        bar = frame.by_time.get(open_time)
        present.append(bar is not None)
        synthetic.append(bar is not None and bar.synthetic)
    return asset_flags(
        present,
        synthetic,
        step=step,
        threshold_minutes=threshold,
        one_minute=one_minute,
    )


def _empty_result(initial: Decimal, step: int, threshold: int) -> BacktestResult:
    quality = QualityCounts(0, 0, 0, 0, 0, threshold, 0, 0, 0, 0)
    metrics = compute_metrics(
        initial=initial,
        curve=(),
        closed_pnls=(),
        open_trades=0,
        bar_seconds=step,
        take_profit_limit_below_low=0,
        quality=quality,
    )
    return BacktestResult((), (), (), initial, initial, (), (), metrics, ())


def _walk(
    document: Mapping[str, Any],
    frames: dict[str, _Frame],
    grid: list[int],
    initial: Decimal,
    lints: list[str],
    flags: dict[str, AssetFlags],
    step: int,
    threshold: int,
) -> BacktestResult:
    book = _Book(cash=initial, positions={}, ocos={})
    actions = {action["id"]: action for action in document["actions"]}
    portfolio = document["portfolio"]
    allow_pyramiding = bool(portfolio["allow_pyramiding"])
    max_positions = int(portfolio["max_positions"])
    commission_bps = _dec(document["costs"]["commission_bps"])
    slippage_bps = _dec(document["costs"]["slippage_bps"])
    if slippage_bps >= _BPS:
        raise BacktestError("slippage_bps must be less than 10000; a sell would fill at 0")
    fills: list[Fill] = []
    skipped: list[SkippedOrder] = []
    curve: list[EquityPoint] = []
    marks: dict[str, Decimal] = {}
    last_mark: dict[str, Decimal] = {}
    open_acc: dict[str, _Acc] = {}
    closed_acc: list[_Acc] = []
    missing_bars = synthetic_bars = outage_bars = 0
    missing_held = synthetic_held = outage_held = 0
    config = config_from_document(document)
    runner_state = initial_state(config.asset_ids)
    series = {asset_id: frame.series for asset_id, frame in frames.items()}
    for index, open_time in enumerate(grid):
        held_before = set(book.positions)
        filled_now: set[str] = set()
        _mark_bar(frames, grid, index, marks, last_mark)
        bars_now = {asset_id: frame.by_time.get(open_time) for asset_id, frame in frames.items()}
        outcome = decide_bar(
            config,
            runner_state,
            make_window(index, open_time, series, bars_now),
            _book_view(book),
        )
        runner_state = outcome.state
        _apply_decision(
            outcome.decision,
            actions,
            book,
            marks,
            open_acc,
            closed_acc,
            fills,
            skipped,
            lints,
            filled_now,
            open_time,
            allow_pyramiding=allow_pyramiding,
            max_positions=max_positions,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
        )
        for asset_id, flag in flags.items():
            slot_missing = flag.missing[index]
            slot_synthetic = flag.synthetic[index]
            slot_outage = flag.outage[index]
            if slot_missing:
                missing_bars += 1
            if slot_synthetic:
                synthetic_bars += 1
            if slot_outage:
                outage_bars += 1
            in_position = (
                asset_id in held_before or asset_id in book.positions or asset_id in filled_now
            )
            if not in_position:
                continue
            if slot_missing:
                missing_held += 1
            if slot_synthetic:
                synthetic_held += 1
            if slot_outage:
                outage_held += 1
        curve.append(EquityPoint(open_time, _equity(book, marks), book.cash))
    final_equity = curve[-1].equity if curve else initial
    positions = tuple(
        OpenPosition(asset_id, position.qty, position.entry_price)
        for asset_id, position in book.positions.items()
    )
    _require_exact_pnl(closed_acc, open_acc, book, marks, initial)
    reported, exact_unrealized = _trades(closed_acc, open_acc, marks, flags, grid, step)
    trades = _spread_remainder(reported, exact_unrealized, initial, final_equity)
    quality = QualityCounts(
        grid_bars=len(grid),
        missing_bars=missing_bars,
        synthetic_bars=synthetic_bars,
        long_no_trade_bars=outage_bars,
        long_no_trade_minutes=outage_bars * step // 60,
        long_no_trade_threshold=threshold,
        missing_bars_in_position=missing_held,
        synthetic_bars_in_position=synthetic_held,
        long_no_trade_bars_in_position=outage_held,
        long_no_trade_minutes_in_position=outage_held * step // 60,
    )
    closed_pnls = [trade.realized_pnl for trade in trades if trade.status == "closed"]
    metrics = compute_metrics(
        initial=initial,
        curve=[point.equity for point in curve],
        closed_pnls=closed_pnls,
        open_trades=sum(trade.status == "open" for trade in trades),
        bar_seconds=step,
        take_profit_limit_below_low=sum(fill.limit_below_low for fill in fills),
        quality=quality,
    )
    return BacktestResult(
        tuple(fills),
        tuple(skipped),
        tuple(curve),
        final_equity,
        book.cash,
        positions,
        tuple(lints),
        metrics,
        trades,
    )


def _book_view(book: _Book) -> BookView:
    """Entry prices and OCO levels before this bar's fills. The runner only reads them."""
    return BookView(
        tuple((asset_id, position.entry) for asset_id, position in book.positions.items()),
        tuple(
            (asset_id, OcoLevels(oco.take_profit, oco.stop_loss, oco.attached_at))
            for asset_id, oco in book.ocos.items()
        ),
    )


def _apply_decision(
    decision: BarDecision,
    actions: Mapping[str, Mapping[str, Any]],
    book: _Book,
    marks: dict[str, Decimal],
    open_acc: dict[str, _Acc],
    closed_acc: list[_Acc],
    fills: list[Fill],
    skipped: list[SkippedOrder],
    lints: list[str],
    filled_now: set[str],
    open_time: int,
    *,
    allow_pyramiding: bool,
    max_positions: int,
    commission_bps: Decimal,
    slippage_bps: Decimal,
) -> None:
    """Apply one bar's decision. OCO first, then rules, same as the walk used to."""
    for item in decision.oco:
        if isinstance(item, OcoDrop):
            book.ocos.pop(item.asset_id, None)
            continue
        _fill_oco(
            book,
            item.asset_id,
            item.leg,
            item.raw_price,
            item.limit_below_low,
            open_time,
            fills,
            marks,
            open_acc,
            closed_acc,
            filled_now,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
        )
    for attempt in decision.rules:
        action = actions[attempt.action_id]
        asset_id = attempt.asset_id
        if attempt.skip is not None:
            skipped.append(SkippedOrder(open_time, asset_id, action["id"], attempt.skip))
            continue
        if attempt.close is None:
            raise BacktestError("a rule attempt has no close and no skip reason")
        outcome = _fill(
            action,
            attempt.close,
            book,
            marks,
            allow_pyramiding=allow_pyramiding,
            max_positions=max_positions,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
        )
        if isinstance(outcome, str):
            skipped.append(SkippedOrder(open_time, asset_id, action["id"], outcome))
            continue
        side, qty, price, notional, commission = outcome
        if side == "buy":
            _attach_oco(book, action, price, open_time, lints)
            _note_buy(open_acc, asset_id, open_time, qty, price, notional, commission)
        else:
            if asset_id not in book.positions:
                book.ocos.pop(asset_id, None)
            _note_sell(
                open_acc,
                closed_acc,
                asset_id,
                open_time,
                qty,
                price,
                notional,
                commission,
                False,
            )
        filled_now.add(asset_id)
        fills.append(
            Fill(
                open_time,
                asset_id,
                side,
                qty,
                price,
                notional,
                commission,
                _equity(book, marks),
                "market",
                False,
            )
        )


def _mark_bar(
    frames: dict[str, _Frame],
    grid: list[int],
    index: int,
    marks: dict[str, Decimal],
    last_mark: dict[str, Decimal],
) -> None:
    open_time = grid[index]
    for asset_id, frame in frames.items():
        bar = frame.by_time.get(open_time)
        if bar is not None:
            marks[asset_id] = _price(bar.close)
            last_mark[asset_id] = marks[asset_id]
        elif asset_id in last_mark:
            # Equity of a position already held. Not an indicator input, and
            # not a price copied into the hole for a fill.
            marks[asset_id] = last_mark[asset_id]


def _fill_oco(
    book: _Book,
    asset_id: str,
    leg: str,
    raw_price: Decimal,
    limit_below_low: bool,
    open_time: int,
    fills: list[Fill],
    marks: dict[str, Decimal],
    open_acc: dict[str, _Acc],
    closed_acc: list[_Acc],
    filled_now: set[str],
    *,
    commission_bps: Decimal,
    slippage_bps: Decimal,
) -> None:
    outcome = _exit(
        asset_id,
        Decimal(100),
        raw_price,
        book,
        commission_bps=commission_bps,
        slippage_bps=slippage_bps,
    )
    book.ocos.pop(asset_id, None)
    if isinstance(outcome, str):
        return
    side, qty, price, notional, commission = outcome
    _note_sell(
        open_acc,
        closed_acc,
        asset_id,
        open_time,
        qty,
        price,
        notional,
        commission,
        limit_below_low,
    )
    filled_now.add(asset_id)
    fills.append(
        Fill(
            open_time,
            asset_id,
            side,
            qty,
            price,
            notional,
            commission,
            _equity(book, marks),
            leg,
            limit_below_low,
        )
    )


def _attach_oco(
    book: _Book,
    action: Mapping[str, Any],
    fill_price: Decimal,
    open_time: int,
    lints: list[str],
) -> None:
    """Attach the pair on the first fill. A later enter may only tighten it."""
    asset_id = action["asset"]
    current = book.ocos.get(asset_id)
    anchor = fill_price if current is None else current.anchor
    take_profit, stop_loss = _oco_levels(anchor, action["risk"])
    if current is None:
        book.ocos[asset_id] = _Oco(take_profit, stop_loss, open_time, fill_price)
        return
    try:
        tightened = move_oco(current.take_profit, current.stop_loss, take_profit, stop_loss)
    except BacktestError as exc:
        lints.append(f"{exc} on {asset_id} at bar {open_time}")
        return
    current.take_profit, current.stop_loss = tightened


def _oco_levels(anchor: Decimal, risk: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    take_pct = _dec(risk["take_profit"]["percent"])
    stop_pct = _dec(risk["stop_loss"]["percent"])
    take_profit = anchor * (Decimal(100) + take_pct) / Decimal(100)
    stop_loss = anchor * (Decimal(100) - stop_pct) / Decimal(100)
    # A percent below the 50-digit unit of the anchor does not move the level,
    # so the stop and the target would both sit on the entry.
    if not (stop_loss > 0 and stop_loss < anchor < take_profit):
        raise BacktestError(
            "take_profit and stop_loss percents are too small to move the price off the entry"
        )
    return take_profit, stop_loss


def _fill(
    action: Mapping[str, Any],
    close: Decimal,
    book: _Book,
    marks: dict[str, Decimal],
    *,
    allow_pyramiding: bool,
    max_positions: int,
    commission_bps: Decimal,
    slippage_bps: Decimal,
) -> str | tuple[str, Decimal, Decimal, Decimal, Decimal]:
    """Apply one market order.

    A string is the skip reason. A tuple is
    ``(side, qty, price, notional, commission)`` after the book has changed.
    ``qty`` is the ``1e-12`` grid quantity.
    """
    asset_id = action["asset"]
    percent = _dec(action["sizing"]["percent"])
    if action["type"] == "enter":
        return _enter(
            asset_id,
            percent,
            close,
            book,
            marks,
            allow_pyramiding=allow_pyramiding,
            max_positions=max_positions,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
        )
    return _exit(
        asset_id,
        percent,
        close,
        book,
        commission_bps=commission_bps,
        slippage_bps=slippage_bps,
    )


def _enter(
    asset_id: str,
    percent: Decimal,
    close: Decimal,
    book: _Book,
    marks: dict[str, Decimal],
    *,
    allow_pyramiding: bool,
    max_positions: int,
    commission_bps: Decimal,
    slippage_bps: Decimal,
) -> str | tuple[str, Decimal, Decimal, Decimal, Decimal]:
    existing = book.positions.get(asset_id)
    if existing is not None and not allow_pyramiding:
        return "pyramiding"
    if existing is None and len(book.positions) >= max_positions:
        return "max_positions"
    equity = _equity(book, marks)
    budget = _cent(equity * percent / Decimal(100))
    spend = budget if budget <= book.cash else book.cash
    if spend <= 0:
        return "no_cash"
    room = _cent(spend * _BPS / (_BPS + commission_bps))
    if room <= 0:
        return "no_cash"
    fill_price = close * (_BPS + slippage_bps) / _BPS
    if fill_price <= 0:
        return "no_cash"
    # Down to the grid, so the quantity costs at most ``room``. The cash that
    # leaves is the cent of that quantity, not the unspent dust.
    qty = floor_qty(Fraction(room) / Fraction(fill_price))
    if qty <= 0:
        return "no_cash"
    notional = _cent(qty * fill_price)
    if notional <= 0 or notional > room:
        return "no_cash"
    commission = _cent(notional * commission_bps / _BPS)
    if notional + commission > spend:
        commission = spend - notional
    book.cash -= notional + commission
    if existing is None:
        position = _Position(qty, notional)
        _remember(position)
        book.positions[asset_id] = position
    else:
        existing.qty += qty
        existing.cost += notional
        _remember(existing)
    return ("buy", qty, fill_price, notional, commission)


def _exit(
    asset_id: str,
    percent: Decimal,
    close: Decimal,
    book: _Book,
    *,
    commission_bps: Decimal,
    slippage_bps: Decimal,
) -> str | tuple[str, Decimal, Decimal, Decimal, Decimal]:
    existing = book.positions.get(asset_id)
    if existing is None or existing.qty <= 0:
        return "no_position"
    if percent == Decimal(100):
        qty_exit = existing.qty
    else:
        qty_exit = floor_qty(Fraction(existing.qty) * Fraction(percent) / 100)
    if qty_exit <= 0 or qty_exit > existing.qty:
        return "no_position"
    fill_price = close * (_BPS - slippage_bps) / _BPS
    if fill_price < 0:
        return "no_position"
    notional = _cent(qty_exit * fill_price)
    commission = _cent(notional * commission_bps / _BPS)
    if commission > notional:
        commission = notional
    book.cash += notional - commission
    if qty_exit == existing.qty:
        del book.positions[asset_id]
    else:
        existing.cost = existing.cost * (existing.qty - qty_exit) / existing.qty
        existing.qty -= qty_exit
        _remember(existing)
    return ("sell", qty_exit, fill_price, notional, commission)


def _cent(amount: Decimal) -> Decimal:
    """Half-even to 0.01. ``amount`` is already a bounded decimal."""
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def _equity(book: _Book, marks: dict[str, Decimal]) -> Decimal:
    total = book.cash
    for asset_id, position in book.positions.items():
        price = marks.get(asset_id)
        if price is None:
            continue
        total += position.qty * price
    return _cent(total)


def _dec(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BacktestError("expected a number")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BacktestError("expected a finite number")
        return Decimal(str(value))
    return Decimal(value)


def _price(value: float) -> Decimal:
    return Decimal(str(value))


def _note_buy(
    open_acc: dict[str, _Acc],
    asset_id: str,
    open_time: int,
    qty: Decimal,
    price: Decimal,
    notional: Decimal,
    commission: Decimal,
) -> None:
    acc = open_acc.get(asset_id)
    if acc is None:
        acc = _Acc(asset_id, open_time)
        open_acc[asset_id] = acc
    acc.qty += qty
    acc.qty_entry += qty
    acc.cost += notional
    acc.entry_value += qty * price
    acc.entry_notional += notional
    acc.buy_commission += commission
    acc.basis += Fraction(notional) + Fraction(commission)


# A partial sale's share of basis, in units of 1e-30. Exact ``basis * qty /
# previous`` multiplies a new denominator into the open basis on every trim.
_BASIS_QUANTUM = 10**30


def _sold_basis(basis: Fraction, qty: Decimal, previous: Decimal) -> Fraction:
    """Basis removed by one sale.

    A full close takes the basis still open. A partial sale takes
    ``basis * qty / previous``, half-even to ``1e-30``. The same rounded
    amount is booked as realized and removed from the open basis, so the
    pnl identity stays exact and the denominator stays bounded.
    """
    if qty == previous:
        _note_basis_denominator(basis.denominator)
        return basis
    share = basis * Fraction(qty) / Fraction(previous)
    sold = Fraction(round(share * _BASIS_QUANTUM), _BASIS_QUANTUM)
    _note_basis_denominator(sold.denominator)
    return sold


def _note_sell(
    open_acc: dict[str, _Acc],
    closed_acc: list[_Acc],
    asset_id: str,
    open_time: int,
    qty: Decimal,
    price: Decimal,
    notional: Decimal,
    commission: Decimal,
    limit_below_low: bool,
) -> None:
    acc = open_acc[asset_id]
    previous = acc.qty
    sold_basis = _sold_basis(acc.basis, qty, previous)
    acc.realized_exact += Fraction(notional) - Fraction(commission) - sold_basis
    acc.basis -= sold_basis
    if qty == previous:
        acc.basis = Fraction(0)
        acc.cost = Decimal(0)
        acc.qty = Decimal(0)
    else:
        acc.cost = acc.cost * (previous - qty) / previous
        acc.qty = previous - qty
    acc.exit_value += qty * price
    acc.exit_notional += notional
    acc.sell_commission += commission
    acc.limit_below_low = acc.limit_below_low or limit_below_low
    if acc.qty == 0:
        acc.exit_time = open_time
        closed_acc.append(acc)
        del open_acc[asset_id]


def _trades(
    closed_acc: list[_Acc],
    open_acc: dict[str, _Acc],
    marks: Mapping[str, Decimal],
    flags: Mapping[str, AssetFlags],
    grid: Sequence[int],
    step: int,
) -> tuple[tuple[Trade, ...], tuple[Fraction, ...]]:
    ordered = [*closed_acc, *open_acc.values()]
    trades: list[Trade] = []
    exact_unrealized: list[Fraction] = []
    # One prefix per asset. Each trade then reads its span in log(bars),
    # instead of walking the whole grid.
    prefixes = {
        asset_id: (
            _flag_prefix(flag.synthetic),
            _flag_prefix(flag.missing),
            _flag_prefix(flag.outage),
        )
        for asset_id, flag in flags.items()
    }
    for number, acc in enumerate(ordered, start=1):
        end = grid[-1] if acc.exit_time is None else acc.exit_time
        synthetic_prefix, missing_prefix, outage_prefix = prefixes[acc.asset_id]
        synthetic = _between(synthetic_prefix, grid, acc.entry_time, end)
        missing = _between(missing_prefix, grid, acc.entry_time, end)
        outage = _between(outage_prefix, grid, acc.entry_time, end)
        sold = acc.qty_entry - acc.qty
        status = "closed" if acc.qty == 0 else "open"
        realized, unrealized = _pnl(acc, marks.get(acc.asset_id))
        if status == "closed":
            unrealized = Decimal("0.00")
        exit_price = decimal_ratio(Fraction(acc.exit_value) / Fraction(sold)) if sold > 0 else None
        entry_price = (
            decimal_ratio(Fraction(acc.entry_value) / Fraction(acc.qty_entry))
            if acc.qty_entry
            else Decimal("0")
        )
        qty = acc.qty_entry if status == "closed" else acc.qty
        minutes = outage * step // 60
        trades.append(
            Trade(
                trade_id=number,
                asset_id=acc.asset_id,
                status=status,
                entry_time=acc.entry_time,
                exit_time=acc.exit_time,
                qty=qty,
                entry_price=entry_price,
                exit_price=exit_price,
                entry_notional=acc.entry_notional,
                exit_notional=acc.exit_notional,
                commission=acc.buy_commission + acc.sell_commission,
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                limit_below_low=acc.limit_below_low,
                overlaps_suspect_outage=outage > 0,
                synthetic_bars=synthetic,
                missing_bars=missing,
                long_no_trade_bars=outage,
                long_no_trade_minutes=minutes,
                suspect_outage_bars=outage,
                suspect_outage_minutes=minutes,
            )
        )
        if status == "open":
            exact_unrealized.append(_exact_unrealized(acc, marks.get(acc.asset_id)))
        else:
            exact_unrealized.append(Fraction(0))
    return tuple(trades), tuple(exact_unrealized)


def _pnl(acc: _Acc, mark: Decimal | None) -> tuple[Decimal, Decimal]:
    """Cents from the basis booked at each fill.

    Realized uses the basis that was open at the sell, so a later add does
    not reprice it. Unrealized marks what is left, and that basis still
    includes the buy commission on the open quantity.
    """
    sold = acc.qty_entry - acc.qty
    if sold == 0:
        realized = Decimal("0.00")
    else:
        realized = quantize_cents(acc.realized_exact)
    if acc.qty == 0 or mark is None:
        unrealized = Decimal("0.00")
    else:
        unrealized = quantize_cents(Fraction(acc.qty) * Fraction(mark) - acc.basis)
    return realized, unrealized


def _book_pnl(
    closed_acc: Sequence[_Acc], open_acc: Mapping[str, _Acc], marks: Mapping[str, Decimal]
) -> Fraction:
    """Exact realized plus open mark-to-basis, before any cent rounding."""
    total = Fraction(0)
    for acc in (*closed_acc, *open_acc.values()):
        total += acc.realized_exact
        if acc.qty != 0:
            total += _exact_unrealized(acc, marks.get(acc.asset_id))
    return total


def _unrounded_equity(book: _Book, marks: Mapping[str, Decimal]) -> Fraction:
    total = Fraction(book.cash)
    for asset_id, position in book.positions.items():
        price = marks.get(asset_id)
        if price is None:
            continue
        total += Fraction(position.qty) * Fraction(price)
    return total


def _exact_unrealized(acc: _Acc, mark: Decimal | None) -> Fraction:
    if acc.qty == 0 or mark is None:
        return Fraction(0)
    return Fraction(acc.qty) * Fraction(mark) - acc.basis


def _require_exact_pnl(
    closed_acc: Sequence[_Acc],
    open_acc: Mapping[str, _Acc],
    book: _Book,
    marks: Mapping[str, Decimal],
    initial: Decimal,
) -> None:
    """The booked fraction must equal unrounded equity minus initial.

    This runs before cent rounding. A sub-cent miss is a real accounting
    error, and it raises instead of being absorbed by the cent plug.
    """
    exact = _book_pnl(closed_acc, open_acc, marks)
    if exact != _unrounded_equity(book, marks) - Fraction(initial):
        raise BacktestError("realized and unrealized pnl do not match the equity change")


def _spread_remainder(
    trades: tuple[Trade, ...],
    exact_unrealized: Sequence[Fraction],
    initial: Decimal,
    final: Decimal,
) -> tuple[Trade, ...]:
    """Give the cent gap to open trades by largest remainder.

    The exact identity has already held. Each open trade's realized and
    unrealized pnl are then half-even cents, and ``final`` is already a
    cent. The gap ``final - initial - sum(cents)`` is at most one cent per
    open trade. Each whole cent of that gap is added to, or taken from, one
    open trade's unrealized pnl: the trade whose unrounded unrealized pnl
    sits farthest from its rounded cent in the direction of the gap. A tie
    goes to the earlier open trade, the smaller trade id. No trade moves by
    more than one cent. Closed trades are left as rounded.
    """
    total = sum((trade.realized_pnl + trade.unrealized_pnl for trade in trades), Decimal("0.00"))
    delta = final - initial - total
    if delta == 0:
        return trades
    open_indexes = [index for index, trade in enumerate(trades) if trade.status == "open"]
    scaled = delta * 100
    if scaled != scaled.to_integral_value() or not open_indexes:
        raise BacktestError("realized and unrealized pnl do not match the equity change")
    cents = int(scaled)
    if abs(cents) > len(open_indexes):
        raise BacktestError("realized and unrealized pnl do not match the equity change")
    sign = 1 if cents > 0 else -1

    def _rank(index: int) -> tuple[Fraction, int]:
        remainder = Fraction(exact_unrealized[index]) - Fraction(trades[index].unrealized_pnl)
        # A positive gap fills the trades rounded down the most. A negative
        # gap takes the cent back from the trades rounded up the most.
        return (-sign * remainder, index)

    chosen = set(sorted(open_indexes, key=_rank)[: abs(cents)])
    step = Decimal("0.01") * sign
    patched: list[Trade] = []
    for index, trade in enumerate(trades):
        if index in chosen:
            patched.append(replace(trade, unrealized_pnl=trade.unrealized_pnl + step))
        else:
            patched.append(trade)
    return tuple(patched)


def _flag_prefix(flags: Sequence[bool]) -> list[int]:
    """Inclusive counts. ``prefix[i]`` is the number of true flags before ``i``."""
    prefix = [0]
    total = 0
    for flag in flags:
        if flag:
            total += 1
        prefix.append(total)
    return prefix


# Slots ``_between`` examines. A bisect probes a few stamps per call. A walk
# of the whole grid would add one per slot per call. Tests bound this.
# One context per run, so two threads do not add their probes together.
_between_slots: contextvars.ContextVar[int] = contextvars.ContextVar(
    "tradoortest_between_slots", default=0
)
# Largest basis denominator this run wrote. Exact ``basis * qty / previous``
# grows without a bound. The 1e-30 grid stays at ``10**30``.
_basis_denominator: contextvars.ContextVar[int] = contextvars.ContextVar(
    "tradoortest_basis_denominator", default=1
)


def between_slots() -> int:
    """Probes ``_between`` made by the run on this thread."""
    return _between_slots.get()


def basis_denominator() -> int:
    """Largest basis denominator written by the run on this thread."""
    return _basis_denominator.get()


def _probe() -> None:
    _between_slots.set(_between_slots.get() + 1)


def _note_basis_denominator(denominator: int) -> None:
    if denominator > _basis_denominator.get():
        _basis_denominator.set(denominator)


def _lower_bound(grid: Sequence[int], target: int) -> int:
    lo = 0
    hi = len(grid)
    while lo < hi:
        _probe()
        mid = (lo + hi) // 2
        if grid[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _upper_bound(grid: Sequence[int], target: int) -> int:
    lo = 0
    hi = len(grid)
    while lo < hi:
        _probe()
        mid = (lo + hi) // 2
        if grid[mid] <= target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _between(prefix: Sequence[int], grid: Sequence[int], start: int, end: int) -> int:
    """How many flagged slots have an open time in ``start..end``, inclusive.

    The span is two binary searches. The prefix then answers in one subtraction.
    """
    lo = _lower_bound(grid, start)
    hi = _upper_bound(grid, end)
    return prefix[hi] - prefix[lo]
