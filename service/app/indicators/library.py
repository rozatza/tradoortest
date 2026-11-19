"""SMA, EMA, RSI, ATR, MACD, Bollinger, and Stochastic.

Outputs have the same length as the input. Bars inside the warmup are NaN.
Nothing is backfilled, forward-filled, or otherwise invented. A non-finite
sample (NaN or an infinity) is a gap.

Warmup length (leading NaNs on a finite series longer than that warmup):

| Output | Leading NaNs |
| --- | --- |
| SMA, EMA, Bollinger upper, middle, and lower | `period - 1` |
| RSI | `period` |
| ATR | `period` |
| MACD line | `max(fast, slow) - 1` |
| MACD signal and histogram | line warmup + `signal - 1` |
| Stochastic %K | `k_period - 1` |
| Stochastic %D | `k_period + d_period - 2` |

Formulas match `docs/dsl-spec.md`. Rolling windows resume after a gap once
the window is entirely finite. EMA, RSI, ATR, and MACD drop their running
state at a gap and seed again from the next finite run.
"""

from __future__ import annotations

import math
import sys
import warnings
from collections.abc import Sequence
from dataclasses import dataclass

from app.indicators.boundary import as_float_series, as_period
from app.indicators.errors import IndicatorError, IndicatorLint


@dataclass(frozen=True, slots=True)
class Macd:
    """MACD line, signal line, and histogram. Each list matches the input length."""

    macd: list[float]
    signal: list[float]
    histogram: list[float]


@dataclass(frozen=True, slots=True)
class Bollinger:
    """Bollinger bands. `middle` is the SMA. `sd` uses population ddof 0."""

    upper: list[float]
    middle: list[float]
    lower: list[float]


@dataclass(frozen=True, slots=True)
class Stochastic:
    """Fast stochastic. `%K` is unsmoothed. `%D` is the SMA of `%K`."""

    k: list[float]
    d: list[float]


def sma(values: Sequence[float], period: int) -> list[float]:
    """Arithmetic mean of the last `period` samples.

    Warmup is `period - 1`. A window that contains a non-finite sample is NaN.
    """
    period = _period(period, "period")
    data = _series(values, "values")
    out = _nan(len(data))
    for start, end in _finite_runs(data):
        if end - start < period:
            continue
        for index in range(start + period - 1, end):
            window = data[index - period + 1 : index + 1]
            out[index] = sum(window) / period
    return out


def sma_warmup(period: int) -> int:
    """Leading NaNs of `sma` on a finite series."""
    return _period(period, "period") - 1


def ema(values: Sequence[float], period: int) -> list[float]:
    """EMA seeded with an SMA, then `ema + α (price - ema)` with `α = 2 / (period + 1)`.

    Warmup is `period - 1`. Period 1 has α = 1, so the series is copied.
    A gap drops the previous EMA. The next seed is the SMA of the next
    `period` finite samples.
    """
    period = _period(period, "period")
    data = _series(values, "values")
    out = _nan(len(data))
    alpha = 2.0 / (period + 1)
    for start, end in _finite_runs(data):
        if end - start < period:
            continue
        prev = sum(data[start : start + period]) / period
        out[start + period - 1] = prev
        for index in range(start + period, end):
            prev = (data[index] - prev) * alpha + prev
            out[index] = prev
    return out


def ema_warmup(period: int) -> int:
    """Leading NaNs of `ema` on a finite series."""
    return _period(period, "period") - 1


def rsi(values: Sequence[float], period: int) -> list[float]:
    """Wilder RSI of close-to-close changes.

    Warmup is `period`. The first averages are the mean gain and mean loss
    over the first `period` changes. Later bars use
    `(prev * (period - 1) + current) / period`.

    RSI holds its last value when a market goes flat after a move. Only a
    fully flat window, where average gain and average loss are both 0, is
    NaN. That call also emits ``IndicatorLint``. TA-Lib returns 0 there.
    A very long flat run would scale both Wilder averages down into the
    subnormal range and drift the ratio toward 50. TA-Lib keeps scaling.
    This function stops scaling once the next flat step would be subnormal
    or would flush a non-zero average to 0, so the last value stays put.
    Average loss 0 with a positive average gain produces 100. The result is
    never infinite. A non-finite price is a gap and ends the run; the next
    value needs `period + 1` new finite prices. A flat window does not reset
    the running averages.

    The strategy schema requires a period of at least 2. Period 1 is defined
    here: one change, then each new bar replaces the averages.
    """
    period = _period(period, "period")
    data = _series(values, "values")
    out = _nan(len(data))
    warned = False
    for start, end in _finite_runs(data):
        if end - start < period + 1:
            continue
        gain_sum = 0.0
        loss_sum = 0.0
        for index in range(start + 1, start + period + 1):
            change = data[index] - data[index - 1]
            if change >= 0.0:
                gain_sum += change
            else:
                loss_sum -= change
        avg_gain = gain_sum / period
        avg_loss = loss_sum / period
        out[start + period] = _rsi_point(avg_gain, avg_loss, warned)
        warned = warned or _flat_rsi(avg_gain, avg_loss)
        for index in range(start + period + 1, end):
            change = data[index] - data[index - 1]
            gain = change if change > 0.0 else 0.0
            loss = -change if change < 0.0 else 0.0
            if gain == 0.0 and loss == 0.0:
                avg_gain, avg_loss = _hold_if_subnormal(avg_gain, avg_loss, period)
            else:
                avg_gain = (avg_gain * (period - 1) + gain) / period
                avg_loss = (avg_loss * (period - 1) + loss) / period
            out[index] = _rsi_point(avg_gain, avg_loss, warned)
            warned = warned or _flat_rsi(avg_gain, avg_loss)
    return out


