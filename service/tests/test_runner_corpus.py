"""Wider golden corpus and a seeded property check for the shared runner.

The frozen engine is the one locked in ``test_runner``. ``random`` is used
here only. The runner core still has no clock and no random source.
"""

from __future__ import annotations

import copy
import random
from collections.abc import Mapping, Sequence
from typing import Any

import app.backtest.engine as engine
import test_backtest
from app.backtest import run_backtest
from app.backtest.errors import BacktestError
from app.ir.constants import BAR_SIZES, BUILTIN_SERIES
from app.ir.validate import validate_ir
from app.ohlcv.bars import Bar
from app.runner import initial_state, run, step
from strategy_corpus import (
    bar_pattern_names,
    collect,
    golden_cases,
    scanned_test_files,
)
from test_backtest import _MINT, _START, _bar
from test_runner import _LEGACY, _canonical

# Fixed seed for the property cases. The sequence is the test, not the clock.
_PROPERTY_SEED = 83
_PROPERTY_DOCUMENTS = 50
_PROPERTY_SERIES = 4
_BAR_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}
_CMP = ("gt", "gte", "lt", "lte", "eq", "neq")
_ARITH = ("add", "sub", "mul")


def _copy_bars(bars: Mapping[str, Sequence[Bar]]) -> dict[str, list[Bar]]:
    return {asset_id: list(series) for asset_id, series in bars.items()}


def _error(exc: Exception) -> tuple[str, str, str, str | None, str | None]:
    if isinstance(exc, BacktestError):
        return ("error", type(exc).__name__, exc.message, exc.code, exc.path)
    return ("error", type(exc).__name__, str(exc), None, None)


def _run_new(
    document: dict[str, Any], bars: Mapping[str, Sequence[Bar]]
) -> tuple[tuple[Any, ...], list[tuple[Any, ...]]]:
    recorded: list[tuple[Any, ...]] = []
    real = engine.decide_bar

    def spy(config: Any, state: Any, window: Any, view: Any) -> Any:
        outcome = real(config, state, window, view)
        recorded.append((config, state, window, view, outcome))
        return outcome

    engine.decide_bar = spy
    try:
        try:
            result = run_backtest(copy.deepcopy(document), _copy_bars(bars))
        except Exception as exc:
            return _error(exc), recorded
        return ("ok", _canonical(document, result)), recorded
    finally:
        engine.decide_bar = real


def _run_old(
    document: dict[str, Any], bars: Mapping[str, Sequence[Bar]]
) -> tuple[Any, ...]:
    try:
        result = _LEGACY.run_backtest(copy.deepcopy(document), _copy_bars(bars))
    except Exception as exc:
        return _error(exc)
    return ("ok", _canonical(document, result))


def _steps_match_batch(recorded: list[tuple[Any, ...]]) -> None:
    """One recorded decision per bar equals ``step`` and equals ``run``."""
    if not recorded:
        return
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


def _assert_same(name: str, document: dict[str, Any], bars: Mapping[str, Sequence[Bar]]) -> None:
    old = _run_old(document, bars)
    new, recorded = _run_new(document, bars)
    assert old == new, _repro(name, document, old, new)
    _steps_match_batch(recorded)


def _repro(name: str, document: dict[str, Any], old: object, new: object) -> str:
    import json

    body = json.dumps(document, sort_keys=True)[:1500]
    return f"{name}\nold={old!r}\nnew={new!r}\nir={body}"


def test_strategy_fixture_catalog_covers_every_call_and_example() -> None:
    """A new example file or a new ``_strategy`` call is part of the corpus."""
    corpus = collect()
    assert corpus.example_names == ("bollinger-revert.json", "ema-cross.json", "rsi-cross.json")
    present = {name.split("#", 1)[0] for name, _document in corpus.documents}
    for example in corpus.example_names:
        assert f"schemas/examples/{example}" in present
    assert corpus.uncovered_calls == ()
    assert corpus.string_failures == ()
    assert test_backtest.run_backtest.__module__ == "app.backtest.engine"
    assert engine.run_backtest.__module__ == "app.backtest.engine"
    scanned = {path.name for path in scanned_test_files()}
    assert {"test_backtest.py", "test_metrics.py", "test_jobs.py", "test_charts.py"} <= scanned
    assert any(name.startswith("test_agent") for name in scanned)


def test_bar_patterns_cover_holes_gaps_synthetic_flat_and_edges() -> None:
    names = set(bar_pattern_names())
    assert {
        "empty",
        "single",
        "flat",
        "hole",
        "gap",
        "synthetic",
        "hand_round_trip",
        "hand_oco_gap",
        "hand_oco_after_synthetic",
        "missing",
    } <= names


def test_every_strategy_fixture_matches_the_frozen_engine() -> None:
    """Old path and new path over every collected IR and every bar pattern."""
    cases = golden_cases()
    assert len(cases) > 8
    names = {name.split("#", 1)[0] for name, _document, _bars in cases}
    for example in ("ema-cross.json", "rsi-cross.json", "bollinger-revert.json"):
        assert f"schemas/examples/{example}" in names
    for name, document, bars in cases:
        _assert_same(name, document, bars)


