"""JSON Schema Draft 2020-12 for every tool argument and result.

Every object schema sets ``additionalProperties`` to false. A nested IR
document is closed at its top level and accepts the canonical keys the
validator already checked. Diff values stay open because they are JSON
values the structural diff already produced.
"""

from __future__ import annotations

from typing import Any

from app.backtest.log import METRIC_COLUMNS, TRADE_COLUMNS
from app.ir.constants import BAR_SIZES, BUILTIN_SERIES, INDICATOR_PARAMS

_TOPICS = (
    "overview",
    "dsl",
    "expressions",
    "indicators",
    "bar_sizes",
    "ir_schema",
    "examples",
)
_EXAMPLES = ("rsi-cross", "ema-cross", "bollinger-revert")
_INDICATORS = tuple(INDICATOR_PARAMS)
_SERIES = tuple(sorted(BUILTIN_SERIES))
_PARAM_KEYS = ("period", "fast", "slow", "signal", "stddev", "k_period", "d_period")
_RSI_MIN = {"rsi": {"period": 2}}


def _closed(
    properties: dict[str, Any],
    required: list[str] | None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required is not None:
        schema["required"] = required
    return schema


def _string(max_length: int, *, min_length: int = 1) -> dict[str, Any]:
    return {"type": "string", "minLength": min_length, "maxLength": max_length}


def _patterned(pattern: str) -> dict[str, Any]:
    """Identifier, hash, or color text.

    ``not`` rejects a newline. That is valid ECMA-262 and closes the gap
    where Python's ``$`` matches before a trailing newline. The pattern
    itself does not use ``\\Z``.
    """
    return {"type": "string", "pattern": pattern, "not": {"pattern": "\\n"}}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [{"type": "null"}, schema]}


def _ir_document() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "patternProperties": {"^[A-Za-z0-9_]+$": {}},
    }


def _diagnostic() -> dict[str, Any]:
    return _closed(
        {
            "code": _string(80),
            "message": _string(200),
            "path": _nullable(_string(200)),
            "line": _nullable({"type": "integer", "minimum": 0, "maximum": 1_000_000}),
            "column": _nullable({"type": "integer", "minimum": 0, "maximum": 1_000_000}),
        },
        ["code", "message", "path", "line", "column"],
    )


def _exactly_one(*names: str) -> list[dict[str, Any]]:
    branches: list[dict[str, Any]] = []
    for name in names:
        others = [{"required": [other]} for other in names if other != name]
        branch: dict[str, Any] = {"required": [name]}
        if len(others) == 1:
            branch["not"] = others[0]
        else:
            branch["not"] = {"anyOf": others}
        branches.append(branch)
    return branches


def _period(minimum: int = 1) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": 500}


def _param_properties() -> dict[str, Any]:
    properties: dict[str, Any] = {key: _period() for key in _PARAM_KEYS if key != "stddev"}
    properties["stddev"] = {"type": "number", "exclusiveMinimum": 0, "maximum": 10}
    return properties


def _indicator_branches() -> list[dict[str, Any]]:
    branches: list[dict[str, Any]] = []
    for name, params in INDICATOR_PARAMS.items():
        properties = {}
        for key in params:
            if key == "stddev":
                properties[key] = {"type": "number", "exclusiveMinimum": 0, "maximum": 10}
            else:
                minimum = _RSI_MIN.get(name, {}).get(key, 1)
                properties[key] = _period(minimum)
        branches.append(
            {
                "if": {
                    "properties": {"indicator": {"const": name}},
                    "required": ["indicator"],
                },
                "then": {
                    "properties": {
                        "params": _closed(properties, list(params)),
                    },
                    "required": ["params"],
                },
            }
        )
    return branches


def _dsl_field() -> dict[str, Any]:
    return _string(100_000)


def _strategy_id() -> dict[str, Any]:
    return _patterned("^[0-9a-f]{32}$")


def _version() -> dict[str, Any]:
    return {"type": "integer", "minimum": 1, "maximum": 1_000_000}


def _hash() -> dict[str, Any]:
    return _patterned("^[0-9a-f]{64}$")


def _iso() -> dict[str, Any]:
    return {"type": "string", "minLength": 20, "maxLength": 32}