def rsi_warmup(period: int) -> int:
    """Leading NaNs of `rsi` on a finite series."""
    return _period(period, "period")


def atr(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    period: int,
) -> list[float]:
    """Wilder ATR of true range.

    Warmup is `period`. True range starts at bar 1 (bar 0 has no previous
    close) and is the greatest of `high - low`, `|high - previous close|`,
    and `|low - previous close|`. The first ATR is the SMA of the first
    `period` true ranges. Later bars use
    `(prev * (period - 1) + true_range) / period`.

    `high`, `low`, and `close` must be the same length. A non-finite bar ends
    the run. Period 1 is the true range from index 1 onward.
    """
    period = _period(period, "period")
    highs = _series(high, "high")
    lows = _series(low, "low")
    closes = _series(close, "close")
    _same_length(("high", "low", "close"), (highs, lows, closes))
    out = _nan(len(closes))
    for start, end in _ohlc_runs(highs, lows, closes):
        # The first bar of a run has no previous close inside the run.
        if end - start < period + 1:
            continue
        total = 0.0
        for index in range(start + 1, start + period + 1):
            total += _true_range(highs, lows, closes, index)
        prev = total / period
        out[start + period] = prev
        for index in range(start + period + 1, end):
            prev = (prev * (period - 1) + _true_range(highs, lows, closes, index)) / period
            out[index] = prev
    return out


def atr_warmup(period: int) -> int:
    """Leading NaNs of `atr` on a finite series."""
    return _period(period, "period")


def macd(
    values: Sequence[float],
    fast: int,
    slow: int,
    signal: int,
) -> Macd:
    """MACD line, signal, and histogram from continuous EMAs.

    The line is `EMA(fast) - EMA(slow)`. Both EMAs are seeded from the start
    of the series. `fast` and `slow` are not swapped when `fast` is larger.
    The signal is an EMA of the line. The histogram is the line minus the
    signal, and it is NaN until the signal exists.

    Warmup: the line needs `max(fast, slow) - 1` bars. The signal and the
    histogram need that many plus `signal - 1`.
    """
    fast_period = _period(fast, "fast")
    slow_period = _period(slow, "slow")
    signal_period = _period(signal, "signal")
    data = _series(values, "values")
    fast_ema = ema(data, fast_period)
    slow_ema = ema(data, slow_period)
    line = _nan(len(data))
    for index, (fast_value, slow_value) in enumerate(zip(fast_ema, slow_ema, strict=True)):
        if math.isfinite(fast_value) and math.isfinite(slow_value):
            line[index] = fast_value - slow_value
    signal_line = ema(line, signal_period)
    histogram = _nan(len(data))
    for index, (line_value, signal_value) in enumerate(zip(line, signal_line, strict=True)):
        if math.isfinite(line_value) and math.isfinite(signal_value):
            histogram[index] = line_value - signal_value
    return Macd(line, signal_line, histogram)


def macd_warmup(fast: int, slow: int, signal: int) -> tuple[int, int, int]:
    """Leading NaNs of `(macd, signal, histogram)` on a finite series."""
    fast_period = _period(fast, "fast")
    slow_period = _period(slow, "slow")
    signal_period = _period(signal, "signal")
    line = max(fast_period, slow_period) - 1
    signal_bars = line + signal_period - 1
    return line, signal_bars, signal_bars


def bollinger(values: Sequence[float], period: int, stddev: float) -> Bollinger:
    """Bollinger bands around an SMA, using the population standard deviation.

    Warmup is `period - 1` for every band. `sd` divides by `period` (ddof 0).
    `upper = middle + stddev * sd` and `lower = middle - stddev * sd`.
    A constant window has `sd` 0, so the three bands are equal. `stddev` is
    in `(0, 10]`.
    """
    period = _period(period, "period")
    width = _band_width(stddev)
    data = _series(values, "values")
    middle = sma(data, period)
    upper = _nan(len(data))
    lower = _nan(len(data))
    for index, mid in enumerate(middle):
        if not math.isfinite(mid):
            continue
        window = data[index - period + 1 : index + 1]
        var = sum((sample - mid) ** 2 for sample in window) / period
        sd = math.sqrt(var)
        upper[index] = mid + width * sd
        lower[index] = mid - width * sd
    return Bollinger(upper, middle, lower)


def bollinger_warmup(period: int) -> int:
    """Leading NaNs of each Bollinger band on a finite series."""
    return _period(period, "period") - 1


