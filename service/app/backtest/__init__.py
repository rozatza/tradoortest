"""Deterministic bar backtest. Simulation only: no orders are sent.

See ``docs/backtest.md``. Take-profit and stop-loss are not evaluated here.
"""

from app.backtest.engine import (
    BacktestResult,
    EquityPoint,
    Fill,
    OpenPosition,
    SkippedOrder,
    run_backtest,
)
from app.backtest.errors import BacktestError

__all__ = [
    "BacktestError",
    "BacktestResult",
    "EquityPoint",
    "Fill",
    "OpenPosition",
    "SkippedOrder",
    "run_backtest",
]
