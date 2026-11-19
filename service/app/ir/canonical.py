"""Deterministic canonical form and sha256 for strategy IR v1.

Key order follows the schema's documented field order. Integral floats become
integers. The hash is sha256 over the UTF-8 canonical JSON, with no
insignificant whitespace.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from typing import Any

from app.ir.constants import DOCUMENT_KEYS, INDICATOR_PARAMS

_STRATEGY_KEYS = ("id", "name", "description")
_ENGINE_KEYS = ("bar_size", "event", "fill_price_mode", "warmup_bars")
_CAPITAL_KEYS = ("initial", "currency")
_COSTS_KEYS = ("commission_bps", "slippage_bps")
_ASSET_KEYS = ("id", "symbol", "chain", "market", "quote")
_SERIES_KEYS = ("id", "kind", "indicator", "source", "params", "output")
_SIGNAL_KEYS = ("id", "expr")
_ACTION_KEYS = ("id", "type", "side", "asset", "order", "sizing", "risk")
_ORDER_KEYS = ("type",)
_SIZING_KEYS = ("mode", "percent")
_RISK_KEYS = ("type", "evaluation", "movement", "take_profit", "stop_loss")
_LEVEL_KEYS = ("mode", "percent")
_RULE_KEYS = ("id", "when", "do")
_PORTFOLIO_KEYS = (
    "mode",
    "direction",
    "chain",
    "max_positions",
    "allow_pyramiding",
    "quote_currency",
)

_CMP = frozenset({"gt", "gte", "lt", "lte", "eq", "neq"})
_ARITH = frozenset({"add", "sub", "mul", "div"})
_BOOL = frozenset({"and", "or"})
_CALLS = frozenset({"cross_above", "cross_below", "bars_since", "entry_price"})

# Floats past this magnitude are not exact integers. Leave them as floats.
_SAFE_INT = 2**53


def canonical_number(value: int | float) -> int | float:
    """Return the canonical number for a JSON numeric value.

    Booleans are rejected. A finite float with no fractional part, inside the
    safe integer range, becomes an int so `25` and `25.0` hash the same.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("canonical number must be an int or float")
    if isinstance(value, int):
        return value
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    if value.is_integer() and abs(value) <= _SAFE_INT:
        return int(value)
    return value


def format_number(value: int | float) -> str:
    """DSL spelling of a canonical number. Integers have no decimal point."""
    number = canonical_number(value)
    if isinstance(number, int):
        return str(number)
    text = format(number, ".16g")
    if "e" not in text and "." not in text:
        text += ".0"
    return text


def canonicalize(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a new document with stable key order and canonical numbers."""
    if not isinstance(document, Mapping):
        raise TypeError("strategy IR must be a JSON object")
    return _ordered(
        document,
        DOCUMENT_KEYS,
        {
            "strategy": lambda value: _ordered(value, _STRATEGY_KEYS),
            "engine": lambda value: _ordered(value, _ENGINE_KEYS),
            "capital": lambda value: _ordered(value, _CAPITAL_KEYS),
            "costs": lambda value: _ordered(value, _COSTS_KEYS),
            "assets": lambda value: [_ordered(item, _ASSET_KEYS) for item in value],
            "derived_series": lambda value: [_series(item) for item in value],
            "signals": lambda value: [_signal(item) for item in value],
            "actions": lambda value: [_action(item) for item in value],
            "rules": lambda value: [_ordered(item, _RULE_KEYS) for item in value],
            "portfolio": lambda value: _ordered(value, _PORTFOLIO_KEYS),
        },
    )


def canonical_json(document: Mapping[str, Any]) -> str:
    """Canonical JSON text. Key order is significant; whitespace is not."""
    return json.dumps(
        canonicalize(document),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def ir_hash(document: Mapping[str, Any]) -> str:
    """Lowercase sha256 hex digest of `canonical_json(document)`."""
    payload = canonical_json(document).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _series(series: Mapping[str, Any]) -> dict[str, Any]:
    indicator = series.get("indicator")
    order = INDICATOR_PARAMS.get(indicator, ())

    def params(value: Mapping[str, Any]) -> dict[str, Any]:
        return _ordered(value, order)

    return _ordered(series, _SERIES_KEYS, {"params": params})


def _signal(signal: Mapping[str, Any]) -> dict[str, Any]:
    return _ordered(signal, _SIGNAL_KEYS, {"expr": _expr})


def _action(action: Mapping[str, Any]) -> dict[str, Any]:
    return _ordered(
        action,
        _ACTION_KEYS,
        {
            "order": lambda value: _ordered(value, _ORDER_KEYS),
            "sizing": lambda value: _ordered(value, _SIZING_KEYS),
            "risk": _risk,
        },
    )


def _risk(risk: Mapping[str, Any]) -> dict[str, Any]:
    return _ordered(
        risk,
        _RISK_KEYS,
        {
            "take_profit": lambda value: _ordered(value, _LEVEL_KEYS),
            "stop_loss": lambda value: _ordered(value, _LEVEL_KEYS),
        },
    )


def _expr(node: Any) -> Any:
    if not isinstance(node, Mapping):
        return _scalar(node)
    op = node.get("op")
    if op == "lit":
        return _ordered(node, ("op", "value"))
    if op == "ref":
        return _ordered(node, ("op", "name"))
    if op in {"not", "neg"}:
        return _ordered(node, ("op", "arg"), {"arg": _expr})
    if op in _CMP or op in _ARITH or op in _BOOL:
        return _ordered(node, ("op", "left", "right"), {"left": _expr, "right": _expr})
    if op in _CALLS:
        return _ordered(
            node,
            ("op", "args"),
            {"args": lambda args: [_expr(arg) for arg in args]},
        )
    return _ordered(node, ())


def _ordered(
    data: Mapping[str, Any],
    keys: tuple[str, ...],
    nested: Mapping[str, Callable[[Any], Any]] | None = None,
) -> dict[str, Any]:
    nested = nested or {}
    result: dict[str, Any] = {}
    for key in keys:
        if key not in data:
            continue
        value = data[key]
        transform = nested.get(key)
        result[key] = transform(value) if transform is not None else _scalar(value)
    for key in sorted(data):
        if key in result:
            continue
        value = data[key]
        transform = nested.get(key)
        result[key] = transform(value) if transform is not None else _scalar(value)
    return result


def _scalar(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _scalar(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_scalar(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return canonical_number(value)
    return value
