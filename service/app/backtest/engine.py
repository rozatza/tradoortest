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

The decision for bar ``i`` reads bars ``0..i`` only. Indicator functions are
the causal library in ``app.indicators``, called on the grid with holes left
as NaN. A later bar cannot change an earlier fill.

Metrics and the trade log are ``docs/metrics.md``. Quantity is an exact
fraction. USDC cents are half-even from that fraction, not from a 50-digit
approximation of it.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, localcontext
from fractions import Fraction
from typing import Any

from app.backtest.errors import BacktestError
from app.backtest.eval import EvalContext, EvalMemory, evaluate
from app.backtest.metrics import Metrics, Trade, compute_metrics
from app.backtest.money import decimal_ratio, quantize_cents
from app.backtest.quality import AssetFlags, QualityCounts, asset_flags, outage_lints
from app.indicators import atr, bollinger, ema, macd, rsi, sma, stochastic
from app.indicators.errors import IndicatorLint
from app.ir.validate import format_issues, validate_ir
from app.ohlcv.bars import MAX_ABS_VALUE, Bar

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
    """``qty`` and ``cost`` are exact. ``cost`` is the open quote notional."""

    qty: Fraction
    cost: Fraction

    @property
    def entry_price(self) -> Decimal:
        if self.qty == 0:
            return Decimal("0")
        return decimal_ratio(self.cost / self.qty)


@dataclass
class _Acc:
    """One open long while the walk is still running."""

    asset_id: str
    entry_time: int
    qty: Fraction = field(default_factory=lambda: Fraction(0))
    qty_entry: Fraction = field(default_factory=lambda: Fraction(0))
    cost: Fraction = field(default_factory=lambda: Fraction(0))
    entry_value: Fraction = field(default_factory=lambda: Fraction(0))
    exit_value: Fraction = field(default_factory=lambda: Fraction(0))
    entry_notional: Decimal = Decimal("0.00")
    exit_notional: Decimal = Decimal("0.00")
    buy_commission: Decimal = Decimal("0.00")
    sell_commission: Decimal = Decimal("0.00")
    limit_below_low: bool = False
    exit_time: int | None = None


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
    try:
        with localcontext() as ctx:
            # 1e12 capital marked at 1e15 after a 1e-9 fill needs more than
            # the default 28 digits. Anything still outside this precision
            # becomes BacktestError instead of an uncaught Decimal error.
            ctx.prec = 50
            ctx.rounding = ROUND_HALF_EVEN
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
        raise BacktestError(format_issues(issues))
    bar_size = document["engine"]["bar_size"]
    step = _BAR_SECONDS[bar_size]
    warmup = int(document["engine"]["warmup_bars"])
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
        warmup,
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
    """Write derived series and return indicator warnings moved onto the result.

    ``catch_warnings`` takes the warning off the caller's filter, so it is
    not also emitted. The text names the series and the asset.
    """
    notes: list[str] = []
    seen: set[str] = set()
    for spec in specs:
        series_id = str(spec["id"])
        for asset_id, frame in frames.items():
            source = frame.series.get(spec["source"])
            if source is None:
                raise BacktestError(f"unknown series {spec['source']}")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", IndicatorLint)
                frame.series[series_id] = _indicator(spec, source, frame)
            for item in caught:
                if not issubclass(item.category, IndicatorLint):
                    continue
                text = str(item.message).strip()
                if not text:
                    continue
                note = f"{series_id} on {asset_id}: {text}"
                if note not in seen:
                    seen.add(note)
                    notes.append(note)
    return tuple(notes)


