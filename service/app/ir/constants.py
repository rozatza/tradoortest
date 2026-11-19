"""Shared IR v1 constants."""

from __future__ import annotations

IR_VERSION = "1.0"

BUILTIN_SERIES = frozenset({"open", "high", "low", "close", "volume"})

BAR_SIZES = (
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
)

# Canonical object key order. Expression objects are ordered by operator shape.
DOCUMENT_KEYS = (
    "ir_version",
    "strategy",
    "engine",
    "capital",
    "costs",
    "assets",
    "derived_series",
    "signals",
    "actions",
    "rules",
    "portfolio",
)

INDICATOR_PARAMS = {
    "sma": ("period",),
    "ema": ("period",),
    "atr": ("period",),
    "rsi": ("period",),
    "macd": ("fast", "slow", "signal"),
    "bollinger": ("period", "stddev"),
    "stochastic": ("k_period", "d_period"),
}
