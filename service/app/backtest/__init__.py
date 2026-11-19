"""Deterministic bar backtest. Simulation only: no orders are sent.

See ``docs/backtest.md``. An entry OCO pair fills inside the run.
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

__all__ = [
    "BacktestError",
    "BacktestResult",
    "EquityPoint",
    "Fill",
    "OpenPosition",
    "SkippedOrder",
    "move_oco",
    "run_backtest",
]