def _indicator(spec: Mapping[str, Any], source: Sequence[float], frame: _Frame) -> list[float]:
    name = spec["indicator"]
    params = spec["params"]
    high = frame.series["high"]
    low = frame.series["low"]
    close = frame.series["close"]
    if name == "sma":
        return sma(source, int(params["period"]))
    if name == "ema":
        return ema(source, int(params["period"]))
    if name == "rsi":
        return rsi(source, int(params["period"]))
    if name == "atr":
        return atr(high, low, close, int(params["period"]))
    if name == "macd":
        result = macd(source, int(params["fast"]), int(params["slow"]), int(params["signal"]))
        return list(getattr(result, spec["output"]))
    if name == "bollinger":
        result = bollinger(source, int(params["period"]), float(params["stddev"]))
        return list(getattr(result, spec["output"]))
    if name == "stochastic":
        result = stochastic(
            high, low, close, int(params["k_period"]), int(params["d_period"])
        )
        return list(getattr(result, spec["output"]))
    raise BacktestError(f"unknown indicator {name}")


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
    warmup: int,
    initial: Decimal,
    lints: list[str],
    flags: dict[str, AssetFlags],
    step: int,
    threshold: int,
) -> BacktestResult:
    book = _Book(cash=initial, positions={}, ocos={})
    signals = {signal["id"]: signal["expr"] for signal in document["signals"]}
    actions = {action["id"]: action for action in document["actions"]}
    rules = list(document["rules"])
    portfolio = document["portfolio"]
    allow_pyramiding = bool(portfolio["allow_pyramiding"])
    max_positions = int(portfolio["max_positions"])
    commission_bps = _dec(document["costs"]["commission_bps"])
    slippage_bps = _dec(document["costs"]["slippage_bps"])
    if slippage_bps >= _BPS:
        raise BacktestError("slippage_bps must be less than 10000; a sell would fill at 0")
    memory = {asset_id: EvalMemory() for asset_id in frames}
    fills: list[Fill] = []
    skipped: list[SkippedOrder] = []
    curve: list[EquityPoint] = []
    marks: dict[str, Decimal] = {}
    last_mark: dict[str, Decimal] = {}
    open_acc: dict[str, _Acc] = {}
    closed_acc: list[_Acc] = []
    missing_bars = synthetic_bars = outage_bars = 0
    missing_held = synthetic_held = outage_held = 0
    pairs = _rule_pairs(rules, actions)
    for index, open_time in enumerate(grid):
        held_before = set(book.positions)
        filled_now: set[str] = set()
        _mark_bar(frames, grid, index, marks, last_mark)
        truth = _snapshot(pairs, signals, frames, memory, book, index)
        if index >= warmup:
            _settle_ocos(
                book,
                frames,
                open_time,
                fills,
                marks,
                open_acc,
                closed_acc,
                filled_now,
                commission_bps=commission_bps,
                slippage_bps=slippage_bps,
            )
            for rule in rules:
                action = actions[rule["do"]]
                asset_id = action["asset"]
                if not truth[(rule["when"], asset_id)]:
                    continue
                bar = frames[asset_id].by_time.get(open_time)
                if bar is None:
                    skipped.append(SkippedOrder(open_time, asset_id, action["id"], "missing_bar"))
                    continue
                if bar.synthetic:
                    skipped.append(
                        SkippedOrder(open_time, asset_id, action["id"], "synthetic_bar")
                    )
                    continue
                outcome = _fill(
                    action,
                    _price(bar.close),
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
                        decimal_ratio(qty),
                        price,
                        notional,
                        commission,
                        _equity(book, marks),
                        "market",
                        False,
                    )
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
        OpenPosition(asset_id, decimal_ratio(position.qty), position.entry_price)
        for asset_id, position in book.positions.items()
    )
    trades = _trades(closed_acc, open_acc, marks, flags, grid, step)
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