def stochastic(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    k_period: int,
    d_period: int,
) -> Stochastic:
    """Fast stochastic. `%K` is unsmoothed. `%D` is `SMA(%K, d_period)`.

    `%K` warmup is `k_period - 1`. `%D` warmup is `k_period + d_period - 2`.
    There is no third smoothing period.

    `%K = 100 * (close - lowest low) / (highest high - lowest low)` over the
    last `k_period` bars. When the highest high equals the lowest low, `%K`
    is NaN and the call emits ``IndicatorLint``. TA-Lib returns 0 there. The
    value is not infinite and it is not copied from the previous bar. A
    non-finite sample in the window makes that `%K` NaN. `%D` then follows
    SMA rules, so a NaN `%K` makes that `%D` window NaN too.
    """
    k_length = _period(k_period, "k_period")
    d_length = _period(d_period, "d_period")
    highs = _series(high, "high")
    lows = _series(low, "low")
    closes = _series(close, "close")
    _same_length(("high", "low", "close"), (highs, lows, closes))
    k = _nan(len(closes))
    warned = False
    for index in range(k_length - 1, len(closes)):
        window_high = highs[index - k_length + 1 : index + 1]
        window_low = lows[index - k_length + 1 : index + 1]
        price = closes[index]
        if not (
            math.isfinite(price)
            and all(math.isfinite(sample) for sample in window_high)
            and all(math.isfinite(sample) for sample in window_low)
        ):
            continue
        highest = max(window_high)
        lowest = min(window_low)
        span = highest - lowest
        if span == 0.0:
            if not warned:
                warnings.warn(
                    "Stochastic %K is NaN because the highest high equals the lowest low. "
                    "TA-Lib returns 0 for this zero range.",
                    IndicatorLint,
                    stacklevel=2,
                )
                warned = True
            continue
        k[index] = 100.0 * (price - lowest) / span
    return Stochastic(k, sma(k, d_length))


def stochastic_warmup(k_period: int, d_period: int) -> tuple[int, int]:
    """Leading NaNs of `(%K, %D)` on a finite series."""
    k_length = _period(k_period, "k_period")
    d_length = _period(d_period, "d_period")
    return k_length - 1, k_length + d_length - 2


def _hold_if_subnormal(avg_gain: float, avg_loss: float, period: int) -> tuple[float, float]:
    """Scale a flat step, unless that step would underflow either average.

    Both averages use the same factor, so the ratio is unchanged until one
    of them would become subnormal. Freezing both keeps that ratio. A later
    non-zero change still uses the frozen averages.
    """
    scale = (period - 1) / period
    nxt_gain = avg_gain * scale
    nxt_loss = avg_loss * scale
    if _underflows(avg_gain, nxt_gain) or _underflows(avg_loss, nxt_loss):
        return avg_gain, avg_loss
    return nxt_gain, nxt_loss


def _underflows(previous: float, nxt: float) -> bool:
    return previous != 0.0 and (nxt == 0.0 or abs(nxt) < sys.float_info.min)


def _flat_rsi(avg_gain: float, avg_loss: float) -> bool:
    return avg_gain == 0.0 and avg_loss == 0.0


def _rsi_point(avg_gain: float, avg_loss: float, already_warned: bool) -> float:
    if _flat_rsi(avg_gain, avg_loss):
        if not already_warned:
            warnings.warn(
                "RSI is NaN because average gain and average loss are both 0. "
                "TA-Lib returns 0 for this fully flat window.",
                IndicatorLint,
                stacklevel=3,
            )
        return math.nan
    if avg_loss == 0.0:
        return 100.0
    return 100.0 * avg_gain / (avg_gain + avg_loss)


def _true_range(high: list[float], low: list[float], close: list[float], index: int) -> float:
    previous = close[index - 1]
    return max(
        high[index] - low[index],
        abs(high[index] - previous),
        abs(low[index] - previous),
    )


def _period(value: object, name: str) -> int:
    return as_period(value, name)


def _band_width(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IndicatorError("stddev must be a number greater than 0 and at most 10")
    width = float(value)
    if not math.isfinite(width) or width <= 0.0 or width > 10.0:
        raise IndicatorError("stddev must be a number greater than 0 and at most 10")
    return width


def _series(values: object, name: str) -> list[float]:
    return as_float_series(values, name)


def _same_length(names: tuple[str, ...], series: tuple[list[float], ...]) -> None:
    length = len(series[0])
    for name, data in zip(names, series, strict=True):
        if len(data) != length:
            raise IndicatorError(
                f"{name} length {len(data)} does not match {names[0]} length {length}"
            )


def _nan(length: int) -> list[float]:
    return [math.nan] * length


def _finite_runs(data: Sequence[float]) -> list[tuple[int, int]]:
    """Half-open spans of consecutive finite samples."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(data):
        if math.isfinite(value):
            if start is None:
                start = index
        elif start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(data)))
    return runs


def _ohlc_runs(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
) -> list[tuple[int, int]]:
    """Half-open spans where high, low, and close are all finite."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(len(close)):
        finite = (
            math.isfinite(high[index])
            and math.isfinite(low[index])
            and math.isfinite(close[index])
        )
        if finite:
            if start is None:
                start = index
        elif start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(close)))
    return runs