def _bar() -> dict[str, Any]:
    return _closed(
        {
            "open_time": {"type": "integer", "minimum": 0, "maximum": 4_000_000_000},
            "open": {"type": "number"},
            "high": {"type": "number"},
            "low": {"type": "number"},
            "close": {"type": "number"},
            "volume": {"type": "number"},
            "synthetic": {"type": "boolean"},
        },
        ["open_time", "close"],
    )


def _bars() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "minProperties": 1,
        "maxProperties": 20,
        "patternProperties": {
            "^[a-z][a-z0-9_]{0,63}$": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5000,
                "items": _bar(),
            }
        },
    }


def _diff_item() -> dict[str, Any]:
    return _closed(
        {
            "op": {"enum": ["add", "remove", "replace"]},
            "path": _string(500),
            "from": {},
            "to": {},
            "value": {},
        },
        ["op", "path"],
    )


def _job_error() -> dict[str, Any]:
    return _closed(
        {
            "code": _string(80),
            "message": _string(500),
            "path": _nullable(_string(200)),
        },
        ["code", "message", "path"],
    )


def _metric_values() -> dict[str, Any]:
    return _closed(
        {name: _nullable(_string(200)) for name in METRIC_COLUMNS},
        list(METRIC_COLUMNS),
    )


def _metric_undefined() -> dict[str, Any]:
    return _closed({name: _string(200) for name in METRIC_COLUMNS}, [])


def _trade() -> dict[str, Any]:
    return _closed(
        {name: _nullable(_string(200)) for name in TRADE_COLUMNS},
        list(TRADE_COLUMNS),
    )


def _fill() -> dict[str, Any]:
    return _closed(
        {
            "open_time": {"type": "integer"},
            "asset_id": _string(64),
            "side": _string(32),
            "leg": _string(32),
            "qty": _string(200),
            "price": _string(200),
            "notional": _string(200),
            "commission": _string(200),
            "equity_after": _string(200),
            "limit_below_low": {"type": "boolean"},
        },
        [
            "open_time",
            "asset_id",
            "side",
            "leg",
            "qty",
            "price",
            "notional",
            "commission",
            "equity_after",
            "limit_below_low",
        ],
    )


def _count() -> dict[str, Any]:
    return {"type": "integer", "minimum": 0}


def _result() -> dict[str, Any]:
    metrics = {
        "values": _metric_values(),
        "undefined": _metric_undefined(),
        "initial_equity": _string(200),
        "final_equity": _string(200),
        "bar_count": _count(),
        "bars_per_year": _count(),
        "grid_bars": _count(),
        "missing_bars": _count(),
        "synthetic_bars": _count(),
        "long_no_trade_bars": _count(),
        "long_no_trade_minutes": _count(),
        "long_no_trade_threshold": _count(),
        "suspect_outage": {"type": "boolean"},
        "suspect_outage_bars": _count(),
        "suspect_outage_minutes": _count(),
        "missing_bars_in_position": _count(),
        "synthetic_bars_in_position": _count(),
        "long_no_trade_bars_in_position": _count(),
        "long_no_trade_minutes_in_position": _count(),
        "suspect_outage_bars_in_position": _count(),
        "suspect_outage_minutes_in_position": _count(),
        "take_profit_limit_below_low": _count(),
    }
    return _closed(
        {
            "schema": {"const": "tradoortest.backtest_result.v1"},
            "final_equity": _string(200),
            "final_cash": _string(200),
            "lints": {"type": "array", "items": _string(500)},
            "metrics": _closed(metrics, list(metrics)),
            "trades": {"type": "array", "items": _trade()},
            "fills": {"type": "array", "items": _fill()},
        },
        ["schema", "final_equity", "final_cash", "lints", "metrics", "trades", "fills"],
    )


def _pool() -> dict[str, Any]:
    return _closed(
        {
            "address": _string(128),
            "dex": _string(80),
            "dex_id": _string(80),
            "quote_token": _string(64),
            "quote_symbol": _nullable(_string(32)),
            "liquidity_usd": _string(80),
            "resolved_at": _iso(),
            "pool_created_at": _nullable({"type": "integer", "minimum": 0}),
        },
        [
            "address",
            "dex",
            "dex_id",
            "quote_token",
            "quote_symbol",
            "liquidity_usd",
            "resolved_at",
            "pool_created_at",
        ],
    )


