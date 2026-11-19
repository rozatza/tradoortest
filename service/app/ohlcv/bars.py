"""One OHLCV bar and the checks every ingested bar has to pass.

A failing bar is reported and dropped. No field is rewritten to make the
bar pass. A 1m minute the provider answered with no candle can be stored
later as a flagged synthetic flat bar. That path is explicit. A bad candle
is never turned into one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Every timeframe is a native GeckoTerminal aggregate. See docs/ohlcv.md.
TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

# Values above about 1e154 overflow the Bollinger square and make SMA, EMA,
# and ATR non-finite. Anything above 1e15 is rejected here, before that.
MAX_ABS_VALUE = 1e15

_FIELDS = ("open", "high", "low", "close", "volume")
_PRICES = ("open", "high", "low", "close")


@dataclass(frozen=True, slots=True)
class Bar:
    """One candle. ``open_time`` is the bucket start, unix seconds, UTC."""

    mint: str
    timeframe: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    synthetic: bool = False


@dataclass(frozen=True, slots=True)
class BarRejection:
    """A candle that was not stored, with the rules it failed."""

    mint: str
    timeframe: str
    open_time: int | None
    reasons: tuple[str, ...]


def validate_bar(bar: Bar) -> list[str]:
    """Return every rule the bar breaks. An empty list means it can be stored.

    Rules, all of them, with no repair:

    - ``mint`` is a non-empty string and ``timeframe`` is one of the six.
    - ``open_time`` is an integer on that timeframe's UTC epoch grid.
    - open, high, low, close, and volume are finite and ``<= 1e15``.
    - each price is ``> 0``. Volume may be ``0``.
    - ``high >= max(open, close) >= min(open, close) >= low``.
    - a synthetic bar is flat, volume 0, and ``synthetic`` is true. A real
      bar keeps ``synthetic`` false, including a real bar whose volume is 0.
    """
    reasons: list[str] = []
    if not isinstance(bar.mint, str) or bar.mint == "":
        reasons.append("mint is empty")
    step = TIMEFRAME_SECONDS.get(bar.timeframe)
    if step is None:
        reasons.append("timeframe is not supported")
    elif isinstance(bar.open_time, bool) or not isinstance(bar.open_time, int):
        reasons.append("open_time is not an integer second")
    elif bar.open_time % step != 0:
        reasons.append(f"open_time is not aligned to {bar.timeframe}")

    finite: dict[str, float] = {}
    for name in _FIELDS:
        value = getattr(bar, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            reasons.append(f"{name} is not a real number")
            continue
        if not math.isfinite(value):
            reasons.append(f"{name} is not finite")
            continue
        if value < 0.0:
            reasons.append(f"{name} is negative")
            continue
        if name != "volume" and value == 0.0:
            reasons.append(f"{name} must be greater than 0")
            continue
        if value > MAX_ABS_VALUE:
            reasons.append(f"{name} is above 1e15")
            continue
        finite[name] = float(value)

    if all(name in finite for name in _PRICES):
        body_high = max(finite["open"], finite["close"])
        body_low = min(finite["open"], finite["close"])
        if finite["high"] < body_high:
            reasons.append("high is below max(open, close)")
        if finite["low"] > body_low:
            reasons.append("low is above min(open, close)")
    if not isinstance(bar.synthetic, bool):
        reasons.append("synthetic flag is not a boolean")
    elif bar.synthetic and all(name in finite for name in _FIELDS):
        if any(finite[name] != finite["open"] for name in _PRICES):
            reasons.append("synthetic bar must be flat")
        if finite["volume"] != 0.0:
            reasons.append("synthetic bar must have volume 0")
    return reasons
