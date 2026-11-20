"""Agent tool registry, schemas, and the tools that do not save a strategy."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from app.auth import LOCAL_USER_ID, CurrentUser
from app.indicators.library import macd_warmup
from app.ir.canonical import ir_hash
from app.ir.constants import BAR_SIZES, INDICATOR_PARAMS, IR_VERSION
from app.ir.depth import MAX_JSON_DEPTH
from app.ir.validate import REPO_ROOT
from app.jobs.queue import MAX_RESULT_TTL_SECONDS
from app.ohlcv.adapters.any_mint import MEMORY_TTL_SECONDS, AnyMintSource
from app.ohlcv.registry import MAJORS
from app.ohlcv.routes import OhlcvRuntime
from app.ohlcv.store import MAX_CACHE_TTL_SECONDS, SqliteBarStore
from app.tools import REGISTRY, TOOL_ORDER
from app.tools.dispatch import Dispatcher
from app.tools.reference import MAX_REFERENCE_CHARS
from app.tools.registry import MUTATES, export_specs
from app.tools.runtime import ToolRuntime
from app.tools.specs import INPUTS, OUTPUTS
from app.tools.strict import MAX_STRING_LENGTH
from test_ohlcv_any_mint import MINT, POOLS, PUMP

_USER = CurrentUser(user_id=LOCAL_USER_ID)
_SOL = "So11111111111111111111111111111111111111112"
_LEAKS = ("traceback", "sqlite", "operationalerror", "/tmp/", "/home/", ".env", "secret")


def _invoke(dispatcher: Dispatcher, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return dispatcher.invoke(name, arguments, _USER)


def _plain() -> Dispatcher:
    return Dispatcher(ToolRuntime())


def _walk(schema: object, path: str = "$") -> None:
    if not isinstance(schema, dict):
        return
    kind = schema.get("type")
    if kind == "object" or (isinstance(kind, list) and "object" in kind):
        assert schema.get("additionalProperties") is False, path
    for key in ("properties", "patternProperties", "$defs"):
        nested = schema.get(key, {})
        if isinstance(nested, dict):
            for name, child in nested.items():
                _walk(child, f"{path}.{name}")
    for key in ("items", "if", "then", "else", "not", "contains"):
        if isinstance(schema.get(key), dict):
            _walk(schema[key], f"{path}/{key}")
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        children = schema.get(key, [])
        if isinstance(children, list):
            for index, child in enumerate(children):
                _walk(child, f"{path}/{key}[{index}]")


def _no_leak(payload: object, *extra: str) -> None:
    text = json.dumps(payload).lower()
    for token in _LEAKS:
        assert token not in text
    for item in extra:
        assert item.lower() not in text


def _source(
    handler: Callable[[httpx.Request], httpx.Response],
    path: Path,
    clock: dict[str, int],
    *,
    max_calls: int = 20,
) -> tuple[OhlcvRuntime, AnyMintSource, SqliteBarStore]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = AnyMintSource(
        client,
        min_interval=0,
        max_calls=max_calls,
        sleep=lambda _seconds: None,
        now=lambda: clock["t"],
    )
    store = SqliteBarStore(path, now=lambda: clock["t"])
    return OhlcvRuntime(store, source, lambda: clock["t"]), source, store


def _count(path: Path, sql: str) -> int:
    with sqlite3.connect(path) as conn:
        row = conn.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def test_registry_lists_the_ten_tools_in_contract_order() -> None:
    assert tuple(spec.name for spec in REGISTRY) == TOOL_ORDER
    assert len(REGISTRY) == 10
    names = [item["name"] for item in export_specs()]
    assert names == list(TOOL_ORDER)
    assert all("handler" not in item for item in export_specs())
    mutating = {"write_strategy", "update_strategy", "backtest_start"}
    for spec in REGISTRY:
        assert spec.mutates_state is (spec.name in mutating)
        assert MUTATES[spec.name] is spec.mutates_state
        Draft202012Validator.check_schema(spec.input_schema)
        Draft202012Validator.check_schema(spec.output_schema)
        _walk(spec.input_schema, spec.name)
        _walk(spec.output_schema, spec.name)
        assert spec.input_schema is INPUTS[spec.name]
        assert spec.output_schema is OUTPUTS[spec.name]


def test_ttl_caps_stay_at_24_hours() -> None:
    assert MAX_RESULT_TTL_SECONDS == 86_400
    assert MAX_CACHE_TTL_SECONDS == 86_400
    assert MEMORY_TTL_SECONDS == 86_400


def test_unknown_tool_and_rejected_argument_shapes() -> None:
    dispatcher = _plain()
    unknown = dispatcher.invoke("nope", {}, _USER)
    assert unknown["ok"] is False
    assert unknown["tool"] == "nope"
    assert unknown["error"]["code"] == "unknown_tool"
    assert unknown["error"]["retryable"] is False
    hidden = dispatcher.invoke("../etc/passwd", {}, _USER)
    assert hidden["tool"] == "unknown"
    assert "passwd" not in json.dumps(hidden)
    extra = _invoke(dispatcher, "add_chart", {"symbol": "SOL", "timeframe": "1h", "user_id": "x"})
    assert extra["error"]["code"] == "validation_error"
    assert extra["error"]["path"] == "$.user_id"
    owner = _invoke(dispatcher, "search_asset", {"query": "SOL", "owner_id": "other"})
    assert owner["error"]["code"] == "validation_error"
    assert owner["error"]["path"] == "$.owner_id"
    boolean = _invoke(
        dispatcher,
        "add_chart",
        {"symbol": "SOL", "timeframe": "1h", "visible_bars": True},
    )
    assert boolean["error"]["code"] == "validation_error"
    missing = _invoke(dispatcher, "add_chart", {"timeframe": "1h"})
    assert missing["error"]["code"] == "validation_error"
    assert missing["error"]["message"] == "arguments do not match the tool schema"


def test_non_finite_nul_depth_and_oversized_arguments_are_rejected() -> None:
    dispatcher = _plain()
    for value in (float("nan"), float("inf"), float("-inf")):
        failed = _invoke(
            dispatcher,
            "add_chart",
            {"symbol": "SOL", "timeframe": "1h", "visible_bars": value},
        )
        assert failed["error"]["code"] == "non_finite_number"
        _no_leak(failed)
    nul = _invoke(dispatcher, "search_asset", {"query": "SO\x00L"})
    assert nul["error"]["code"] == "invalid_string"
    nested: dict[str, Any] = {"query": "SOL"}
    cursor = nested
    for _ in range(MAX_JSON_DEPTH + 2):
        cursor["child"] = {}
        cursor = cursor["child"]
    deep = _invoke(dispatcher, "search_asset", nested)
    assert deep["error"]["code"] == "expr_too_deep"
    huge = _invoke(dispatcher, "search_asset", {"query": "a" * (MAX_STRING_LENGTH + 1)})
    assert huge["error"]["code"] == "payload_too_large"


def test_authoring_reference_is_allowlisted_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher = _plain()
    overview = _invoke(dispatcher, "read_authoring_reference", {"topic": "overview"})
    assert overview["ok"] is True
    assert overview["data"]["content"]["ir_version"] == IR_VERSION
    assert overview["data"]["content"]["bar_sizes"] == list(BAR_SIZES)
    assert overview["data"]["truncated"] is False
    dsl = _invoke(dispatcher, "read_authoring_reference", {"topic": "dsl"})
    assert dsl["data"]["content"]["source"] == "docs/dsl-spec.md"
    assert dsl["data"]["truncated"] is False
    expected = (REPO_ROOT / "docs/dsl-spec.md").read_text(encoding="utf-8")
    assert dsl["data"]["content"]["text"] == expected
    assert str(REPO_ROOT) not in json.dumps(dsl)
    indicators = _invoke(dispatcher, "read_authoring_reference", {"topic": "indicators"})
    catalog = indicators["data"]["content"]["indicators"]
    assert [item["name"] for item in catalog] == list(INDICATOR_PARAMS)
    rsi = next(item for item in catalog if item["name"] == "rsi")
    assert rsi["param_bounds"]["period"]["minimum"] == 2
    listed = _invoke(dispatcher, "read_authoring_reference", {"topic": "examples"})
    names = [item["name"] for item in listed["data"]["content"]["examples"]]
    assert names == ["rsi-cross", "ema-cross", "bollinger-revert"]
    one = _invoke(
        dispatcher,
        "read_authoring_reference",
        {"topic": "examples", "example": "ema-cross"},
    )
    assert one["data"]["content"]["source"] == "schemas/examples/ema-cross.json"
    stray = _invoke(
        dispatcher,
        "read_authoring_reference",
        {"topic": "dsl", "example": "ema-cross"},
    )
    assert stray["error"]["code"] == "validation_error"
    escaped = _invoke(
        dispatcher,
        "read_authoring_reference",
        {"topic": "dsl", "path": "../.env"},
    )
    assert escaped["error"]["code"] == "validation_error"
    assert ".env" not in json.dumps(escaped["error"]["message"])
    monkeypatch.setattr("app.tools.reference.MAX_REFERENCE_CHARS", 24)
    short = _invoke(dispatcher, "read_authoring_reference", {"topic": "ir_schema"})
    assert short["data"]["truncated"] is True
    assert short["data"]["omitted_chars"] > 0
    assert len(short["data"]["content"]["schema_json"]) == 24
    assert MAX_REFERENCE_CHARS == 48_000


def test_chart_and_indicator_directives_are_deterministic() -> None:
    dispatcher = _plain()
    first = _invoke(dispatcher, "add_chart", {"symbol": "sol", "timeframe": "1h"})
    second = _invoke(dispatcher, "add_chart", {"symbol": "SOL", "timeframe": "1h"})
    assert first["ok"] is True
    assert first["data"]["directive_id"] == second["data"]["directive_id"]
    assert first["data"]["directive_type"] == "chart"
    assert first["data"]["directive_version"] == "1.0"
    assert first["data"]["kind"] == "major"
    assert first["data"]["symbol"] == "SOL"
    assert first["data"]["mint"] == _SOL
    mint = _invoke(dispatcher, "add_chart", {"mint": _SOL, "timeframe": "1d"})
    assert mint["data"]["kind"] == "major"
    assert mint["data"]["symbol"] == "SOL"
    unknown = _invoke(dispatcher, "add_chart", {"symbol": "BTC", "timeframe": "1m"})
    assert unknown["error"]["code"] == "unknown_symbol"
    invalid = _invoke(dispatcher, "add_chart", {"mint": "1" * 44, "timeframe": "1m"})
    assert invalid["error"]["code"] == "invalid_mint"
    bad_frame = _invoke(dispatcher, "add_chart", {"symbol": "SOL", "timeframe": "2m"})
    assert bad_frame["error"]["code"] == "validation_error"
    rsi = _invoke(dispatcher, "add_indicator", {"indicator": "rsi", "params": {"period": 14}})
    again = _invoke(dispatcher, "add_indicator", {"indicator": "rsi", "params": {"period": 14}})
    assert rsi["data"]["directive_id"] == again["data"]["directive_id"]
    assert rsi["data"]["pane"] == "separate"
    assert rsi["data"]["outputs"] == ["rsi"]
    assert rsi["data"]["warmup_bars"] == 14
    macd = _invoke(
        dispatcher,
        "add_indicator",
        {"indicator": "macd", "params": {"fast": 26, "slow": 12, "signal": 9}},
    )
    assert macd["ok"] is True
    assert macd["data"]["warmup_bars"] == max(macd_warmup(26, 12, 9))
    assert macd["data"]["pane"] == "separate"
    bands = _invoke(
        dispatcher,
        "add_indicator",
        {"indicator": "bollinger", "params": {"period": 20, "stddev": 2}},
    )
    assert bands["data"]["pane"] == "overlay"
    assert bands["data"]["outputs"] == ["lower", "middle", "upper"]
    for name, params in (
        ("sma", {"period": 10}),
        ("ema", {"period": 10}),
        ("atr", {"period": 14}),
        ("stochastic", {"k_period": 14, "d_period": 3}),
    ):
        made = _invoke(dispatcher, "add_indicator", {"indicator": name, "params": params})
        assert made["ok"] is True, name
    for params in ({"period": 0}, {"period": 501}, {"period": True}, {"period": 14, "fast": 2}):
        rejected = _invoke(dispatcher, "add_indicator", {"indicator": "rsi", "params": params})
        assert rejected["error"]["code"] == "validation_error"
    zero = _invoke(
        dispatcher,
        "add_indicator",
        {"indicator": "bollinger", "params": {"period": 20, "stddev": 0}},
    )
    assert zero["error"]["code"] == "validation_error"
    missing = _invoke(dispatcher, "add_indicator", {"indicator": "macd", "params": {"fast": 12}})
    assert missing["error"]["code"] == "validation_error"
    alien = _invoke(dispatcher, "add_indicator", {"indicator": "vwap", "params": {}})
    assert alien["error"]["code"] == "validation_error"


def test_validate_reports_strategy_diagnostics_without_failing_the_call() -> None:
    dispatcher = _plain()
    document = json.loads((REPO_ROOT / "schemas/examples/rsi-cross.json").read_text())
    valid = _invoke(dispatcher, "validate", {"ir": document})
    assert valid["ok"] is True
    assert valid["data"]["valid"] is True
    assert valid["data"]["ir_version"] == IR_VERSION
    assert valid["data"]["ir_hash"] == ir_hash(valid["data"]["canonical_ir"])
    assert valid["data"]["diagnostics"] == []
    invalid = _invoke(dispatcher, "validate", {"dsl": "not a strategy"})
    assert invalid["ok"] is True
    assert invalid["data"]["valid"] is False
    assert invalid["data"]["ir_hash"] is None
    assert invalid["data"]["diagnostics"]
    both = _invoke(dispatcher, "validate", {"dsl": "x", "ir": document})
    assert both["ok"] is False
    assert both["error"]["code"] == "validation_error"


def test_search_asset_resolves_symbols_without_a_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a major must not open the asset source")

    monkeypatch.setattr(AnyMintSource, "__init__", explode)
    dispatcher = _plain()
    exact = _invoke(dispatcher, "search_asset", {"query": "sol"})
    assert exact["data"]["matches"] == [
        {
            "kind": "major",
            "symbol": "SOL",
            "mint": _SOL,
            "pool": None,
            "cached": False,
            "provider": None,
        }
    ]
    spaced = _invoke(dispatcher, "search_asset", {"query": "  jup  "})
    assert [item["symbol"] for item in spaced["data"]["matches"]] == ["JUP"]
    partial = _invoke(dispatcher, "search_asset", {"query": "j", "limit": 1})
    assert [item["symbol"] for item in partial["data"]["matches"]] == ["JUP"]
    minted = _invoke(dispatcher, "search_asset", {"query": _SOL})
    assert minted["data"]["matches"][0]["symbol"] == "SOL"
    empty = _invoke(dispatcher, "search_asset", {"query": "btc"})
    assert empty["ok"] is True
    assert empty["data"]["matches"] == []
    invalid = _invoke(dispatcher, "search_asset", {"query": "not-a-mint"})
    assert invalid["error"]["code"] == "invalid_mint"
    assert {asset.symbol for asset in MAJORS} >= {"SOL", "JUP", "JTO", "BONK", "WIF"}


def test_search_asset_uses_the_pool_cache_and_never_stores_bars(tmp_path: Path) -> None:
    calls: list[str] = []
    clock = {"t": 1_700_000_000}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json=POOLS)
        return httpx.Response(404, json={"data": []})

    path = tmp_path / "ohlcv.sqlite"
    runtime, source, store = _source(handler, path, clock)
    dispatcher = Dispatcher(ToolRuntime(ohlcv=runtime))
    try:
        found = _invoke(dispatcher, "search_asset", {"query": MINT})
        assert found["ok"] is True
        match = found["data"]["matches"][0]
        assert match["kind"] == "pool"
        assert match["cached"] is False
        assert match["provider"] == "geckoterminal"
        assert match["pool"]["address"] == "PoolHigh"
        assert len(calls) == 1
        cached = _invoke(dispatcher, "search_asset", {"query": MINT})
        assert cached["data"]["matches"][0]["cached"] is True
        assert len(calls) == 1
        clock["t"] += MAX_CACHE_TTL_SECONDS + 5
        refreshed = _invoke(dispatcher, "search_asset", {"query": MINT})
        assert refreshed["data"]["matches"][0]["cached"] is False
        assert len(calls) == 2
        assert _count(path, "SELECT COUNT(*) FROM bars") == 0
        assert _count(path, "SELECT COUNT(*) FROM pool_resolutions") == 1
        _no_leak(found)
    finally:
        store.close()
        source.close()


def test_search_asset_maps_pool_source_failures(tmp_path: Path) -> None:
    def empty(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    def pump(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=PUMP)

    def boom(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"secret": "sk-live-should-not-leak"})

    def limited(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    clock = {"t": 1_700_000_000}
    cases = (
        (empty, 20, "no_pool"),
        (pump, 5, "no_dex_pool_yet"),
        (boom, 5, "service_unavailable"),
        (limited, 1, "rate_limited"),
    )
    for index, (handler, cap, code) in enumerate(cases):
        runtime, source, store = _source(
            handler,
            tmp_path / f"ohlcv-{index}.sqlite",
            clock,
            max_calls=cap,
        )
        dispatcher = Dispatcher(ToolRuntime(ohlcv=runtime))
        try:
            failed = _invoke(dispatcher, "search_asset", {"query": MINT})
        finally:
            store.close()
            source.close()
        assert failed["ok"] is False
        assert failed["error"]["code"] == code
        _no_leak(failed, "sk-live")
        if code in {"service_unavailable", "rate_limited"}:
            assert failed["error"]["retryable"] is True