def _match() -> dict[str, Any]:
    return _closed(
        {
            "kind": {"enum": ["major", "pool"]},
            "symbol": _string(64),
            "mint": _string(64),
            "pool": _nullable(_pool()),
            "cached": {"type": "boolean"},
            "provider": _nullable(_string(32)),
        },
        ["kind", "symbol", "mint", "pool", "cached", "provider"],
    )


def _search_input() -> dict[str, Any]:
    return _closed(
        {
            "query": _string(64),
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        ["query"],
    )


def _search_output() -> dict[str, Any]:
    return _closed(
        {
            "query": _string(64),
            "matches": {"type": "array", "maxItems": 20, "items": _match()},
        },
        ["query", "matches"],
    )


def _reference_input() -> dict[str, Any]:
    schema = _closed(
        {
            "topic": {"enum": list(_TOPICS)},
            "example": {"enum": list(_EXAMPLES)},
        },
        ["topic"],
    )
    schema["allOf"] = [
        {
            "if": {
                "properties": {"topic": {"const": "examples"}},
                "required": ["topic"],
            },
            "else": {"not": {"required": ["example"]}},
        }
    ]
    return schema


def _reference_output() -> dict[str, Any]:
    content = {
        "type": "object",
        "additionalProperties": False,
        "patternProperties": {"^[a-z0-9_]+$": {}},
    }
    return _closed(
        {
            "topic": {"enum": list(_TOPICS)},
            "example": _nullable({"enum": list(_EXAMPLES)}),
            "truncated": {"type": "boolean"},
            "omitted_chars": {"type": "integer", "minimum": 0},
            "content": content,
        },
        ["topic", "example", "truncated", "omitted_chars", "content"],
    )


def _write_input() -> dict[str, Any]:
    schema = _closed({"dsl": _dsl_field(), "ir": _ir_document()}, [])
    schema["oneOf"] = _exactly_one("dsl", "ir")
    return schema


def _saved_output(*, version: dict[str, Any]) -> dict[str, Any]:
    return _closed(
        {
            "strategy_id": _strategy_id(),
            "version": version,
            "name": _string(200),
            "ir_strategy_id": _string(64),
            "ir_hash": _hash(),
            "dsl": {"type": "string"},
            "ir": _ir_document(),
            "status": {"const": "active"},
            "created_at": _iso(),
            "updated_at": _iso(),
        },
        [
            "strategy_id",
            "version",
            "name",
            "ir_strategy_id",
            "ir_hash",
            "dsl",
            "ir",
            "status",
            "created_at",
            "updated_at",
        ],
    )


def _update_input() -> dict[str, Any]:
    schema = _closed(
        {
            "strategy_id": _strategy_id(),
            "mode": {"enum": ["preview", "apply"]},
            "confirm": {"type": "boolean"},
            "expected_version": _version(),
            "expected_ir_hash": _hash(),
            "dsl": _dsl_field(),
            "ir": _ir_document(),
        },
        ["strategy_id"],
    )
    schema["oneOf"] = _exactly_one("dsl", "ir")
    return schema


def _preview_output() -> dict[str, Any]:
    return _closed(
        {
            "mode": {"const": "preview"},
            "mutated": {"const": False},
            "strategy_id": _strategy_id(),
            "from_version": _version(),
            "to_version": {"type": "integer", "minimum": 2, "maximum": 1_000_001},
            "from_ir_hash": _hash(),
            "to_ir_hash": _hash(),
            "changed": {"type": "boolean"},
            "dsl_diff": {"type": "string"},
            "ir_diff": {"type": "array", "items": _diff_item()},
            "candidate_name": _string(200),
            "candidate_ir_strategy_id": _string(64),
        },
        [
            "mode",
            "mutated",
            "strategy_id",
            "from_version",
            "to_version",
            "from_ir_hash",
            "to_ir_hash",
            "changed",
            "dsl_diff",
            "ir_diff",
            "candidate_name",
            "candidate_ir_strategy_id",
        ],
    )


def _apply_output() -> dict[str, Any]:
    return _closed(
        {
            "mode": {"const": "apply"},
            "mutated": {"const": True},
            "strategy_id": _strategy_id(),
            "from_version": _version(),
            "version": {"type": "integer", "minimum": 2, "maximum": 1_000_000},
            "from_ir_hash": _hash(),
            "ir_hash": _hash(),
            "dsl_diff": {"type": "string"},
            "ir_diff": {"type": "array", "items": _diff_item()},
            "dsl": {"type": "string"},
            "ir": _ir_document(),
            "updated_at": _iso(),
        },
        [
            "mode",
            "mutated",
            "strategy_id",
            "from_version",
            "version",
            "from_ir_hash",
            "ir_hash",
            "dsl_diff",
            "ir_diff",
            "dsl",
            "ir",
            "updated_at",
        ],
    )


def _validate_output() -> dict[str, Any]:
    return _closed(
        {
            "valid": {"type": "boolean"},
            "ir_hash": _nullable(_hash()),
            "ir_version": _nullable({"const": "1.0"}),
            "strategy_id": _nullable(_string(64)),
            "name": _nullable(_string(200)),
            "canonical_ir": _nullable(_ir_document()),
            "dsl": _nullable({"type": "string"}),
            "diagnostics": {"type": "array", "maxItems": 20, "items": _diagnostic()},
        },
        [
            "valid",
            "ir_hash",
            "ir_version",
            "strategy_id",
            "name",
            "canonical_ir",
            "dsl",
            "diagnostics",
        ],
    )


def _backtest_input() -> dict[str, Any]:
    schema = _closed(
        {
            "strategy_id": _strategy_id(),
            "version": _version(),
            "dsl": _dsl_field(),
            "ir": _ir_document(),
            "bars": _bars(),
            "long_no_trade_minutes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1_000_000,
            },
            "request_key": _string(200),
        },
        ["bars"],
    )
    schema["oneOf"] = _exactly_one("strategy_id", "dsl", "ir")
    schema["allOf"] = [
        {
            "if": {"required": ["version"]},
            "then": {"required": ["strategy_id"]},
        }
    ]
    return schema


