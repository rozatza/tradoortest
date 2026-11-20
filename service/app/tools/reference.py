"""``read_authoring_reference`` reads a fixed list of repo documents.

The topic is an enum. There is no path argument. Output is capped and, when
the cap bites, says how many characters were omitted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from app.auth import CurrentUser
from app.expr import MAX_EXPR_DEPTH, MAX_EXPR_SOURCE_LENGTH
from app.ir.constants import BAR_SIZES, BUILTIN_SERIES, INDICATOR_PARAMS, IR_VERSION
from app.ir.validate import REPO_ROOT
from app.tools.errors import ToolError
from app.tools.runtime import ToolRuntime

MAX_REFERENCE_CHARS = 48_000

_EXAMPLES = ("rsi-cross", "ema-cross", "bollinger-revert")
_OUTPUTS = {
    "sma": ("sma",),
    "ema": ("ema",),
    "atr": ("atr",),
    "rsi": ("rsi",),
    "macd": ("macd", "signal", "histogram"),
    "bollinger": ("lower", "middle", "upper"),
    "stochastic": ("k", "d"),
}
_PANES = {
    "sma": "overlay",
    "ema": "overlay",
    "bollinger": "overlay",
    "rsi": "separate",
    "macd": "separate",
    "atr": "separate",
    "stochastic": "separate",
}
_WARMUP = {
    "sma": "period - 1",
    "ema": "period - 1",
    "atr": "period",
    "rsi": "period",
    "macd": "max(fast, slow) - 1 for the line; add signal - 1 for signal and histogram",
    "bollinger": "period - 1",
    "stochastic": "k_period - 1 for k; k_period + d_period - 2 for d",
}
_BOUNDS = {
    "period": {"type": "integer", "minimum": 1, "maximum": 500},
    "fast": {"type": "integer", "minimum": 1, "maximum": 500},
    "slow": {"type": "integer", "minimum": 1, "maximum": 500},
    "signal": {"type": "integer", "minimum": 1, "maximum": 500},
    "stddev": {"type": "number", "exclusive_minimum": 0, "maximum": 10},
    "k_period": {"type": "integer", "minimum": 1, "maximum": 500},
    "d_period": {"type": "integer", "minimum": 1, "maximum": 500},
}


def read_authoring_reference(
    arguments: Mapping[str, Any],
    _user: CurrentUser,
    _runtime: ToolRuntime,
) -> dict[str, Any]:
    topic = str(arguments["topic"])
    example = arguments.get("example")
    if topic == "overview":
        content: dict[str, Any] = {
            "ir_version": IR_VERSION,
            "bar_sizes": list(BAR_SIZES),
            "builtin_series": sorted(BUILTIN_SERIES),
            "indicators": list(INDICATOR_PARAMS),
            "scope": "Solana spot, long only, one user",
        }
        return _body(topic, None, content, False, 0)
    if topic == "dsl":
        text, truncated, omitted = _read("docs/dsl-spec.md")
        return _body(topic, None, {"source": "docs/dsl-spec.md", "text": text}, truncated, omitted)
    if topic == "expressions":
        return _body(topic, None, _expressions(), False, 0)
    if topic == "indicators":
        catalog = [_indicator(name) for name in INDICATOR_PARAMS]
        return _body(topic, None, {"indicators": catalog}, False, 0)
    if topic == "bar_sizes":
        return _body(
            topic,
            None,
            {"ir_version": IR_VERSION, "bar_sizes": list(BAR_SIZES)},
            False,
            0,
        )
    if topic == "ir_schema":
        text, truncated, omitted = _read("schemas/strategy-ir-v1.schema.json")
        return _body(
            topic,
            None,
            {
                "ir_version": IR_VERSION,
                "source": "schemas/strategy-ir-v1.schema.json",
                "schema_json": text,
            },
            truncated,
            omitted,
        )
    if topic == "examples":
        if example is None:
            listed = [_example_summary(name) for name in _EXAMPLES]
            return _body(topic, None, {"examples": listed}, False, 0)
        text, truncated, omitted = _read(f"schemas/examples/{example}.json")
        return _body(
            topic,
            str(example),
            {
                "source": f"schemas/examples/{example}.json",
                "strategy_name": _strategy_name(text),
                "text": text,
            },
            truncated,
            omitted,
        )
    raise ToolError("validation_error", "topic is not allowlisted", path="$.topic")


def _body(
    topic: str,
    example: str | None,
    content: dict[str, Any],
    truncated: bool,
    omitted: int,
) -> dict[str, Any]:
    return {
        "topic": topic,
        "example": example,
        "truncated": truncated,
        "omitted_chars": omitted,
        "content": content,
    }


def _expressions() -> dict[str, Any]:
    return {
        "max_expr_depth": MAX_EXPR_DEPTH,
        "max_expr_source_length": MAX_EXPR_SOURCE_LENGTH,
        "builtin_series": sorted(BUILTIN_SERIES),
        "arithmetic_ops": ["add", "div", "mul", "neg", "sub"],
        "boolean_ops": ["and", "not", "or"],
        "compare_ops": ["eq", "gt", "gte", "lt", "lte", "neq"],
        "functions": ["bars_since", "cross_above", "cross_below", "entry_price"],
    }


def _indicator(name: str) -> dict[str, Any]:
    params = list(INDICATOR_PARAMS[name])
    bounds = {}
    for param in params:
        bound = dict(_BOUNDS[param])
        if name == "rsi" and param == "period":
            bound["minimum"] = 2
        bounds[param] = bound
    return {
        "name": name,
        "params": params,
        "param_bounds": bounds,
        "outputs": list(_OUTPUTS[name]),
        "pane": _PANES[name],
        "warmup": _WARMUP[name],
        "source": "one builtin series; atr and stochastic still read high, low, and close",
    }


def _example_summary(name: str) -> dict[str, Any]:
    text, _truncated, _omitted = _read(f"schemas/examples/{name}.json")
    return {
        "name": name,
        "source": f"schemas/examples/{name}.json",
        "strategy_name": _strategy_name(text),
    }


def _strategy_name(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise ToolError(
            "service_unavailable",
            "authoring reference is unavailable",
            retryable=True,
        ) from None
    strategy = payload.get("strategy") if isinstance(payload, dict) else None
    name = strategy.get("name") if isinstance(strategy, dict) else None
    if not isinstance(name, str) or name == "":
        raise ToolError(
            "service_unavailable",
            "authoring reference is unavailable",
            retryable=True,
        )
    return name


def _read(relative: str) -> tuple[str, bool, int]:
    path = REPO_ROOT / relative
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise ToolError(
            "service_unavailable",
            "authoring reference is unavailable",
            retryable=True,
        ) from None
    if len(text) <= MAX_REFERENCE_CHARS:
        return text, False, 0
    return text[:MAX_REFERENCE_CHARS], True, len(text) - MAX_REFERENCE_CHARS
