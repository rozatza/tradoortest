"""Deterministic bar backtest. Simulation only: no orders are sent.

See ``docs/backtest.md`` for fills and ``docs/metrics.md`` for the ratios
and the trade log. An entry OCO pair fills inside the run.

The engine is not imported here. ``app.runner`` imports ``app.backtest.errors``
and ``app.backtest.eval`` while it is still loading, and the engine imports
``app.runner``. Importing the engine from this module would make
``from app.runner import step`` fail in a fresh process. Names below are
resolved on first use, which is still before any bar is walked.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "METRIC_COLUMNS",
    "TRADE_COLUMNS",
    "BacktestError",
    "BacktestResult",
    "EquityPoint",
    "Fill",
    "Metrics",
    "OpenPosition",
    "SkippedOrder",
    "Trade",
    "metrics_csv",
    "metrics_json",
    "move_oco",
    "run_backtest",
    "trade_log_csv",
    "trade_log_json",
]

_EXPORTS = {
    "BacktestResult": "app.backtest.engine",
    "EquityPoint": "app.backtest.engine",
    "Fill": "app.backtest.engine",
    "OpenPosition": "app.backtest.engine",
    "SkippedOrder": "app.backtest.engine",
    "move_oco": "app.backtest.engine",
    "run_backtest": "app.backtest.engine",
    "BacktestError": "app.backtest.errors",
    "METRIC_COLUMNS": "app.backtest.log",
    "TRADE_COLUMNS": "app.backtest.log",
    "metrics_csv": "app.backtest.log",
    "metrics_json": "app.backtest.log",
    "trade_log_csv": "app.backtest.log",
    "trade_log_json": "app.backtest.log",
    "Metrics": "app.backtest.metrics",
    "Trade": "app.backtest.metrics",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value