def test_seeded_valid_irs_match_the_frozen_engine_and_step_matches_batch() -> None:
    """50 valid IRs crossed with 4 bar series. Seed 83. 200 cases."""
    rng = random.Random(_PROPERTY_SEED)
    documents = [_random_document(rng, index) for index in range(_PROPERTY_DOCUMENTS)]
    for document in documents:
        assert validate_ir(document) == []
    cases = 0
    for index, document in enumerate(documents):
        for series_index in range(_PROPERTY_SERIES):
            bars = _random_bars(rng, document, series_index)
            _assert_same(f"property-{index}-{series_index}", document, bars)
            cases += 1
    assert cases == _PROPERTY_DOCUMENTS * _PROPERTY_SERIES
    assert cases >= 200


def _random_document(rng: random.Random, index: int) -> dict[str, Any]:
    two = rng.random() < 0.25
    assets = [_asset("sol", "SOL")]
    if two:
        assets.append(_asset("jup", "JUP"))
    numeric = list(BUILTIN_SERIES)
    derived = []
    for series_index in range(rng.randint(0, 2)):
        spec = _indicator(rng, numeric)
        derived.append(spec)
        numeric.append(spec["id"])
    signals = []
    for signal_index in range(rng.randint(1, 3)):
        signals.append(
            {
                "id": f"g{signal_index}",
                "expr": _bool_expr(rng, numeric, [item["id"] for item in signals], 0),
            }
        )
    actions = [_enter(rng, "sol")]
    rules = [{"id": "r_enter_sol", "when": "g0", "do": "enter_sol"}]
    if len(signals) > 1:
        actions.append(_exit(rng, "sol", "exit_sol"))
        rules.append({"id": "r_exit_sol", "when": "g1", "do": "exit_sol"})
    if two and len(signals) > 2:
        actions.append(_enter(rng, "jup", "enter_jup"))
        rules.append({"id": "r_enter_jup", "when": "g2", "do": "enter_jup"})
    slippage = 10000 if rng.random() < 0.05 else rng.choice((0, 5, 10, 25))
    return {
        "ir_version": "1.0",
        "strategy": {"id": f"p{index}", "name": f"Property {index}"},
        "engine": {
            "bar_size": rng.choice(tuple(_BAR_SECONDS)),
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": rng.randint(0, 2),
        },
        "capital": {"initial": rng.choice((1, 100, 10000, 1_000_000)), "currency": "USDC"},
        "costs": {"commission_bps": rng.choice((0, 1, 10, 25)), "slippage_bps": slippage},
        "assets": assets,
        "derived_series": derived,
        "signals": signals,
        "actions": actions,
        "rules": rules,
        "portfolio": {
            "mode": "spot",
            "direction": "long_only",
            "chain": "solana",
            "max_positions": 2 if two else 1,
            "allow_pyramiding": bool(rng.randrange(2)),
            "quote_currency": "USDC",
        },
    }


def _asset(ident: str, symbol: str) -> dict[str, str]:
    return {
        "id": ident,
        "symbol": symbol,
        "chain": "solana",
        "market": "spot",
        "quote": "USDC",
    }


def _indicator(rng: random.Random, numeric: Sequence[str]) -> dict[str, Any]:
    name = rng.choice(("sma", "ema", "rsi", "atr", "macd", "bollinger", "stochastic"))
    period = rng.randint(2, 8)
    spec: dict[str, Any] = {
        "id": f"d{len(numeric)}",
        "kind": "indicator",
        "indicator": name,
        "source": rng.choice(tuple(numeric)),
        "params": {},
    }
    if name in {"sma", "ema", "atr"}:
        spec["params"] = {"period": period}
    elif name == "rsi":
        spec["params"] = {"period": period}
    elif name == "macd":
        spec["params"] = {
            "fast": rng.randint(1, 5),
            "slow": rng.randint(1, 6),
            "signal": rng.randint(1, 4),
        }
        spec["output"] = rng.choice(("macd", "signal", "histogram"))
    elif name == "bollinger":
        spec["params"] = {"period": period, "stddev": rng.choice((0.5, 1, 2, 2.5))}
        spec["output"] = rng.choice(("lower", "middle", "upper"))
    else:
        spec["params"] = {"k_period": period, "d_period": rng.randint(1, 6)}
        spec["output"] = rng.choice(("k", "d"))
    return spec


def _enter(rng: random.Random, asset: str, ident: str | None = None) -> dict[str, Any]:
    return {
        "id": ident or "enter_sol",
        "type": "enter",
        "side": "long",
        "asset": asset,
        "order": {"type": "market"},
        "sizing": {"mode": "percent_equity", "percent": rng.choice((1, 10, 25, 50, 100))},
        "risk": {
            "type": "oco",
            "evaluation": "on_entry_fill",
            "movement": "tighten_only",
            "take_profit": {"mode": "percent", "percent": rng.choice((1, 6, 10, 50, 100))},
            "stop_loss": {"mode": "percent", "percent": rng.choice((1, 3, 5, 10, 50, 99))},
        },
    }


