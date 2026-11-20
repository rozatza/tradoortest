"""Build one closed runner window from stored bars.

Synthetic bars and holes are empty samples. Their prices are not copied
into the series and the bar object is not placed on the window. Indicator
functions are the shared library. This module does not run the backtester
and does not apply a fill.
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


class SeriesError(ValueError):
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

    A synthetic bar is treated as a hole: the series sample is NaN and the
    window's bar is ``None``. The runner therefore cannot fill on it or arm
    an OCO from it.
    """
    if step < 1 or open_time < origin or (open_time - origin) % step != 0:
        raise SeriesError("bar is not on the strategy grid")
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
            current = bars_by_asset[asset_id].get(open_time)
            if current is None or current.synthetic:
                bars_now[asset_id] = None
            else:
                bars_now[asset_id] = current
    return make_window(index, open_time, series, bars_now)


def _columns(by_time: Mapping[int, Bar], grid: Sequence[int]) -> dict[str, list[float]]:
    columns = {name: [] for name in _BUILTINS}
    for stamp in grid:
        bar = by_time.get(stamp)
        if bar is None or bar.synthetic:
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
        raise SeriesError("unknown indicator source")
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
            raise SeriesError("unknown indicator")
    except (OverflowError, IndicatorError, KeyError, TypeError, ValueError) as exc:
        raise SeriesError("indicator failed") from exc
    for value in values:
        if isinstance(value, float) and math.isinf(value):
            raise SeriesError("indicator result is not finite")
    return values
