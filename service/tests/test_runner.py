"""Shared runner: same results as the frozen engine, one bar at a time.

The frozen module is the backtest engine at
e56685c64857117d6e974614c9d5b645afd8bb52, before the runner existed.
Nothing here calls the network.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import sys
from collections.abc import Mapping
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import app.backtest.engine as engine
from app.backtest import run_backtest
from app.backtest.errors import BacktestError
from app.ir.canonical import ir_hash
from app.ohlcv.bars import Bar
from app.runner import (
    BarWindow,
    BookView,
    OcoLevels,
    RuleAttempt,
    RunnerConfig,
    initial_state,
    make_window,
    run,
    step,
)
from app.runner.core import ClosedSeries, config_from_document
from test_backtest import (
    _FIXTURE,
    _OCO_FIXTURE,
    _START,
    _bar,
    _eq,
    _fixture_bar,
    _gt,
    _risk,
    _strategy,
)

_SHA = "e56685c64857117d6e974614c9d5b645afd8bb52"
_REPO = Path(__file__).resolve().parents[2]
_LEGACY_PATH = Path(__file__).parent / "legacy" / "backtest_engine_e56685c.py"
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "backtest"
_CORE_PATH = _REPO / "service" / "app" / "runner" / "core.py"
_FORBIDDEN = frozenset(
    {
        "socket",
        "sqlite3",
        "pathlib",
        "urllib",
        "http",
        "requests",
        "os",
        "subprocess",
        "random",
        "time",
        "datetime",
        "pickle",
    }
)


def _load_legacy():
    spec = importlib.util.spec_from_file_location(
        "tradoortest_legacy_engine_e56685c", _LEGACY_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("frozen engine could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_LEGACY = _load_legacy()


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _encode(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _canonical(document: dict, result: Any) -> bytes:
    """Trades, metrics, equity, and the IR hash, as UTF-8 JSON with sorted keys."""
    payload = {
        "ir_hash": ir_hash(document),
        "trades": [
            {
                name: _encode(getattr(trade, name))
                for name in (field.name for field in fields(trade))
            }
            for trade in result.trades
        ],
        "metrics": {
            field.name: _encode(getattr(result.metrics, field.name))
            for field in fields(result.metrics)
        },
        "equity_curve": [
            {
                "open_time": point.open_time,
                "equity": format(point.equity, "f"),
                "cash": format(point.cash, "f"),
            }
            for point in result.equity_curve
        ],
        "fills": [
            {name: _encode(getattr(fill, name)) for name in (field.name for field in fields(fill))}
            for fill in result.fills
        ],
        "skipped": [
            {
                "open_time": item.open_time,
                "asset_id": item.asset_id,
                "action_id": item.action_id,
                "reason": item.reason,
            }
            for item in result.skipped
        ],
        "lints": list(result.lints),
        "final_equity": format(result.final_equity, "f"),
        "final_cash": format(result.final_cash, "f"),
        "final_positions": [
            {
                "asset_id": position.asset_id,
                "qty": format(position.qty, "f"),
                "entry_price": format(position.entry_price, "f"),
            }
            for position in result.final_positions
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _round_trip() -> tuple[str, dict, dict[str, list[Bar]]]:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    bars = [
        Bar(
            "SyntheticMint111111111111111111111111111",
            "1m",
            _START + item["offset"],
            item["open"],
            item["high"],
            item["low"],
            item["close"],
            item["volume"],
            False,
        )
        for item in payload["bars"]
    ]
    document = _strategy(
        engine={
            "bar_size": "1m",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 1,
        },
        costs={"commission_bps": 10, "slippage_bps": 5},
        signals=[
            {"id": "go", "expr": _gt("close", 50)},
            {"id": "stop", "expr": _gt("close", 120)},
        ],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 50},
                "risk": _risk(100, 99),
            },
            {
                "id": "exit_sol",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 100},
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    return "hand_round_trip.json", document, {"sol": bars}


def _oco_document() -> tuple[dict, dict]:
    payload = json.loads(_OCO_FIXTURE.read_text(encoding="utf-8"))
    document = _strategy(
        costs=payload["costs"],
        signals=[{"id": "go", "expr": _eq("close", 100)}],
    )
    document["actions"][0]["risk"] = _risk(
        payload["take_profit_percent"], payload["stop_loss_percent"]
    )
    return payload, document


def _cases() -> list[tuple[str, dict, dict[str, list[Bar]]]]:
    """Every JSON file under fixtures/backtest, and every bar set that file names."""
    payload, document = _oco_document()
    entry = _fixture_bar(payload["entry"])
    synthetic = _bar(60, 90, synthetic=True)
    after = _fixture_bar(payload["after_synthetic_bar"])
    oco = {
        "entry": [entry],
        "take_profit": [entry, _fixture_bar(payload["take_profit_bar"])],
        "stop_loss": [entry, _fixture_bar(payload["stop_loss_bar"])],
        "both": [entry, _fixture_bar(payload["both_bar"])],
        "gap": [entry, _fixture_bar(payload["gap_bar"])],
        "after_synthetic_bar": [entry, after],
        "after_synthetic": [entry, synthetic, after],
    }
    cases = [_round_trip()]
    for name, bars in oco.items():
        cases.append((f"hand_oco.json#{name}", document, {"sol": bars}))
    return cases


# sha256 of service/app/backtest/engine.py at e56685c. A CI checkout is shallow,
# so the test locks the bytes instead of calling git show.
_FROZEN_ENGINE_SHA256 = "4bc6fcc2fce84df4dbb0844e10e4e86e82d30e4d895112d4a8482205d2264798"


def test_frozen_engine_is_the_base_sha() -> None:
    """The frozen file is ``engine.py`` at ``_SHA``. A shallow CI checkout has no parent."""
    digest = hashlib.sha256(_LEGACY_PATH.read_bytes()).hexdigest()
    assert digest == _FROZEN_ENGINE_SHA256, _SHA


def test_every_backtest_fixture_file_is_in_the_catalog() -> None:
    found = {path.name for path in _FIXTURE_DIR.glob("*.json")}
    used = {name.split("#", 1)[0] for name, _document, _bars in _cases()}
    assert used == found
    assert found == {"hand_oco.json", "hand_round_trip.json"}


def test_shared_core_matches_the_frozen_engine_on_every_fixture() -> None:
    """Old path and new path: identical trades, metrics, equity curve, and IR hash."""
    for name, document, bars in _cases():
        old_document = copy.deepcopy(document)
        new_document = copy.deepcopy(document)
        old_bars = {asset: list(series) for asset, series in bars.items()}
        new_bars = {asset: list(series) for asset, series in bars.items()}
        old = _LEGACY.run_backtest(old_document, old_bars)
        new = run_backtest(new_document, new_bars)
        old_bytes = _canonical(old_document, old)
        new_bytes = _canonical(new_document, new)
        assert old_bytes == new_bytes, name
        assert ir_hash(old_document) == ir_hash(new_document) == ir_hash(document)


def test_the_same_fixture_run_twice_is_the_same_bytes() -> None:
    for name, document, bars in _cases():
        first = _canonical(document, run_backtest(copy.deepcopy(document), bars))
        second = _canonical(document, run_backtest(copy.deepcopy(document), bars))
        assert first == second, name


def _cross(level: int) -> dict:
    return {
        "op": "cross_above",
        "args": [
            {"op": "ref", "name": "close"},
            {"op": "lit", "value": level},
        ],
    }


def _close_config() -> RunnerConfig:
    document = _strategy(
        signals=[
            {"id": "go", "expr": _cross(10)},
            {
                "id": "priced",
                "expr": {
                    "op": "gt",
                    "left": {"op": "entry_price"},
                    "right": {"op": "lit", "value": 25},
                },
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "add_rule", "when": "priced", "do": "enter_sol"},
        ],
    )
    return config_from_document(document)


def _windows(closes: list[float]) -> list[BarWindow]:
    series = {
        "sol": {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1.0 for _close in closes],
        }
    }
    windows = []
    for index, close in enumerate(closes):
        bar = _bar(60 * index, close)
        windows.append(make_window(index, bar.open_time, series, {"sol": bar}))
    return windows


def test_stepping_one_bar_at_a_time_matches_the_batch() -> None:
    """Cross memory has to move forward. A batch of the same views matches each step."""
    config = _close_config()
    closes = [1.0, 12.0, 13.0]
    windows = _windows(closes)
    views = (
        BookView(),
        BookView((("sol", Decimal("20")),), ()),
        BookView(
            (("sol", Decimal("30")),),
            (("sol", OcoLevels(Decimal("40"), Decimal("10"), _START)),),
        ),
    )
    state = initial_state(config.asset_ids)
    batch = run(config, state, windows, views)
    current = state
    stepped = []
    for window, view in zip(windows, views, strict=True):
        outcome = step(config, current, window, view)
        stepped.append(outcome)
        current = outcome.state
    assert tuple(stepped) == batch
    truths = [
        (item.decision.signals[0].is_true, item.decision.signals[1].is_true) for item in batch
    ]
    # Cross needs the previous bar's memory. Entry price comes from that bar's view.
    assert truths == [(False, False), (True, False), (False, True)]
    assert state.memory[0][1].cross_prev == ()
    assert current.memory[0][1].cross_prev != ()


def test_step_does_not_mutate_the_state_it_was_given() -> None:
    config = _close_config()
    state = initial_state(config.asset_ids)
    window = _windows([1.0, 12.0])[1]
    view = BookView((("sol", Decimal("20")),), ())
    before = state
    outcome = step(config, state, window, view)
    assert state == before
    assert outcome.state is not state
    assert state.memory == before.memory
    assert outcome.state.memory[0][1].cross_prev != ()
    assert outcome.decision.signals[0].is_true is False


def test_a_mutant_that_peeks_at_the_next_bar_fails() -> None:
    """Bar t + 1 is not on the window. A read of it raises, and cannot change the decision.

    The two tails are 1000 and 1. A comparison of that future close with 50 would
    disagree. The decision at t sees the close of 100 on both windows.
    """

    def peek(window: BarWindow) -> float:
        return window.series["sol"]["close"][window.t + 1]

    bar = _bar(0, 100)
    tails = (1000.0, 1.0)
    config = config_from_document(_strategy(signals=[{"id": "go", "expr": _gt("close", 50)}]))
    state = initial_state(config.asset_ids)
    decisions = []
    for tail in tails:
        series = {"sol": {"close": [100.0, tail]}}
        window = make_window(0, bar.open_time, series, {"sol": bar})
        assert len(window.series["sol"]["close"]) == 1
        assert isinstance(window.series["sol"]["close"], ClosedSeries)
        with pytest.raises(IndexError, match="closed window"):
            peek(window)
        with pytest.raises(IndexError, match="closed window"):
            window.series["sol"]["close"][0:2]
        decisions.append(step(config, state, window, BookView()))
    assert tails[0] != tails[1]
    assert decisions[0] == decisions[1]
    attempt = decisions[0].decision.rules[0]
    assert attempt == RuleAttempt("enter_sol", "sol", None, Decimal("100"))


def test_a_window_that_still_holds_the_future_is_rejected() -> None:
    bar = _bar(0, 100)
    window = BarWindow(0, bar.open_time, {"sol": {"close": [100.0, 1.0]}}, {"sol": bar})
    config = config_from_document(_strategy())
    with pytest.raises(BacktestError, match="after 0"):
        step(config, initial_state(("sol",)), window, BookView())


def test_warmup_updates_memory_and_does_not_attempt_an_order() -> None:
    document = _strategy(
        engine={
            "bar_size": "1m",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 1,
        },
        signals=[{"id": "go", "expr": _cross(10)}],
    )
    config = config_from_document(document)
    windows = _windows([1.0, 12.0])
    state = initial_state(config.asset_ids)
    first = step(config, state, windows[0], BookView())
    assert first.decision.oco == ()
    assert first.decision.rules == ()
    assert first.decision.signals[0].is_true is False
    second = step(config, first.state, windows[1], BookView())
    assert second.decision.signals[0].is_true is True
    assert second.decision.rules[0].close == Decimal("12")


def test_a_synthetic_bar_is_a_skip_and_not_an_oco_touch() -> None:
    config = config_from_document(_strategy(signals=[{"id": "go", "expr": _gt("close", 50)}]))
    bar = _bar(0, 90, synthetic=True)
    window = make_window(0, bar.open_time, {"sol": {"close": [90.0]}}, {"sol": bar})
    view = BookView(
        (("sol", Decimal("100")),),
        (("sol", OcoLevels(Decimal("110"), Decimal("95"), _START - 60)),),
    )
    outcome = step(config, initial_state(("sol",)), window, view)
    assert outcome.decision.oco == ()
    assert outcome.decision.rules == (RuleAttempt("enter_sol", "sol", "synthetic_bar", None),)


def test_a_hole_is_a_missing_bar_skip() -> None:
    config = config_from_document(
        _strategy(signals=[{"id": "go", "expr": {"op": "lit", "value": True}}])
    )
    window = make_window(0, _START, {"sol": {"close": [float("nan")]}}, {"sol": None})
    outcome = step(config, initial_state(("sol",)), window, BookView())
    assert outcome.decision.rules == (RuleAttempt("enter_sol", "sol", "missing_bar", None),)


def test_the_backtester_applies_one_step_per_bar_and_matches_the_batch() -> None:
    name, document, bars = _round_trip()
    recorded: list[tuple] = []
    real = engine.decide_bar

    def spy(config, state, window, view):
        outcome = real(config, state, window, view)
        recorded.append((config, state, window, view, outcome))
        return outcome

    engine.decide_bar = spy
    try:
        result = run_backtest(document, bars)
    finally:
        engine.decide_bar = real
    assert recorded, name
    config = recorded[0][0]
    state = initial_state(config.asset_ids)
    windows = []
    views = []
    for seen, prior, window, view, outcome in recorded:
        assert seen is config
        assert prior == state
        assert step(config, state, window, view) == outcome
        windows.append(window)
        views.append(view)
        state = outcome.state
    assert run(config, initial_state(config.asset_ids), windows, views) == tuple(
        item[4] for item in recorded
    )
    assert _canonical(document, result) == _canonical(document, run_backtest(document, bars))


def test_runner_core_has_no_io_and_no_mutable_global() -> None:
    source = _CORE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in _FORBIDDEN
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module.split(".")[0] not in _FORBIDDEN
        elif isinstance(node, ast.Global):
            raise AssertionError("runner core assigns a global")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "print"}
    for node in tree.body:
        assert not isinstance(node, (ast.Assign, ast.AnnAssign))
    assert "Bar(" not in source
    for word in ("wallet", "private_key", "mnemonic", "keypair", "swap", "sign_transaction"):
        assert word not in source
