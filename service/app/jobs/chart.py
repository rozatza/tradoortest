"""Capped price-chart window for one finished job.

The request body, including every raw candle, is deleted when the job
finishes. Those candles are the caller's series. They are not looked up
again from the OHLCV cache, and they are not fetched. A second read would
not be the series the engine ran. The chart keeps only a window of that
series, in ``job_charts``, until the same retention pass deletes the job.
The window is not written into ``result_json``.

Indicator numbers are the lists returned by ``app.indicators``. This module
does not implement SMA, EMA, RSI, ATR, MACD, Bollinger, or Stochastic.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from decimal import Decimal
from typing import Any

from app.backtest.engine import MAX_GRID_BARS
from app.indicators import atr, bollinger, ema, macd, rsi, sma, stochastic
from app.indicators.lints import collect_lints

# Same IR bar sizes as the engine. A chart time is an engine grid slot.
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

_BUILTINS = ("open", "high", "low", "close", "volume")

# A window the browser can step through. The full grid is not stored.
CHART_SLOT_CAP = 2000

BARS_SCHEMA = "tradoortest.backtest_bars.v1"
INDICATORS_SCHEMA = "tradoortest.backtest_indicators.v1"

_PARTS: ContextVar[tuple[str, str] | None] = ContextVar("tradoortest_job_chart", default=None)


def publish_chart_parts(bars_json: str, indicators_json: str) -> None:
    """Hand the chart documents to the worker that is storing this job."""
    _PARTS.set((bars_json, indicators_json))


def take_chart_parts() -> tuple[str, str] | None:
    """Return the documents published on this thread, and clear them."""
    parts = _PARTS.get()
    _PARTS.set(None)
    return parts


def chart_documents(
    body: Mapping[str, Any],
    result: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the bars document and the indicator document for ``body``.

    ``result`` is read only for trade-log timestamps, which choose the
    window. Nothing is added to ``result``.
    """
    bars_doc, indicator_doc = _unavailable_pair("the price chart could not be built")
    try:
        built = _build(body, result)
    except Exception:
        return bars_doc, indicator_doc
    if built is None:
        return _unavailable_pair("bars unavailable")
    return built


def _unavailable_pair(reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {"schema": BARS_SCHEMA, "available": False, "reason": reason},
        {"schema": INDICATORS_SCHEMA, "available": False, "reason": "indicators unavailable"},
    )


