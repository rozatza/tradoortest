"""Tokenizer, parser, types, and IR conversion for the expression mini-language."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from app.expr import (
    Call,
    ExprError,
    ExprType,
    IndicatorCall,
    Lit,
    Ref,
    TokenKind,
    format_expr,
    from_ir,
    parse,
    parse_condition,
    parse_numeric,
    to_ir,
    tokenize,
)
from app.ir.validate import REPO_ROOT, load_schema

EXAMPLES = REPO_ROOT / "schemas" / "examples"
EXPR_PKG = Path(__file__).resolve().parents[1] / "app" / "expr"

# Text form of every signal expression in the three example strategies.
SIGNAL_TEXT = {
    "rsi-cross.json": {
        "rsi_buy": "CROSS_ABOVE(rsi_14, 30)",
        "rsi_sell": "CROSS_BELOW(rsi_14, 70)",
    },
    "ema-cross.json": {
        "ema_buy": "CROSS_ABOVE(ema_fast, ema_slow)",
        "ema_sell": "CROSS_BELOW(ema_fast, ema_slow)",
    },
    "bollinger-revert.json": {
        "revert_buy": "CROSS_BELOW(close, bb_lower)",
        "revert_exit": "CROSS_ABOVE(close, bb_middle)",
    },
}


def _signals() -> list[tuple[str, str, dict]]:
    rows: list[tuple[str, str, dict]] = []
    for filename, texts in SIGNAL_TEXT.items():
        document = json.loads((EXAMPLES / filename).read_text(encoding="utf-8"))
        by_id = {signal["id"]: signal["expr"] for signal in document["signals"]}
        for signal_id, text in texts.items():
            rows.append((f"{filename}:{signal_id}", text, by_id[signal_id]))
    return rows


def _err(source: str, fn=parse) -> ExprError:
    with pytest.raises(ExprError) as caught:
        fn(source)
    return caught.value


def _expr_validator() -> Draft202012Validator:
    schema = load_schema()
    return Draft202012Validator(
        {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/expr"}
    )


def test_signal_texts_cover_every_example_signal() -> None:
    for path in sorted(EXAMPLES.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        ids = {signal["id"] for signal in document["signals"]}
        assert set(SIGNAL_TEXT[path.name]) == ids


@pytest.mark.parametrize(("label", "text", "expected"), _signals())
def test_example_signal_parses_to_ir(label: str, text: str, expected: dict) -> None:
    node = parse_condition(text)
    assert node.result_type is ExprType.BOOLEAN
    assert to_ir(node) == expected
    assert to_ir(parse(format_expr(from_ir(expected)))) == expected
    _expr_validator().validate(to_ir(node))
    assert label


@pytest.mark.parametrize(
    ("text", "op"),
    [
        ("close + 1", "add"),
        ("close - 1", "sub"),
        ("close * 1", "mul"),
        ("close / 1", "div"),
        ("close > 1", "gt"),
        ("close >= 1", "gte"),
        ("close < 1", "lt"),
        ("close <= 1", "lte"),
        ("close == 1", "eq"),
        ("close != 1", "neq"),
    ],
)
def test_operator_spelling(text: str, op: str) -> None:
    node = parse(text)
    assert to_ir(node)["op"] == op
    if op in {"gt", "gte", "lt", "lte", "eq", "neq"}:
        assert node.result_type is ExprType.BOOLEAN
    else:
        assert node.result_type is ExprType.NUMERIC


def test_mul_binds_tighter_than_add() -> None:
    assert to_ir(parse("close + open * 2")) == {
        "op": "add",
        "left": {"op": "ref", "name": "close"},
        "right": {
            "op": "mul",
            "left": {"op": "ref", "name": "open"},
            "right": {"op": "lit", "value": 2},
        },
    }


def test_add_and_mul_are_left_associative() -> None:
    assert to_ir(parse("close - open - low"))["left"]["op"] == "sub"
    assert to_ir(parse("close / open * low")) == to_ir(parse("(close / open) * low"))
    assert format_expr(parse("close - open - low")) == "close - open - low"


def test_parentheses_override_precedence() -> None:
    assert to_ir(parse("(close + open) * 2"))["op"] == "mul"
    assert to_ir(parse("(close + open) * 2")) == to_ir(parse("(close + open) * 2"))
    assert to_ir(parse("close + open * 2")) != to_ir(parse("(close + open) * 2"))


def test_unary_minus_is_tighter_than_mul_and_is_neg() -> None:
    node = parse("-close * open")
    assert to_ir(node) == {
        "op": "mul",
        "left": {"op": "neg", "arg": {"op": "ref", "name": "close"}},
        "right": {"op": "ref", "name": "open"},
    }
    assert to_ir(parse("--close")) == {
        "op": "neg",
        "arg": {"op": "neg", "arg": {"op": "ref", "name": "close"}},
    }
    assert to_ir(parse("-30")) == {"op": "neg", "arg": {"op": "lit", "value": 30}}
    assert format_expr(parse("-(close + open)")) == "-(close + open)"


def test_comparison_binds_tighter_than_boolean_ops() -> None:
    text = "close > open AND volume >= 1 OR low < high"
    node = parse_condition(text)
    ir = to_ir(node)
    assert ir["op"] == "or"
    assert ir["left"]["op"] == "and"
    assert ir["left"]["left"]["op"] == "gt"
    assert ir["left"]["right"]["op"] == "gte"
    assert ir["right"]["op"] == "lt"


def test_not_applies_to_a_comparison_and_binds_tighter_than_and() -> None:
    assert to_ir(parse_condition("NOT close > open")) == {
        "op": "not",
        "arg": {
            "op": "gt",
            "left": {"op": "ref", "name": "close"},
            "right": {"op": "ref", "name": "open"},
        },
    }
    assert to_ir(parse_condition("NOT close > open AND low < high"))["op"] == "and"
    assert to_ir(parse_condition("NOT NOT (close > open)")) == {
        "op": "not",
        "arg": {"op": "not", "arg": to_ir(parse("close > open"))},
    }


def test_and_binds_tighter_than_or() -> None:
    node = parse_condition("close > 1 OR close > 2 AND close > 3")
    ir = to_ir(node)
    assert ir["op"] == "or"
    assert ir["right"]["op"] == "and"


def test_chained_comparison_is_rejected() -> None:
    source = "close > open > low"
    err = _err(source)
    assert err.message == "unexpected token '>': chained comparisons are not allowed"
    assert err.offset == source.rindex(">")
    assert err.line == 1
    assert err.column == err.offset + 1


def test_integer_literal_stays_int_and_bools_are_bools() -> None:
    lit = parse("30")
    assert isinstance(lit, Lit)
    assert lit.value == 30
    assert type(lit.value) is int
    assert parse("true").result_type is ExprType.BOOLEAN
    assert to_ir(parse("TRUE")) == {"op": "lit", "value": True}
    assert to_ir(parse("False")) == {"op": "lit", "value": False}
    assert isinstance(parse("true"), Lit)


def test_float_and_scientific_literals() -> None:
    assert parse("2.5").value == 2.5  # type: ignore[attr-defined]
    assert type(parse("2.5").value) is float  # type: ignore[attr-defined]
    assert parse("1.5e1").value == 15.0  # type: ignore[attr-defined]
    assert parse("1e-2").value == 0.01  # type: ignore[attr-defined]
    assert parse("1E+2").value == 100.0  # type: ignore[attr-defined]
    assert parse("007").value == 7  # type: ignore[attr-defined]


def test_keywords_and_calls_are_case_insensitive() -> None:
    assert to_ir(parse_condition("close > 1 And close < 2"))["op"] == "and"
    lowered = to_ir(parse_condition("cross_above(rsi_14, 30)"))
    assert lowered == to_ir(parse("CROSS_ABOVE(rsi_14, 30)"))
    assert to_ir(parse_condition("Not close > open"))["op"] == "not"


def test_builtin_series_and_derived_ids_are_numeric_refs() -> None:
    for name in ("open", "high", "low", "close", "volume", "rsi_14", "ema_fast", "bb_lower"):
        node = parse(name)
        assert isinstance(node, Ref)
        assert node.name == name
        assert node.result_type is ExprType.NUMERIC
        assert to_ir(node) == {"op": "ref", "name": name}


def test_calls_and_entry_price() -> None:
    bars = parse("BARS_SINCE(close > open)")
    assert isinstance(bars, Call)
    assert bars.op == "bars_since"
    assert bars.result_type is ExprType.NUMERIC
    assert parse_condition("BARS_SINCE(close > open) > 5").result_type is ExprType.BOOLEAN
    price = parse("ENTRY_PRICE()")
    assert isinstance(price, Call)
    assert price.op == "entry_price"
    assert price.args == ()
    assert to_ir(parse_numeric("close - ENTRY_PRICE()"))["op"] == "sub"
    _expr_validator().validate(to_ir(parse_condition("ENTRY_PRICE() > close")))


def test_spec_indicator_examples() -> None:
    rsi = parse("RSI(close, period=14)")
    assert isinstance(rsi, IndicatorCall)
    assert rsi.name == "rsi"
    assert rsi.output is None
    assert rsi.result_type is ExprType.NUMERIC
    assert to_ir(rsi.source) == {"op": "ref", "name": "close"}
    assert rsi.params[0][0] == "period"
    assert isinstance(rsi.params[0][1], Lit)
    assert rsi.params[0][1].value == 14

    ema = parse("EMA(close, period=12)")
    assert isinstance(ema, IndicatorCall)
    assert ema.name == "ema"

    band = parse("BOLLINGER(close, period=20, stddev=2).lower")
    assert isinstance(band, IndicatorCall)
    assert band.name == "bollinger"
    assert band.output == "lower"
    assert [name for name, _ in band.params] == ["period", "stddev"]


@pytest.mark.parametrize(
    ("text", "name", "params", "output"),
    [
        ("sma(close, 14)", "sma", ["period"], None),
        ("atr(high, period=7)", "atr", ["period"], None),
        ("macd(close, 12, 26, 9).histogram", "macd", ["fast", "slow", "signal"], "histogram"),
        (
            "macd(close, signal=9, fast=12, slow=26).signal",
            "macd",
            ["fast", "slow", "signal"],
            "signal",
        ),
        ("bb(close, 20, 2).middle", "bollinger", ["period", "stddev"], "middle"),
        ("BB(close, 20, 2).UPPER", "bollinger", ["period", "stddev"], "upper"),
        (
            "stochastic(close, k_period=14, d_period=3).k",
            "stochastic",
            ["k_period", "d_period"],
            "k",
        ),
        ("sma(source=close, period=14 + 1)", "sma", ["period"], None),
    ],
)
def test_indicator_calls(text: str, name: str, params: list[str], output: str | None) -> None:
    node = parse_numeric(text)
    assert isinstance(node, IndicatorCall)
    assert node.name == name
    assert [param for param, _ in node.params] == params
    assert node.output == output
    assert node.result_type is ExprType.NUMERIC


def test_indicator_is_not_an_ir_expression_operator() -> None:
    node = parse("rsi(close, 14)")
    err = _err_node(node)
    assert err.message == "indicator call is not an IR expression operator"
    assert err.offset == node.span.offset
    assert parse_condition("rsi(close, 14) > 30").result_type is ExprType.BOOLEAN


def _err_node(node) -> ExprError:
    with pytest.raises(ExprError) as caught:
        to_ir(node)
    return caught.value


def test_comments_whitespace_and_newlines_are_ignored() -> None:
    plain = to_ir(parse("CROSS_ABOVE(rsi_14, 30)"))
    assert to_ir(parse("CROSS_ABOVE( rsi_14 , 30 )")) == plain
    assert to_ir(parse("CROSS_ABOVE(rsi_14, # threshold\n  30)")) == plain
    assert to_ir(parse("close >\r\nopen")) == to_ir(parse("close > open"))
    commented = parse("1 + 2 # sum")
    assert to_ir(commented) == to_ir(parse("1 + 2"))


def test_division_by_zero_parses_without_evaluating() -> None:
    assert to_ir(parse("1 / 0")) == {
        "op": "div",
        "left": {"op": "lit", "value": 1},
        "right": {"op": "lit", "value": 0},
    }


def test_ref_span_skips_leading_whitespace() -> None:
    node = parse("  close")
    assert isinstance(node, Ref)
    assert (node.span.offset, node.span.line, node.span.column) == (2, 1, 3)


def test_identifier_length_limits() -> None:
    short = "a" * 64
    node = parse(short)
    assert isinstance(node, Ref)
    assert node.name == short
    err = _err("a" * 65)
    assert err.message == f"invalid identifier '{'a' * 65}'"
    assert err.offset == 0


def test_empty_input() -> None:
    err = _err("")
    assert err.message == "empty input"
    assert (err.offset, err.line, err.column) == (0, 1, 1)
    assert str(err) == "1:1: empty input (offset 0)"


def test_whitespace_and_comment_only_are_empty() -> None:
    err = _err("  \n\t")
    assert err.message == "empty input"
    assert (err.offset, err.line, err.column) == (4, 2, 2)
    assert _err("# nothing\n").message == "empty input"


def test_unexpected_end_of_input() -> None:
    err = _err("1 +")
    assert err.message == "unexpected token (end of input)"
    assert (err.offset, err.line, err.column) == (3, 1, 4)


def test_unexpected_token_and_character() -> None:
    err = _err("1 + * 2")
    assert err.message == "unexpected token '*'"
    assert (err.offset, err.line, err.column) == (4, 1, 5)
    bad = _err("close $ 1")
    assert bad.message == "unexpected character '$'"
    assert bad.offset == 6
    assert bad.column == 7


def test_multiline_error_position() -> None:
    source = "close >\n  *"
    err = _err(source)
    assert err.message == "unexpected token '*'"
    assert err.line == 2
    assert err.column == 3
    assert err.offset == source.index("*")
    assert str(err) == f"2:3: {err.message} (offset {err.offset})"


def test_unterminated_paren_points_at_the_opening() -> None:
    source = "1 + (2 * 3"
    err = _err(source)
    assert err.message == "unterminated paren"
    assert err.offset == source.index("(")
    assert (err.line, err.column) == (1, 5)
    nested = "(close > (1 + 2)"
    nested_err = _err(nested)
    assert nested_err.message == "unterminated paren"
    assert nested_err.offset == 0


def test_call_missing_paren_is_unterminated() -> None:
    source = "CROSS_ABOVE(close, 30"
    err = _err(source)
    assert err.message == "unterminated paren"
    assert err.offset == source.index("(")


def test_unknown_function() -> None:
    source = "1 + foo(2)"
    err = _err(source)
    assert err.message == "unknown function 'foo'"
    assert err.offset == source.index("foo")
    assert err.line == 1
    assert _err("FOO(1)").message == "unknown function 'FOO'"


def test_wrong_arity() -> None:
    assert _err("CROSS_ABOVE(close)").message == (
        "wrong arity: CROSS_ABOVE expects 2 arguments, got 1"
    )
    assert _err("CROSS_BELOW()").message == "wrong arity: CROSS_BELOW expects 2 arguments, got 0"
    assert _err("BARS_SINCE()").message == "wrong arity: BARS_SINCE expects 1 argument, got 0"
    assert _err("BARS_SINCE(close > 1, close > 2)").message == (
        "wrong arity: BARS_SINCE expects 1 argument, got 2"
    )
    assert _err("ENTRY_PRICE(close)").message == (
        "wrong arity: ENTRY_PRICE expects 0 arguments, got 1"
    )
    missing = _err("sma(close)")
    assert missing.message == "wrong arity: sma expects arguments (source, period), missing period"
    assert _err("sma(close, 14, 15)").message == "wrong arity: sma expects 2 arguments, got 3"
    assert _err("CROSS_ABOVE(close)").offset == 0
    assert _err("CROSS_ABOVE(close)").column == 1


def test_trailing_input_and_trailing_comma() -> None:
    err = _err("1 2")
    assert err.message == "trailing input: unexpected token '2'"
    assert (err.offset, err.column) == (2, 3)
    comma = _err("CROSS_ABOVE(close, open,)")
    assert comma.message == "unexpected token ','"
    assert _err("1close").message.startswith("trailing input")
    assert _err("(1, 2)").message == "unexpected token ','"


def test_bad_number() -> None:
    err = _err("1 + 1e")
    assert err.message == "bad number"
    assert err.offset == 4
    assert err.column == 5
    assert _err("1e+").message == "bad number"
    assert _err("1e9999").message == "bad number"


def test_invalid_identifier_and_bare_function() -> None:
    err = _err("Close")
    assert err.message == "invalid identifier 'Close'"
    assert (err.offset, err.line, err.column) == (0, 1, 1)
    bare = _err("CROSS_ABOVE")
    assert bare.message == "unexpected token 'CROSS_ABOVE': expected '('"
    assert _err("_close").message == "invalid identifier '_close'"
    assert isinstance(parse("cross_above"), Ref)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("1 AND 2", "type error: AND expects BOOLEAN, got NUMERIC"),
        ("true OR close", "type error: OR expects BOOLEAN, got NUMERIC"),
        ("true > false", "type error: > expects NUMERIC, got BOOLEAN"),
        ("true == false", "type error: == expects NUMERIC, got BOOLEAN"),
        ("(close > 1) != (close > 2)", "type error: != expects NUMERIC, got BOOLEAN"),
        ("true + 1", "type error: + expects NUMERIC, got BOOLEAN"),
        ("NOT close", "type error: NOT expects BOOLEAN, got NUMERIC"),
        ("-(close > 1)", "type error: unary minus expects NUMERIC, got BOOLEAN"),
        (
            "CROSS_ABOVE(close > 1, 2)",
            "type error: CROSS_ABOVE argument 1 expects NUMERIC, got BOOLEAN",
        ),
        (
            "CROSS_BELOW(close, close > 1)",
            "type error: CROSS_BELOW argument 2 expects NUMERIC, got BOOLEAN",
        ),
        ("BARS_SINCE(close)", "type error: BARS_SINCE argument 1 expects BOOLEAN, got NUMERIC"),
        ("sma(close > 1, 14)", "type error: sma argument source expects NUMERIC, got BOOLEAN"),
        ("rsi(close, close > 1)", "type error: rsi argument period expects NUMERIC, got BOOLEAN"),
    ],
)
def test_type_errors(source: str, message: str) -> None:
    err = _err(source)
    assert err.message == message
    assert err.offset >= 0
    assert err.line >= 1
    assert err.column >= 1


def test_type_error_positions() -> None:
    left = _err("1 AND true")
    assert left.offset == 0
    assert left.column == 1
    right = _err("true AND 1")
    assert right.offset == len("true AND ")
    assert right.message == "type error: AND expects BOOLEAN, got NUMERIC"
    cross = _err("CROSS_ABOVE(close > 1, 2)")
    assert cross.offset == len("CROSS_ABOVE(")


def test_expected_type_entry_points() -> None:
    assert parse_condition("CROSS_ABOVE(rsi_14, 30)").result_type is ExprType.BOOLEAN
    condition = _err("close", parse_condition)
    assert condition.message == "type error: condition expects BOOLEAN, got NUMERIC"
    assert condition.offset == 0
    numeric = _err("close > 1", parse_numeric)
    assert numeric.message == "type error: numeric expression expects NUMERIC, got BOOLEAN"
    assert parse_numeric("ENTRY_PRICE() * 1.06").result_type is ExprType.NUMERIC


def test_indicator_argument_errors() -> None:
    missing = _err("bollinger(close, 20, 2)")
    assert missing.message == "bollinger requires an output field (.lower, .middle, .upper)"
    assert missing.offset == 0
    assert _err("macd(close, 12, 26, 9)").message.startswith("macd requires an output field")
    band = _err("bollinger(close, 20, 2).band")
    assert band.message == "unknown output 'band' for bollinger"
    assert band.offset == len("bollinger(close, 20, 2).")
    assert _err("rsi(close, 14).lower").message == "rsi does not take an output field"
    assert _err("sma(close, foo=1)").message == "unknown parameter 'foo' for sma"
    assert _err("sma(close, 14, period=14)").message == "duplicate argument 'period'"
    assert _err("CROSS_ABOVE(a=close, b=open)").message == (
        "keyword arguments are not allowed in CROSS_ABOVE"
    )
    assert _err("ENTRY_PRICE().x").message == "ENTRY_PRICE does not take an output field"
    followed = _err("sma(period=14, close)")
    assert followed.message.endswith("positional argument follows a keyword argument")


def test_compound_expression_matches_the_schema() -> None:
    text = "NOT (close > open AND volume >= 1000) OR BARS_SINCE(CROSS_BELOW(low, high)) == 0"
    node = parse_condition(text)
    ir = to_ir(node)
    _expr_validator().validate(ir)
    assert to_ir(parse(format_expr(node))) == ir
    assert ir["op"] == "or"
    assert ir["right"]["op"] == "eq"


def test_tokenize_keywords_numbers_and_operators() -> None:
    kinds = [token.kind for token in tokenize("close >= 1 AND NOT true")]
    assert kinds == [
        TokenKind.IDENT,
        TokenKind.GTE,
        TokenKind.NUMBER,
        TokenKind.AND,
        TokenKind.NOT,
        TokenKind.TRUE,
        TokenKind.EOF,
    ]
    number = tokenize("1e-2")[0]
    assert number.kind is TokenKind.NUMBER
    assert number.value == 0.01
    assert number.offset == 0


def test_expr_package_does_not_execute_code() -> None:
    files = sorted(EXPR_PKG.glob("*.py"))
    assert len(files) >= 4
    forbidden = re.compile(r"\b(eval|exec|compile)\s*\(")
    offenders: list[str] = []
    for path in files:
        for match in forbidden.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}: {match.group(0)}")
    assert offenders == []


def test_parse_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        parse(None)  # type: ignore[arg-type]
