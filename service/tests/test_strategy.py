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


def _rsi_text() -> str:
    return format_strategy(_load(EXAMPLES / "rsi-cross.json"))


def _with_signal(expr: str) -> str:
    text = _rsi_text()
    anchor = "signal rsi_sell = CROSS_BELOW(rsi_14, 70)\n"
    return text.replace(anchor, anchor + f"signal deep = {expr}\n", 1)


def _too_deep(expr: str) -> StrategyError:
    source = _with_signal(expr)
    with pytest.raises(StrategyError) as caught:
        parse_strategy(source)
    error = caught.value
    assert error.code == "expr_too_deep"
    assert error.offset == source.index(expr)
    assert error.line > 1
    assert error.column > 1
    return error


def test_inline_comment_with_a_leading_space_is_ignored() -> None:
    text = _rsi_text()
    edited = text
    for old in ("strategy rsi_cross_sol:", "when rsi_buy do enter_sol", "risk oco:"):
        edited = edited.replace(old, f"{old} # note", 1)
    assert parse_strategy(edited) == parse_strategy(text)


@pytest.mark.parametrize("suffix", [" ", "\t", "\u00a0"])
def test_trailing_whitespace_on_a_header_is_ignored(suffix: str) -> None:
    text = _rsi_text()
    padded = text.replace("engine:", f"engine:{suffix}", 1)
    assert parse_strategy(padded) == parse_strategy(text)


def test_trailing_space_on_every_line_parses() -> None:
    text = _rsi_text()
    padded = "\n".join(f"{line} " for line in text.split("\n"))
    assert parse_strategy(padded) == parse_strategy(text)


def test_trailing_space_on_risk_oco_keeps_the_block() -> None:
    text = _rsi_text()
    padded = text.replace("risk oco:", "risk oco: ", 1)
    parsed = parse_strategy(padded)
    assert parsed == parse_strategy(text)
    assert parsed["actions"][0]["risk"]["evaluation"] == "on_entry_fill"


def test_flat_and_chain_past_the_ir_depth_limit_is_positioned() -> None:
    _too_deep(" AND ".join(["rsi_buy"] * 82))


def test_flat_addition_chain_is_positioned_expr_too_deep() -> None:
    _too_deep("+".join(["1"] * 500) + " > 0")


def test_unary_minus_inside_a_comparison_counts_every_ir_node() -> None:
    _too_deep("-" * 80 + "close > 0")


def test_nested_bars_since_counts_every_ir_node() -> None:
    expr = "close > 0"
    for _ in range(41):
        expr = f"BARS_SINCE({expr}) > 0"
    _too_deep(expr)


def test_and_chain_at_the_ir_depth_limit_round_trips() -> None:
    source = _with_signal(" AND ".join(["rsi_buy"] * 81))
    parsed = parse_strategy(source)
    assert validate_ir(parsed) == []
    assert canonicalize(parsed) == parsed
    formatted = format_strategy(parsed)
    assert parse_strategy(formatted) == parsed


def test_deep_series_formula_is_positioned_expr_too_deep() -> None:
    expr = "+".join(["close"] * 500)
    source = _rsi_text().replace("RSI(close, period=14)", expr, 1)
    with pytest.raises(StrategyError) as caught:
        parse_strategy(source)
    assert caught.value.code == "expr_too_deep"
    assert caught.value.offset == source.index(expr)


def _inject_signal(text: str, expr: str) -> str:
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("signal "):
            lines.insert(index + 1, f"signal extra = {expr}\n")
            return "".join(lines)
    raise AssertionError("example has no signal")


def _file_position(source: str, offset: int) -> tuple[int, int]:
    line = source.count("\n", 0, offset) + 1
    column = offset - source.rfind("\n", 0, offset)
    return line, column


def _parse_error(source: str) -> StrategyError:
    """Every failure from `parse_strategy` is a StrategyError at a file offset."""
    with pytest.raises(StrategyError) as caught:
        parse_strategy(source)
    error = caught.value
    assert error.code
    assert 0 <= error.offset <= len(source)
    assert (error.line, error.column) == _file_position(source, error.offset)
    return error


@pytest.mark.parametrize(
    "expr",
    ["RSI(close, period=14) < 30", "close > EMA(close, period=3)"],
)
def test_inline_indicator_in_a_signal_is_a_file_error(expr: str) -> None:
    source = _inject_signal(_rsi_text(), expr)
    error = _parse_error(source)
    assert error.code == "invalid_expr"
    assert (error.line, error.column) != (1, 1)
    name = "RSI" if expr.startswith("RSI") else "EMA"
    formula = source.index(f"signal extra = {expr}") + len("signal extra = ")
    assert error.offset == formula + expr.index(name)


_BAD_SIGNALS = (
    "RSI(close, period=14) < 30",
    "close > EMA(close, period=3)",
    "CROSS_ABOVE(EMA(close, period=3), close)",
    "BOLLINGER(close, period=20, stddev=2).lower > close",
    "nope",
    "((",
    "close",
    "1" * 1001 + " > 0",
)


@pytest.mark.parametrize("path", _examples(), ids=lambda path: path.name)
@pytest.mark.parametrize("expr", _BAD_SIGNALS)
def test_strategy_errors_keep_a_file_position(path: Path, expr: str) -> None:
    source = _inject_signal(format_strategy(_load(path)), expr)
    error = _parse_error(source)
    assert error.line > 1


def test_series_formula_errors_stay_strategy_errors() -> None:
    text = _rsi_text()
    added = text.replace(
        "series rsi_14 = RSI(close, period=14)\n",
        "series rsi_14 = RSI(close, period=14)\nseries extra = close + 1\n",
        1,
    )
    assert _parse_error(added).code == "invalid_series"
    nested = text.replace(
        "series rsi_14 = RSI(close, period=14)\n",
        "series rsi_14 = RSI(close, period=14)\n"
        "series nested = RSI(EMA(close, period=3), period=14)\n",
        1,
    )
    assert _parse_error(nested).code == "invalid_series"


def test_long_signal_literal_keeps_the_bad_number_code() -> None:
    expr = "1" * 1001 + " > 0"
    source = _rsi_text().replace("CROSS_BELOW(rsi_14, 70)", expr, 1)
    with pytest.raises(StrategyError) as caught:
        parse_strategy(source)
    assert caught.value.code == "bad_number"
    assert caught.value.offset == source.index(expr)


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