def _job_status_fields() -> dict[str, Any]:
    return {
        "job_id": _strategy_id(),
        "status": {"enum": ["queued", "running", "succeeded", "failed"]},
        "created_at": _iso(),
        "started_at": _nullable(_iso()),
        "finished_at": _nullable(_iso()),
        "expires_at": _iso(),
        "error": _nullable(_job_error()),
    }


def _status_output() -> dict[str, Any]:
    fields = _job_status_fields()
    return _closed(fields, list(fields))


def _start_output() -> dict[str, Any]:
    return _closed(
        {
            "job_id": _strategy_id(),
            "request_key": _string(200),
            "status": {"enum": ["queued", "running", "succeeded", "failed"]},
            "created": {"type": "boolean"},
            "created_at": _iso(),
            "expires_at": _iso(),
        },
        ["job_id", "request_key", "status", "created", "created_at", "expires_at"],
    )


def _get_output() -> dict[str, Any]:
    fields = _job_status_fields()
    fields["ready"] = {"type": "boolean"}
    fields["result"] = {"anyOf": [{"type": "null"}, _result()]}
    schema = _closed(fields, list(fields))
    schema["allOf"] = [
        {
            "if": {"properties": {"ready": {"const": True}}, "required": ["ready"]},
            "then": {"properties": {"result": _result()}, "required": ["result"]},
        },
        {
            "if": {"properties": {"ready": {"const": False}}, "required": ["ready"]},
            "then": {"properties": {"result": {"type": "null"}}, "required": ["result"]},
        },
    ]
    return schema


def _job_id_input() -> dict[str, Any]:
    return _closed({"job_id": _strategy_id()}, ["job_id"])


def _chart_input() -> dict[str, Any]:
    schema = _closed(
        {
            "asset": _string(44),
            "symbol": _string(16),
            "mint": _string(44, min_length=32),
            "timeframe": {"enum": list(BAR_SIZES)},
            "title": _string(80),
            "chart_id": _patterned("^[a-z][a-z0-9_]{0,63}$"),
            "price_scale": {"enum": ["linear", "log"]},
            "visible_bars": {"type": "integer", "minimum": 1, "maximum": 5000},
        },
        ["timeframe"],
    )
    schema["oneOf"] = _exactly_one("asset", "symbol", "mint")
    return schema


