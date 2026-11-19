"""DSL round-trip, canonical IR hash, and the versioned save document."""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path

import pytest

from app.dsl import StrategyError, format_strategy, parse_strategy
from app.ir import (
    SaveError,
    canonical_json,
    canonicalize,
    ir_hash,
    load_document,
    save_document,
    validate_ir,
)
from app.ir.save import save_key_order
from app.ir.validate import REPO_ROOT

EXAMPLES = REPO_ROOT / "schemas" / "examples"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _examples() -> list[Path]:
    return sorted(EXAMPLES.glob("*.json"))


def _permute(value: object, rng: random.Random) -> object:
    if isinstance(value, dict):
        keys = list(value)
        rng.shuffle(keys)
        return {key: _permute(value[key], rng) for key in keys}
    if isinstance(value, list):
        return [_permute(item, rng) for item in value]
    return value


@pytest.mark.parametrize("path", _examples(), ids=lambda path: path.name)
def test_example_round_trip_is_stable(path: Path) -> None:
    original = _load(path)
    text = format_strategy(original)
    parsed = parse_strategy(text)
    assert parsed == canonicalize(original)
    assert validate_ir(parsed) == []
    assert format_strategy(parsed) == text
    assert parse_strategy(format_strategy(parsed)) == parsed
    assert ir_hash(parsed) == ir_hash(original)


def test_signal_names_round_trip_through_the_strategy_file() -> None:
    text = format_strategy(_load(EXAMPLES / "rsi-cross.json"))
    old = "signal rsi_sell = CROSS_BELOW(rsi_14, 70)\n"
    new = (
        "signal rsi_sell = CROSS_BELOW(rsi_14, 70)\n"
        "signal both = rsi_buy AND close > 1\n"
        "signal negated = NOT rsi_buy\n"
        "signal waited = BARS_SINCE(rsi_buy) > 1\n"
    )
    parsed = parse_strategy(text.replace(old, new, 1))
    assert validate_ir(parsed) == []
    again = format_strategy(parsed)
    assert "signal both = rsi_buy AND close > 1" in again
    assert "signal negated = NOT rsi_buy" in again
    assert "signal waited = BARS_SINCE(rsi_buy) > 1" in again
    assert parse_strategy(again) == parsed


def test_strategy_file_unknown_name_and_type_errors_are_positioned() -> None:
    text = format_strategy(_load(EXAMPLES / "rsi-cross.json"))
    unknown = text.replace("CROSS_BELOW(rsi_14, 70)", "nope > 1", 1)
    with pytest.raises(StrategyError) as unknown_err:
        parse_strategy(unknown)
    assert unknown_err.value.code == "unknown_series"
    assert unknown_err.value.offset == unknown.index("nope")
    assert "nope" in unknown_err.value.message

    numeric = text.replace("CROSS_BELOW(rsi_14, 70)", "rsi_buy + 1", 1)
    with pytest.raises(StrategyError) as numeric_err:
        parse_strategy(numeric)
    assert numeric_err.value.code == "type_error"
    assert numeric_err.value.offset == numeric.index("rsi_buy + 1")

    condition = text.replace("CROSS_BELOW(rsi_14, 70)", "close", 1)
    with pytest.raises(StrategyError) as condition_err:
        parse_strategy(condition)
    assert condition_err.value.code == "type_error"
    assert "BOOLEAN" in condition_err.value.message


def test_dsl_values_reach_the_semantic_codes() -> None:
    text = format_strategy(_load(EXAMPLES / "rsi-cross.json"))
    bar = parse_strategy(text.replace("bar_size: 1h", "bar_size: 2m", 1))
    assert any(issue.keyword == "invalid_bar_size" for issue in validate_ir(bar))
    sizing = parse_strategy(text.replace("sizing: 25% equity", "sizing: 0% equity", 1))
    assert any(issue.keyword == "invalid_sizing" for issue in validate_ir(sizing))
    stop = parse_strategy(text.replace("stop_loss: 3%", "stop_loss: 100%", 1))
    assert any(issue.keyword == "insane_stop_loss" for issue in validate_ir(stop))
    take = parse_strategy(text.replace("take_profit: 6%", "take_profit: 0%", 1))
    assert any(issue.keyword == "insane_take_profit" for issue in validate_ir(take))
    exit_only = text.replace("rule enter_on_rsi_buy:\n  when rsi_buy do enter_sol\n", "", 1)
    assert any(issue.keyword == "missing_entry" for issue in validate_ir(parse_strategy(exit_only)))


@pytest.mark.parametrize("path", _examples(), ids=lambda path: path.name)
def test_hash_is_stable_across_key_order(path: Path) -> None:
    original = _load(path)
    digest = ir_hash(original)
    assert ir_hash(original) == digest
    assert canonical_json(original).startswith('{"ir_version":"1.0"')
    rng = random.Random(0)
    for _ in range(20):
        permuted = _permute(original, rng)
        assert ir_hash(permuted) == digest
        assert canonicalize(permuted) == canonicalize(original)
    widened = copy.deepcopy(original)
    widened["actions"][0]["sizing"]["percent"] = float(widened["actions"][0]["sizing"]["percent"])
    widened["capital"]["initial"] = float(widened["capital"]["initial"])
    assert ir_hash(widened) == digest


def test_save_document_is_versioned_and_checks_the_hash() -> None:
    original = _load(EXAMPLES / "ema-cross.json")
    saved = save_document(_permute(original, random.Random(1)))
    assert list(saved) == list(save_key_order())
    assert saved["format"] == "tradoor.strategy"
    assert saved["format_version"] == "1"
    assert saved["ir_version"] == "1.0"
    assert saved["ir"]["ir_version"] == "1.0"
    assert saved["ir_hash"] == ir_hash(original)
    assert load_document(saved) == canonicalize(original)
    assert "user_id" not in saved
    assert "user_id" not in saved["ir"]

    reordered = copy.deepcopy(saved)
    reordered["ir"] = _permute(reordered["ir"], random.Random(2))
    assert load_document(reordered) == canonicalize(original)

    tampered = copy.deepcopy(saved)
    tampered["ir_hash"] = "0" * 64
    with pytest.raises(SaveError) as mismatch:
        load_document(tampered)
    assert mismatch.value.code == "ir_hash_mismatch"

    wrong_version = copy.deepcopy(saved)
    wrong_version["format_version"] = "2"
    with pytest.raises(SaveError) as unsupported:
        load_document(wrong_version)
    assert unsupported.value.code == "unsupported_format"