def _build(
    body: Mapping[str, Any],
    result: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    document = body.get("ir")
    raw_bars = body.get("bars")
    if not isinstance(document, Mapping) or not isinstance(raw_bars, Mapping):
        return None
    engine = document.get("engine")
    assets = document.get("assets")
    specs = document.get("derived_series")
    if not isinstance(engine, Mapping) or not isinstance(assets, list):
        return None
    if not isinstance(specs, list):
        specs = []
    bar_size = engine.get("bar_size")
    step = _BAR_SECONDS.get(bar_size) if isinstance(bar_size, str) else None
    if step is None:
        return None
    frames: list[tuple[str, dict[int, Mapping[str, Any]]]] = []
    times: list[int] = []
    for asset in assets:
        if not isinstance(asset, Mapping) or not isinstance(asset.get("id"), str):
            return None
        asset_id = str(asset["id"])
        rows = raw_bars.get(asset_id)
        if not isinstance(rows, list) or not rows:
            return None
        by_time: dict[int, Mapping[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                return None
            stamp = row.get("open_time")
            if isinstance(stamp, bool) or not isinstance(stamp, int):
                return None
            if stamp in by_time:
                return None
            by_time[stamp] = row
            times.append(stamp)
        frames.append((asset_id, by_time))
    if not times:
        return None
    slots = (max(times) - min(times)) // step + 1
    if slots < 1 or slots > MAX_GRID_BARS:
        return None
    grid = list(range(min(times), max(times) + step, step))
    if len(grid) != slots:
        return None
    start, end = _window(slots, _trade_indexes(grid, result.get("trades")), CHART_SLOT_CAP)
    window = grid[start:end]
    asset_docs: list[dict[str, Any]] = []
    columns: dict[str, dict[str, list[float]]] = {}
    for asset_id, by_time in frames:
        candles, rejected, total = _candles(by_time, grid, window)
        asset_docs.append(
            {
                "asset_id": asset_id,
                "total": total,
                "shown": len(candles),
                "rejected": rejected,
                "bars": candles,
            }
        )
        columns[asset_id] = _builtin_columns(by_time, grid)
    bars_doc = {
        "schema": BARS_SCHEMA,
        "available": True,
        "bar_size": bar_size,
        "slots_total": slots,
        "slots_shown": len(window),
        "assets": asset_docs,
    }
    indicator_doc = _indicators(specs, columns, grid, window, slots)
    return bars_doc, indicator_doc


def _candles(
    by_time: Mapping[int, Mapping[str, Any]],
    grid: Sequence[int],
    window: Sequence[int],
) -> tuple[list[dict[str, Any]], int, int]:
    total = sum(1 for stamp in grid if stamp in by_time)
    candles: list[dict[str, Any]] = []
    rejected = 0
    window_set = set(window)
    for stamp in grid:
        row = by_time.get(stamp)
        if row is None or stamp not in window_set:
            continue
        candle = _candle(stamp, row)
        if candle is None:
            rejected += 1
            continue
        candles.append(candle)
    return candles, rejected, total


def _candle(stamp: int, row: Mapping[str, Any]) -> dict[str, Any] | None:
    close = row.get("close")
    if not _is_number(close):
        return None
    try:
        fields = {name: _spell(_field(row, name, close)) for name in _BUILTINS}
    except (TypeError, ValueError):
        return None
    synthetic = row.get("synthetic", False)
    if not isinstance(synthetic, bool):
        return None
    return {
        "open_time": stamp,
        "open": fields["open"],
        "high": fields["high"],
        "low": fields["low"],
        "close": fields["close"],
        "volume": fields["volume"],
        "synthetic": synthetic,
    }


def _field(row: Mapping[str, Any], name: str, close: object) -> object:
    if name in row:
        value = row[name]
    elif name == "volume":
        value = 1
    else:
        value = close
    if not _is_number(value):
        raise ValueError(name)
    return value


def _is_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and (isinstance(value, int) or math.isfinite(value))
    )


# Same grammar as DECIMAL_MARK in web/lib/backtest.ts. ASCII digits only.
_DECIMAL_MARK = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


def _spell(value: object) -> str:
    """Plain decimal text for one finite number. No exponent.

    ``json.dumps`` spells a float under ``1e-4`` as ``2.2e-05``. The chart
    grammar rejects that, so a real BONK price would be dropped. A float is
    ``format(Decimal(repr(value)), "f")`` instead. An integer keeps its digits.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("not a number")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValueError("not finite")
    spelled = format(Decimal(repr(value)), "f")
    if _DECIMAL_MARK.fullmatch(spelled) is None:
        raise ValueError("not a decimal")
    return spelled


def _builtin_columns(
    by_time: Mapping[int, Mapping[str, Any]],
    grid: Sequence[int],
) -> dict[str, list[float]]:
    columns = {name: [] for name in _BUILTINS}
    for stamp in grid:
        row = by_time.get(stamp)
        if row is None:
            for name in _BUILTINS:
                columns[name].append(math.nan)
            continue
        close = row.get("close")
        for name in _BUILTINS:
            try:
                raw = _field(row, name, close)
                columns[name].append(float(raw))
            except (TypeError, ValueError):
                columns[name].append(math.nan)
    return columns


def _indicators(
    specs: Sequence[object],
    columns: dict[str, dict[str, list[float]]],
    grid: Sequence[int],
    window: Sequence[int],
    slots: int,
) -> dict[str, Any]:
    series_docs: list[dict[str, Any]] = []
    try:
        with collect_lints():
            for spec in specs:
                if not isinstance(spec, Mapping):
                    raise ValueError("spec")
                _append_series(series_docs, spec, columns, grid, window, slots)
    except Exception:
        return {
            "schema": INDICATORS_SCHEMA,
            "available": False,
            "reason": "indicators unavailable",
        }
    return {
        "schema": INDICATORS_SCHEMA,
        "available": True,
        "slots_total": slots,
        "slots_shown": len(window),
        "series": series_docs,
    }


def _append_series(
    series_docs: list[dict[str, Any]],
    spec: Mapping[str, Any],
    columns: dict[str, dict[str, list[float]]],
    grid: Sequence[int],
    window: Sequence[int],
    slots: int,
) -> None:
    series_id = spec.get("id")
    name = spec.get("indicator")
    source_name = spec.get("source")
    params = spec.get("params")
    if not isinstance(series_id, str) or not isinstance(name, str):
        raise ValueError("spec")
    if not isinstance(source_name, str) or not isinstance(params, Mapping):
        raise ValueError("spec")
    output = spec.get("output")
    if output is not None and not isinstance(output, str):
        raise ValueError("output")
    for asset_id, frame in columns.items():
        source = frame.get(source_name)
        if source is None:
            raise ValueError("source")
        values = _indicator_values(name, params, output, source, frame)
        frame[series_id] = values
        points, valid = _window_points(grid, window, values)
        series_docs.append(
            {
                "id": series_id,
                "asset_id": asset_id,
                "indicator": name,
                "output": output,
                "total": slots,
                "shown": len(points),
                "valid": valid,
                "rejected": len(points) - valid,
                "points": points,
            }
        )


def _indicator_values(
    name: str,
    params: Mapping[str, Any],
    output: str | None,
    source: Sequence[float],
    frame: Mapping[str, Sequence[float]],
) -> list[float]:
    if name == "sma":
        values = sma(source, int(params["period"]))
    elif name == "ema":
        values = ema(source, int(params["period"]))
    elif name == "rsi":
        values = rsi(source, int(params["period"]))
    elif name == "atr":
        values = atr(frame["high"], frame["low"], frame["close"], int(params["period"]))
    elif name == "macd":
        result = macd(
            source,
            int(params["fast"]),
            int(params["slow"]),
            int(params["signal"]),
        )
        if output not in {"macd", "signal", "histogram"}:
            raise ValueError("output")
        values = list(getattr(result, output))
    elif name == "bollinger":
        result = bollinger(source, int(params["period"]), float(params["stddev"]))
        if output not in {"upper", "middle", "lower"}:
            raise ValueError("output")
        values = list(getattr(result, output))
    elif name == "stochastic":
        result = stochastic(
            frame["high"],
            frame["low"],
            frame["close"],
            int(params["k_period"]),
            int(params["d_period"]),
        )
        if output not in {"k", "d"}:
            raise ValueError("output")
        values = list(getattr(result, output))
    else:
        raise ValueError(name)
    if len(values) != len(source):
        raise ValueError("length")
    return [float(value) for value in values]


def _window_points(
    grid: Sequence[int],
    window: Sequence[int],
    values: Sequence[float],
) -> tuple[list[dict[str, Any]], int]:
    start = grid.index(window[0]) if window else 0
    points: list[dict[str, Any]] = []
    valid = 0
    for offset, stamp in enumerate(window):
        value = values[start + offset]
        if isinstance(value, float) and math.isfinite(value):
            points.append({"open_time": stamp, "value": _spell(value)})
            valid += 1
        else:
            points.append({"open_time": stamp, "value": None})
    return points, valid


def _trade_indexes(grid: Sequence[int], trades: object) -> list[int]:
    if not isinstance(trades, list):
        return []
    wanted: set[int] = set()
    for trade in trades:
        if not isinstance(trade, Mapping):
            continue
        for key in ("entry_time", "exit_time"):
            stamp = _unix(trade.get(key))
            if stamp is not None:
                wanted.add(stamp)
    lookup = {stamp: index for index, stamp in enumerate(grid)}
    return sorted(lookup[stamp] for stamp in wanted if stamp in lookup)


def _unix(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def _window(total: int, indexes: Sequence[int], cap: int) -> tuple[int, int]:
    """Earliest slice of ``cap`` slots that covers the most trade indexes.

    No trade time on the grid keeps the last slice. A series that already
    fits is returned whole.
    """
    if cap < 1:
        raise ValueError("cap")
    if total <= cap:
        return 0, total
    if not indexes:
        return total - cap, total
    best_count = 0
    best_start = indexes[0]
    left = 0
    for right in range(len(indexes)):
        while indexes[right] - indexes[left] >= cap:
            left += 1
        count = right - left + 1
        if count > best_count:
            best_count = count
            best_start = indexes[left]
    if best_start + cap > total:
        best_start = total - cap
    if best_start < 0:
        best_start = 0
    return best_start, best_start + cap