def _chart_output() -> dict[str, Any]:
    return _closed(
        {
            "directive_type": {"const": "chart"},
            "directive_version": {"const": "1.0"},
            "directive_id": _hash(),
            "kind": {"enum": ["major", "mint"]},
            "symbol": _string(64),
            "mint": _string(64),
            "timeframe": {"enum": list(BAR_SIZES)},
            "title": _nullable(_string(80)),
            "chart_id": _nullable(_patterned("^[a-z][a-z0-9_]{0,63}$")),
            "price_scale": {"enum": ["linear", "log"]},
            "visible_bars": _nullable({"type": "integer", "minimum": 1, "maximum": 5000}),
        },
        [
            "directive_type",
            "directive_version",
            "directive_id",
            "kind",
            "symbol",
            "mint",
            "timeframe",
            "title",
            "chart_id",
            "price_scale",
            "visible_bars",
        ],
    )


def _indicator_input() -> dict[str, Any]:
    schema = _closed(
        {
            "indicator": {"enum": list(_INDICATORS)},
            "params": _closed(_param_properties(), []),
            "source": {"enum": list(_SERIES)},
            "pane": {"enum": ["overlay", "separate"]},
            "chart_id": _patterned("^[a-z][a-z0-9_]{0,63}$"),
            "label": _string(80),
            "color": _patterned("^#[0-9A-Fa-f]{6}$"),
        },
        ["indicator", "params"],
    )
    schema["allOf"] = _indicator_branches()
    return schema


def _indicator_output() -> dict[str, Any]:
    return _closed(
        {
            "directive_type": {"const": "indicator"},
            "directive_version": {"const": "1.0"},
            "directive_id": _hash(),
            "indicator": {"enum": list(_INDICATORS)},
            "params": {
                "type": "object",
                "additionalProperties": False,
                "patternProperties": {"^[a-z_]+$": {"type": "number"}},
            },
            "source": {"enum": list(_SERIES)},
            "outputs": {
                "type": "array",
                "minItems": 1,
                "items": _string(32),
            },
            "pane": {"enum": ["overlay", "separate"]},
            "warmup_bars": {"type": "integer", "minimum": 0, "maximum": 2000},
            "chart_id": _nullable(_patterned("^[a-z][a-z0-9_]{0,63}$")),
            "label": _nullable(_string(80)),
            "color": _nullable(_patterned("^#[0-9A-Fa-f]{6}$")),
        },
        [
            "directive_type",
            "directive_version",
            "directive_id",
            "indicator",
            "params",
            "source",
            "outputs",
            "pane",
            "warmup_bars",
            "chart_id",
            "label",
            "color",
        ],
    )


DESCRIPTIONS = {
    "search_asset": "Resolve a Solana symbol or SPL mint. Does not return OHLCV bars.",
    "read_authoring_reference": "Return one allowlisted section of the Phase 1 reference.",
    "write_strategy": "Validate DSL or IR and store a new strategy as version 1.",
    "update_strategy": "Preview a strategy diff, or apply it with confirm and expected_version.",
    "validate": "Parse DSL or IR and return the canonical hash, or diagnostics.",
    "backtest_start": "Start an async backtest from a saved strategy or inline DSL or IR.",
    "backtest_status": "Read the status of one backtest job owned by the current user.",
    "backtest_get": "Read a finished backtest result, or the status when it is not ready.",
    "add_chart": "Build a chart directive for a symbol or mint and a bar size. Does not load bars.",
    "add_indicator": "Build an indicator directive from the Phase 1 catalog. Does not load bars.",
}

MUTATES = {
    "search_asset": False,
    "read_authoring_reference": False,
    "write_strategy": True,
    "update_strategy": True,
    "validate": False,
    "backtest_start": True,
    "backtest_status": False,
    "backtest_get": False,
    "add_chart": False,
    "add_indicator": False,
}

INPUTS = {
    "search_asset": _search_input(),
    "read_authoring_reference": _reference_input(),
    "write_strategy": _write_input(),
    "update_strategy": _update_input(),
    "validate": _write_input(),
    "backtest_start": _backtest_input(),
    "backtest_status": _job_id_input(),
    "backtest_get": _job_id_input(),
    "add_chart": _chart_input(),
    "add_indicator": _indicator_input(),
}

OUTPUTS = {
    "search_asset": _search_output(),
    "read_authoring_reference": _reference_output(),
    "write_strategy": _saved_output(version={"const": 1}),
    "update_strategy": {"oneOf": [_preview_output(), _apply_output()]},
    "validate": _validate_output(),
    "backtest_start": _start_output(),
    "backtest_status": _status_output(),
    "backtest_get": _get_output(),
    "add_chart": _chart_output(),
    "add_indicator": _indicator_output(),
}
