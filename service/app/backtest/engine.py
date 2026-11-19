"""Run canonical IR on bars. One user, one book, no look-ahead.

The clock is the bar grid from the first open time through the last. A
missing slot is a hole: indicators see NaN there, rules do not fill, and
nothing is copied forward from the previous close. A bar flagged
``synthetic`` is a no-trade minute. Its close may feed indicators, and a
market order on that bar is skipped. It is not queued for a later real bar.

Fills happen at that bar's close (``fill_price_mode=on_close``) after
slippage. The book is long-only spot: cash plus open longs, in USDC cents.
``risk`` on an entry is ignored. Take-profit and stop-loss are a later task.

The decision for bar ``i`` reads bars ``0..i`` only. Indicator functions are
the causal library in ``app.indicators``, called on the grid with holes left
as NaN. A later bar cannot change an earlier fill.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, localcontext
from typing import Any

from app.backtest.errors import BacktestError
from app.backtest.eval import EvalContext, EvalMemory, evaluate
from app.indicators import atr, bollinger, ema, macd, rsi, sma, stochastic
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

_CENT = Decimal("0.01")
_BPS = Decimal(10000)
_BUILTINS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True, slots=True)
class Fill:
    """One simulated market fill. ``side`` is buy to open or sell to close a long."""

    open_time: int
    asset_id: str
    side: str
    qty: Decimal
    price: Decimal
    notional: Decimal
    commission: Decimal
    equity_after: Decimal


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
    lints: tuple[str, ...] = ()


@dataclass
class _Position:
    qty: Decimal
    cost: Decimal

    @property
    def entry_price(self) -> Decimal:
        return self.cost / self.qty


@dataclass
class _Book:
    cash: Decimal
    positions: dict[str, _Position]


@dataclass
class _Frame:
    """One asset aligned to the grid. Missing slots are NaN and ``present`` is false."""

    by_time: dict[int, Bar]
    series: dict[str, list[float]]
    present: list[bool]


def run_backtest(document: Mapping[str, Any], bars: Mapping[str, Sequence[Bar]]) -> BacktestResult:
    """Simulate ``document`` on ``bars`` keyed by asset id.

    ``document`` must be valid canonical IR. Bars are that asset's candles
    for ``engine.bar_size``. The run does not fetch data and does not place
    a live order.
    """
    try:
        with localcontext() as ctx:
            # 1e12 capital marked at 1e15 after a 1e-9 fill needs more than
            # the default 28 digits. Anything still outside this precision
            # becomes BacktestError instead of an uncaught Decimal error.
            ctx.prec = 50
            ctx.rounding = ROUND_HALF_EVEN
            return _run_backtest(document, bars)
    except DecimalException as exc:
        raise BacktestError("order amount is outside the range this book can price") from exc


def _run_backtest(document: Mapping[str, Any], bars: Mapping[str, Sequence[Bar]]) -> BacktestResult:
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
    initial = _q(_dec(document["capital"]["initial"]))
    times = [bar.open_time for frame in frames.values() for bar in frame.by_time.values()]
    if not times:
        return BacktestResult((), (), (), initial, initial, ())
    grid = list(range(min(times), max(times) + step, step))
    _align(frames, grid)
    _derive(document["derived_series"], frames)
    lints = _series_lints(document["derived_series"], frames, len(grid))
    return _walk(document, frames, grid, warmup, initial, lints)


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


def _derive(specs: Sequence[Mapping[str, Any]], frames: dict[str, _Frame]) -> None:
    for spec in specs:
        for frame in frames.values():
            source = frame.series.get(spec["source"])
            if source is None:
                raise BacktestError(f"unknown series {spec['source']}")
            frame.series[spec["id"]] = _indicator(spec, source, frame)


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


def _walk(
    document: Mapping[str, Any],
    frames: dict[str, _Frame],
    grid: list[int],
    warmup: int,
    initial: Decimal,
    lints: tuple[str, ...],
) -> BacktestResult:
    book = _Book(cash=initial, positions={})
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
    pairs = _rule_pairs(rules, actions)
    for index, open_time in enumerate(grid):
        _mark_bar(frames, grid, index, marks, last_mark)
        truth = _snapshot(pairs, signals, frames, memory, book, index)
        if index >= warmup:
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
                    )
                )
        curve.append(EquityPoint(open_time, _equity(book, marks), book.cash))
    final_equity = curve[-1].equity if curve else initial
    positions = tuple(
        OpenPosition(asset_id, position.qty, position.entry_price)
        for asset_id, position in book.positions.items()
    )
    return BacktestResult(
        tuple(fills),
        tuple(skipped),
        tuple(curve),
        final_equity,
        book.cash,
        positions,
        lints,
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
    budget = _q(equity * percent / Decimal(100))
    spend = budget if budget <= book.cash else book.cash
    if spend <= 0:
        return "no_cash"
    notional = _q(spend * _BPS / (_BPS + commission_bps))
    if notional <= 0:
        return "no_cash"
    commission = spend - notional
    fill_price = close * (_BPS + slippage_bps) / _BPS
    if fill_price <= 0:
        return "no_cash"
    qty = notional / fill_price
    book.cash -= spend
    if existing is None:
        book.positions[asset_id] = _Position(qty, notional)
    else:
        existing.qty += qty
        existing.cost += notional
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
    qty_exit = existing.qty if percent == Decimal(100) else existing.qty * percent / Decimal(100)
    if qty_exit <= 0 or qty_exit > existing.qty:
        return "no_position"
    fill_price = close * (_BPS - slippage_bps) / _BPS
    if fill_price < 0:
        return "no_position"
    notional = _q(qty_exit * fill_price)
    commission = _q(notional * commission_bps / _BPS)
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
    total = book.cash
    for asset_id, position in book.positions.items():
        price = marks.get(asset_id)
        if price is None:
            continue
        total += position.qty * price
    return _q(total)


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


def _q(amount: Decimal) -> Decimal:
    try:
        return amount.quantize(_CENT, rounding=ROUND_HALF_EVEN)
    except DecimalException as exc:
        raise BacktestError("order amount is outside the range this book can price") from exc
