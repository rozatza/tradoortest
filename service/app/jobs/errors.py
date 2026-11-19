"""Coded failures for the backtest job API.

Every code here is returned as HTTP 4xx. An unexpected failure is still a
coded 422 on the result, not a 500, and the message does not include the
exception text.
"""

from __future__ import annotations

from typing import Any

from app.backtest.errors import BacktestError
from app.dsl.errors import StrategyError
from app.expr.errors import ExprError
from app.indicators.errors import IndicatorError
from app.ir.depth import JsonShapeError
from app.ir.save import SaveError


def job_error(exc: BaseException) -> dict[str, Any]:
    """Machine-readable error for a failed job or a rejected request."""
    if isinstance(exc, BacktestError):
        return {"code": exc.code, "message": exc.message, "path": exc.path}
    if isinstance(exc, IndicatorError):
        return {"code": exc.code, "message": str(exc), "path": None}
    if isinstance(exc, OverflowError):
        return {
            "code": "indicator_overflow",
            "message": "indicator overflowed",
            "path": None,
        }
    if isinstance(exc, RecursionError):
        return {
            "code": "expr_too_deep",
            "message": "expression nested too deeply",
            "path": None,
        }
    if isinstance(exc, JsonShapeError):
        return {"code": exc.code, "message": exc.message, "path": exc.path}
    if isinstance(exc, (StrategyError, ExprError)):
        return {
            "code": exc.code or "invalid_strategy",
            "message": exc.message,
            "path": exc.path,
        }
    if isinstance(exc, SaveError):
        return {"code": exc.code, "message": exc.message, "path": None}
    if isinstance(exc, UnicodeEncodeError):
        return {
            "code": "invalid_string",
            "message": "lone surrogate is not valid text",
            "path": None,
        }
    return {
        "code": "backtest_error",
        "message": "the backtest could not finish",
        "path": None,
    }
