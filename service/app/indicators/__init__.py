"""Indicator math for derived series and the backtester.

The functions are pure. They do not read the network, a strategy file, or
market data. See `docs/dsl-spec.md` for the formulas and warmup lengths.
Inputs are plain Python floats and ints. See `app.indicators.boundary`.
"""

from app.indicators.boundary import (
    as_float_series,
    as_period,
    floats_from_numpy,
    period_from_numpy,
)
from app.indicators.errors import IndicatorError, IndicatorLint
from app.indicators.library import (
    Bollinger,
    Macd,
    Stochastic,
    atr,
    atr_warmup,
    bollinger,
    bollinger_warmup,
    ema,
    ema_warmup,
    macd,
    macd_warmup,
    rsi,
    rsi_warmup,
    sma,
    sma_warmup,
    stochastic,
    stochastic_warmup,
)

__all__ = [
    "Bollinger",
    "IndicatorError",
    "IndicatorLint",
    "Macd",
    "Stochastic",
    "as_float_series",
    "as_period",
    "atr",
    "atr_warmup",
    "bollinger",
    "bollinger_warmup",
    "ema",
    "ema_warmup",
    "floats_from_numpy",
    "macd",
    "macd_warmup",
    "period_from_numpy",
    "rsi",
    "rsi_warmup",
    "sma",
    "sma_warmup",
    "stochastic",
    "stochastic_warmup",
]