def _rule_pairs(
    rules: Sequence[Mapping[str, Any]], actions: Mapping[str, Mapping[str, Any]]
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for rule in rules:
        pair = (rule["when"], actions[rule["do"]]["asset"])
        if pair not in pairs:
            pairs.append(pair)
    return pairs


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


def _snapshot(
    pairs: Sequence[tuple[str, str]],
    signals: Mapping[str, Mapping[str, Any]],
    frames: dict[str, _Frame],
    memory: dict[str, EvalMemory],
    book: _Book,
    index: int,
) -> dict[tuple[str, str], bool]:
    """Signal values from the book before any fill on this bar."""
    truth: dict[tuple[str, str], bool] = {}
    for signal_id, asset_id in pairs:
        position = book.positions.get(asset_id)
        entry = float(position.entry_price) if position is not None else None
        value = evaluate(
            signals[signal_id],
            EvalContext(index, frames[asset_id].series, entry, memory[asset_id]),
            (signal_id,),
        )
        truth[(signal_id, asset_id)] = value is True
    return truth


def _settle_ocos(
    book: _Book,
    frames: dict[str, _Frame],
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
    """Fill a touched OCO leg before this bar's rules.

    The entry bar is skipped: the pair is attached later in the bar, and a
    pair whose ``attached_at`` is this bar is not read. A missing bar and a
    synthetic bar are not touches. The next real bar is evaluated on its own
    high and low.
    """
    for asset_id in frames:
        oco = book.ocos.get(asset_id)
        if oco is None or asset_id not in book.positions:
            book.ocos.pop(asset_id, None)
            continue
        if oco.attached_at == open_time:
            continue
        bar = frames[asset_id].by_time.get(open_time)
        if bar is None or bar.synthetic:
            continue
        leg = _oco_leg(bar, oco)
        if leg is None:
            continue
        raw = _live_price(bar, oco, leg)
        below = leg == "take_profit" and raw < _price(bar.low)
        _fill_oco(
            book,
            asset_id,
            leg,
            raw,
            below,
            open_time,
            fills,
            marks,
            open_acc,
            closed_acc,
            filled_now,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
        )


def _oco_leg(bar: Bar | None, oco: _Oco) -> str | None:
    """Stop wins when one bar touches both levels. A hole touches neither."""
    if bar is None:
        return None
    if _price(bar.low) <= oco.stop_loss:
        return "stop_loss"
    if _price(bar.high) >= oco.take_profit:
        return "take_profit"
    return None


def _live_price(bar: Bar, oco: _Oco, leg: str) -> Decimal:
    """Limit at the target. Stop at the stop, or at the open when gapped through."""
    opened = _price(bar.open)
    if leg == "stop_loss" and opened <= oco.stop_loss:
        return opened
    if leg == "stop_loss":
        return oco.stop_loss
    return oco.take_profit


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
            decimal_ratio(qty),
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
) -> str | tuple[str, Fraction, Decimal, Decimal, Decimal]:
    """Apply one market order.

    A string is the skip reason. A tuple is
    ``(side, qty, price, notional, commission)`` after the book has changed.
    ``qty`` is the exact fraction.
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
) -> str | tuple[str, Fraction, Decimal, Decimal, Decimal]:
    existing = book.positions.get(asset_id)
    if existing is not None and not allow_pyramiding:
        return "pyramiding"
    if existing is None and len(book.positions) >= max_positions:
        return "max_positions"
    equity = _equity(book, marks)
    budget = quantize_cents(Fraction(equity) * Fraction(percent) / 100)
    spend = budget if budget <= book.cash else book.cash
    if spend <= 0:
        return "no_cash"
    notional = quantize_cents(
        Fraction(spend) * Fraction(_BPS) / (Fraction(_BPS) + Fraction(commission_bps))
    )
    if notional <= 0:
        return "no_cash"
    commission = spend - notional
    fill_price = close * (_BPS + slippage_bps) / _BPS
    if fill_price <= 0:
        return "no_cash"
    qty = Fraction(notional) / Fraction(fill_price)
    book.cash -= spend
    if existing is None:
        book.positions[asset_id] = _Position(qty, Fraction(notional))
    else:
        existing.qty += qty
        existing.cost += Fraction(notional)
    return ("buy", qty, fill_price, notional, commission)


def _exit(
    asset_id: str,
    percent: Decimal,
    close: Decimal,
    book: _Book,
    *,
    commission_bps: Decimal,
    slippage_bps: Decimal,
) -> str | tuple[str, Fraction, Decimal, Decimal, Decimal]:
    existing = book.positions.get(asset_id)
    if existing is None or existing.qty <= 0:
        return "no_position"
    if percent == Decimal(100):
        qty_exit = existing.qty
    else:
        qty_exit = existing.qty * Fraction(percent) / 100
    if qty_exit <= 0 or qty_exit > existing.qty:
        return "no_position"
    fill_price = close * (_BPS - slippage_bps) / _BPS
    if fill_price < 0:
        return "no_position"
    notional = quantize_cents(qty_exit * Fraction(fill_price))
    commission = quantize_cents(Fraction(notional) * Fraction(commission_bps) / Fraction(_BPS))
    if commission > notional:
        commission = notional
    book.cash += notional - commission
    if qty_exit == existing.qty:
        del book.positions[asset_id]
    else:
        existing.cost = existing.cost * (existing.qty - qty_exit) / existing.qty
        existing.qty -= qty_exit
    return ("sell", qty_exit, fill_price, notional, commission)


def _equity(book: _Book, marks: dict[str, Decimal]) -> Decimal:
    total = Fraction(book.cash)
    for asset_id, position in book.positions.items():
        price = marks.get(asset_id)
        if price is None:
            continue
        total += position.qty * Fraction(price)
    return quantize_cents(total)


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
    qty: Fraction,
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
    acc.cost += Fraction(notional)
    acc.entry_value += qty * Fraction(price)
    acc.entry_notional += notional
    acc.buy_commission += commission


def _note_sell(
    open_acc: dict[str, _Acc],
    closed_acc: list[_Acc],
    asset_id: str,
    open_time: int,
    qty: Fraction,
    price: Decimal,
    notional: Decimal,
    commission: Decimal,
    limit_below_low: bool,
) -> None:
    acc = open_acc[asset_id]
    previous = acc.qty
    acc.exit_value += qty * Fraction(price)
    acc.exit_notional += notional
    acc.sell_commission += commission
    acc.limit_below_low = acc.limit_below_low or limit_below_low
    acc.cost = acc.cost * (previous - qty) / previous
    acc.qty = previous - qty
    if acc.qty == 0:
        acc.exit_time = open_time
        acc.cost = Fraction(0)
        closed_acc.append(acc)
        del open_acc[asset_id]


def _trades(
    closed_acc: list[_Acc],
    open_acc: dict[str, _Acc],
    marks: Mapping[str, Decimal],
    flags: Mapping[str, AssetFlags],
    grid: Sequence[int],
    step: int,
) -> tuple[Trade, ...]:
    ordered = [*closed_acc, *open_acc.values()]
    trades: list[Trade] = []
    for number, acc in enumerate(ordered, start=1):
        end = grid[-1] if acc.exit_time is None else acc.exit_time
        synthetic, missing, outage = _span(flags[acc.asset_id], grid, acc.entry_time, end)
        sold = acc.qty_entry - acc.qty
        status = "closed" if acc.qty == 0 else "open"
        realized, unrealized = _pnl(acc, marks.get(acc.asset_id))
        if status == "closed":
            unrealized = Decimal("0.00")
        exit_price = decimal_ratio(acc.exit_value / sold) if sold > 0 else None
        entry_price = (
            decimal_ratio(acc.entry_value / acc.qty_entry) if acc.qty_entry else Decimal("0")
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
                qty=decimal_ratio(qty),
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
    return tuple(trades)


def _pnl(acc: _Acc, mark: Decimal | None) -> tuple[Decimal, Decimal]:
    sold = acc.qty_entry - acc.qty
    if acc.qty_entry == 0 or sold == 0:
        realized = Decimal("0.00")
    else:
        frac = sold / acc.qty_entry
        realized = quantize_cents(
            Fraction(acc.exit_notional - acc.sell_commission)
            - Fraction(acc.entry_notional) * frac
            - Fraction(acc.buy_commission) * frac
        )
    if acc.qty == 0 or mark is None:
        unrealized = Decimal("0.00")
    else:
        unrealized = quantize_cents(acc.qty * Fraction(mark) - acc.cost)
    return realized, unrealized


def _span(
    flags: AssetFlags, grid: Sequence[int], start: int, end: int
) -> tuple[int, int, int]:
    synthetic = missing = outage = 0
    for index, open_time in enumerate(grid):
        if open_time < start or open_time > end:
            continue
        if flags.synthetic[index]:
            synthetic += 1
        if flags.missing[index]:
            missing += 1
        if flags.outage[index]:
            outage += 1
    return synthetic, missing, outage
