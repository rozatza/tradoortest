"""Closed runner windows for the paper book.

A synthetic bar stays in the series, the same way the backtester reads it.
Its close can feed an indicator. The bar object is still passed through, so
the runner can see the flag and refuse the fill. A missing slot is NaN and
the bar is absent. Nothing here copies a close into a hole.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from app.indicators import atr, bollinger, ema, macd, rsi, sma, stochastic
from app.indicators.errors import IndicatorError
from app.indicators.lints import collect_lints
from app.ohlcv.bars import Bar
from app.runner import BarWindow, make_window

_BUILTINS = ("open", "high", "low", "close", "volume")


class WindowError(ValueError):
    """The window could not be built. The message has no prices."""


def build_window(
    document: Mapping[str, Any],
    asset_ids: Sequence[str],
    bars_by_asset: Mapping[str, Mapping[int, Bar]],
    origin: int,
    open_time: int,
    step: int,
) -> BarWindow:
    """Window for ``open_time`` on a grid that starts at ``origin``.

    Unlike the live scheduler's window, a synthetic bar is not turned into a
    hole. The backtester keeps its close in the indicator inputs and passes
    the flagged bar to the runner, which then skips the fill.
    """
    if step < 1 or open_time < origin or (open_time - origin) % step != 0:
        raise WindowError("bar is not on the strategy grid")
    index = (open_time - origin) // step
    grid = [origin + offset * step for offset in range(index + 1)]
    series: dict[str, dict[str, list[float]]] = {}
    bars_now: dict[str, Bar | None] = {}
    specs = document["derived_series"]
    with collect_lints():
        for asset_id in asset_ids:
            columns = _columns(bars_by_asset[asset_id], grid)
            _derive(specs, columns)
            series[asset_id] = columns
            bars_now[asset_id] = bars_by_asset[asset_id].get(open_time)
    return make_window(index, open_time, series, bars_now)


def _columns(by_time: Mapping[int, Bar], grid: Sequence[int]) -> dict[str, list[float]]:
    columns = {name: [] for name in _BUILTINS}
    for stamp in grid:
        bar = by_time.get(stamp)
        if bar is None:
            for name in _BUILTINS:
                columns[name].append(math.nan)
            continue
        for name in _BUILTINS:
            columns[name].append(float(getattr(bar, name)))
    return columns


def _derive(specs: Sequence[Mapping[str, Any]], columns: dict[str, list[float]]) -> None:
    for spec in specs:
        columns[str(spec["id"])] = _indicator(spec, columns)


def _indicator(spec: Mapping[str, Any], columns: Mapping[str, Sequence[float]]) -> list[float]:
    name = spec["indicator"]
    params = spec["params"]
    source = columns.get(str(spec["source"]))
    if source is None:
        raise WindowError("unknown indicator source")
    high = columns["high"]
    low = columns["low"]
    close = columns["close"]
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
            raise WindowError("unknown indicator")
    except (OverflowError, IndicatorError, KeyError, TypeError, ValueError) as exc:
        raise WindowError("indicator failed") from exc
    for value in values:
        if isinstance(value, float) and math.isinf(value):
            raise WindowError("indicator result is not finite")
    return values
