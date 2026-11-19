"""Deterministic bar backtest. Simulation only: no orders are sent.

See ``docs/backtest.md`` for fills and ``docs/metrics.md`` for the ratios
and the trade log. An entry OCO pair fills inside the run.
"""

from app.backtest.engine import (
    BacktestResult,
    EquityPoint,
    Fill,
    OpenPosition,
    SkippedOrder,
    move_oco,
    run_backtest,
)
from app.backtest.errors import BacktestError
from app.backtest.log import (
    METRIC_COLUMNS,
    TRADE_COLUMNS,
    metrics_csv,
    metrics_json,
    trade_log_csv,
    trade_log_json,
)
from app.backtest.metrics import Metrics, Trade

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
