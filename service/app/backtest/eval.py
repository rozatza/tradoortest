"""Evaluate one canonical signal expression at one bar.

Numbers are IEEE floats, the same domain as the indicator library. An empty
value (warmup NaN, a missing bar, or a division by zero) is ``None``.
Comparisons with ``None`` are false. ``AND`` and ``OR`` evaluate both sides.
``CROSS_*`` is false unless both the previous bar and this bar have finite
values, so a hole breaks a cross instead of pairing two bars that were not
neighbors.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.backtest.errors import BacktestError


@dataclass
class EvalMemory:
    """Cross and bars-since state for one asset. It survives from bar to bar."""

    cross_prev: dict[int, tuple[float | None, float | None]] = field(default_factory=dict)
    since: dict[int, int | None] = field(default_factory=dict)


@dataclass
class EvalContext:
    """One asset at one grid index. ``series`` values are floats, NaN where empty."""

    index: int
    series: Mapping[str, Sequence[float]]
    entry_price: float | None
    memory: EvalMemory


def evaluate(expr: Mapping[str, Any], ctx: EvalContext) -> float | bool | None:
    """Return a float, a bool, or ``None`` when the value is empty."""
    if not isinstance(expr, Mapping):
        raise BacktestError("signal expression is not an object")
    op = expr.get("op")
    if op == "lit":
        return _literal(expr.get("value"))
    if op == "ref":
        return _ref(expr.get("name"), ctx)
    if op == "neg":
        value = _number(evaluate(_child(expr, "arg"), ctx))
        if value is None:
            return None
        return -value
    if op == "not":
        return evaluate(_child(expr, "arg"), ctx) is not True
    if op in {"add", "sub", "mul", "div"}:
        return _arith(op, expr, ctx)
    if op in {"gt", "gte", "lt", "lte", "eq", "neq"}:
        return _compare(op, expr, ctx)
    if op in {"and", "or"}:
        left = evaluate(_child(expr, "left"), ctx) is True
        right = evaluate(_child(expr, "right"), ctx) is True
        return left or right if op == "or" else left and right
    if op in {"cross_above", "cross_below"}:
        return _cross(op, expr, ctx)
    if op == "bars_since":
        return _bars_since(expr, ctx)
    if op == "entry_price":
        return ctx.entry_price
    raise BacktestError("signal expression uses an unknown operator")


def _child(expr: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    child = expr.get(key)
    if not isinstance(child, Mapping):
        raise BacktestError("signal expression is missing a child")
    return child


def _literal(value: object) -> float | bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _ref(name: object, ctx: EvalContext) -> float | None:
    if not isinstance(name, str):
        raise BacktestError("signal expression reference is not a name")
    series = ctx.series.get(name)
    if series is None:
        raise BacktestError(f"unknown series {name}")
    return _number(series[ctx.index])


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _arith(op: str, expr: Mapping[str, Any], ctx: EvalContext) -> float | None:
    left = _number(evaluate(_child(expr, "left"), ctx))
    right = _number(evaluate(_child(expr, "right"), ctx))
    if left is None or right is None:
        return None
    if op == "add":
        return left + right
    if op == "sub":
        return left - right
    if op == "mul":
        return left * right
    if right == 0.0:
        return None
    return left / right


def _compare(op: str, expr: Mapping[str, Any], ctx: EvalContext) -> bool:
    left = _number(evaluate(_child(expr, "left"), ctx))
    right = _number(evaluate(_child(expr, "right"), ctx))
    if left is None or right is None:
        return False
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "eq":
        return left == right
    return left != right


def _cross(op: str, expr: Mapping[str, Any], ctx: EvalContext) -> bool:
    args = expr.get("args")
    if not isinstance(args, list) or len(args) != 2:
        raise BacktestError("cross expression needs two arguments")
    left_expr, right_expr = args
    if not isinstance(left_expr, Mapping) or not isinstance(right_expr, Mapping):
        raise BacktestError("cross expression needs two arguments")
    left = _number(evaluate(left_expr, ctx))
    right = _number(evaluate(right_expr, ctx))
    key = id(expr)
    previous = ctx.memory.cross_prev.get(key, (None, None))
    ctx.memory.cross_prev[key] = (left, right)
    prev_left, prev_right = previous
    if prev_left is None or prev_right is None or left is None or right is None:
        return False
    if op == "cross_above":
        return prev_left <= prev_right and left > right
    return prev_left >= prev_right and left < right


def _bars_since(expr: Mapping[str, Any], ctx: EvalContext) -> float | None:
    args = expr.get("args")
    if not isinstance(args, list) or len(args) != 1 or not isinstance(args[0], Mapping):
        raise BacktestError("bars_since needs one argument")
    is_true = evaluate(args[0], ctx) is True
    key = id(expr)
    previous = ctx.memory.since.get(key)
    if is_true:
        ctx.memory.since[key] = 0
        return 0.0
    if previous is None:
        return None
    count = previous + 1
    ctx.memory.since[key] = count
    return float(count)