def _exit(rng: random.Random, asset: str, ident: str) -> dict[str, Any]:
    return {
        "id": ident,
        "type": "exit",
        "side": "long",
        "asset": asset,
        "order": {"type": "market"},
        "sizing": {
            "mode": "percent_position",
            "percent": rng.choice((25, 50, 100)),
        },
    }


def _bool_expr(
    rng: random.Random, numeric: Sequence[str], signals: Sequence[str], depth: int
) -> dict[str, Any]:
    kind = rng.randrange(5 if depth < 2 else 2)
    if kind == 0:
        return {"op": "lit", "value": bool(rng.randrange(2))}
    if kind == 1:
        return {
            "op": rng.choice(_CMP),
            "left": _num_expr(rng, numeric, signals, depth + 1),
            "right": _num_expr(rng, numeric, signals, depth + 1),
        }
    if kind == 2 and depth < 2:
        op = rng.choice(("and", "or"))
        return {
            "op": op,
            "left": _bool_expr(rng, numeric, signals, depth + 1),
            "right": _bool_expr(rng, numeric, signals, depth + 1),
        }
    if kind == 3 and depth < 2:
        return {"op": "not", "arg": _bool_expr(rng, numeric, signals, depth + 1)}
    return {
        "op": rng.choice(("cross_above", "cross_below")),
        "args": [
            _num_expr(rng, numeric, signals, depth + 1),
            _num_expr(rng, numeric, signals, depth + 1),
        ],
    }


def _num_expr(
    rng: random.Random, numeric: Sequence[str], signals: Sequence[str], depth: int
) -> dict[str, Any]:
    kind = rng.randrange(4 if depth < 2 else 2)
    if kind == 0:
        return {"op": "lit", "value": rng.choice((0, 1, 2, 10, 50, 100))}
    if kind == 1 or depth >= 2:
        return {"op": "ref", "name": rng.choice(tuple(numeric))}
    if kind == 2:
        return {
            "op": rng.choice(_ARITH),
            "left": _num_expr(rng, numeric, signals, depth + 1),
            "right": _num_expr(rng, numeric, signals, depth + 1),
        }
    if rng.randrange(2) == 0:
        return {"op": "entry_price", "args": []}
    if signals and rng.randrange(2) == 0:
        return {"op": "bars_since", "args": [{"op": "ref", "name": rng.choice(tuple(signals))}]}
    return {"op": "neg", "arg": _num_expr(rng, numeric, signals, depth + 1)}


def _random_bars(
    rng: random.Random, document: Mapping[str, Any], series_index: int
) -> dict[str, list[Bar]]:
    size = str(document["engine"]["bar_size"])
    step = _BAR_SECONDS[size]
    assets = [asset["id"] for asset in document["assets"]]
    if series_index == 0:
        return {asset_id: [] for asset_id in assets}
    if series_index == 1:
        return {asset_id: [_price_bar(rng, 0, step, size, synthetic=False)] for asset_id in assets}
    if series_index == 2:
        bars = [
            _price_bar(rng, 0, step, size, synthetic=False),
            _price_bar(rng, 1, step, size, synthetic=True),
            _price_bar(rng, 3, step, size, synthetic=False),
        ]
        return {asset_id: list(bars) for asset_id in assets}
    length = rng.randint(0, 12)
    built: dict[str, list[Bar]] = {}
    for asset_id in assets:
        series = []
        for index in range(length):
            if length > 2 and rng.random() < 0.2:
                continue
            series.append(
                _price_bar(rng, index, step, size, synthetic=rng.random() < 0.15)
            )
        built[asset_id] = series
    return built


def _price_bar(rng: random.Random, index: int, step: int, size: str, *, synthetic: bool) -> Bar:
    base = rng.uniform(1, 200)
    close = round(base, 4)
    if synthetic:
        return _bar(index * step, close, synthetic=True, timeframe=size)
    opened = round(base * rng.uniform(0.97, 1.03), 4)
    high = round(max(opened, close) * rng.uniform(1, 1.04), 4)
    low = round(min(opened, close) * rng.uniform(0.96, 1), 4)
    low = max(low, 0.0001)
    high = max(high, opened, close)
    return _bar(
        index * step,
        close,
        open_=opened,
        high=high,
        low=low,
        volume=rng.choice((0, 1, 2)),
        timeframe=size,
    )


def test_property_seed_is_fixed_and_bar_sizes_are_real() -> None:
    assert _PROPERTY_SEED == 83
    assert set(_BAR_SECONDS) <= set(BAR_SIZES)
    assert _START % 86400 == 0
    assert _MINT
