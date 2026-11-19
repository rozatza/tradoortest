import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from app.ir.validate import REPO_ROOT, format_issues, load_schema, validate_ir

EXAMPLES = REPO_ROOT / "schemas" / "examples"
INVALID = Path(__file__).parent / "fixtures" / "invalid"

# Each invalid fixture must fail on this path and keyword.
EXPECTED_FAILURES = {
    "missing-ir-version.json": ("$.ir_version", "required"),
    "wrong-ir-version.json": ("$.ir_version", "const"),
    "sell-to-open-side.json": ("$.actions[0].side", "const"),
    "non-solana-chain.json": ("$.assets[0].chain", "const"),
    "non-spot-market.json": ("$.assets[0].market", "const"),
    "oco-missing-stop.json": ("$.actions[0].risk.stop_loss", "required"),
    "bad-stop-percent.json": ("$.actions[0].risk.stop_loss.percent", "insane_stop_loss"),
    "unknown-signal-ref.json": ("$.rules[0].when", "unknown_signal"),
    "unknown-action-ref.json": ("$.rules[1].do", "unknown_action"),
    "unknown-asset-ref.json": ("$.actions[0].asset", "unknown_asset"),
    "unknown-series-ref.json": ("$.derived_series[0].source", "unknown_series"),
    "duplicate-signal-id.json": ("$.signals[1].id", "duplicate_id"),
    "reserved-series.json": ("$.derived_series[0].id", "reserved_series"),
    "missing-entry.json": ("$.rules", "missing_entry"),
    "duplicate-symbol.json": ("$.assets[1].symbol", "duplicate_symbol"),
    "invalid-bar-size.json": ("$.engine.bar_size", "invalid_bar_size"),
    "invalid-sizing.json": ("$.actions[0].sizing.percent", "invalid_sizing"),
    "insane-take-profit.json": ("$.actions[0].risk.take_profit.percent", "insane_take_profit"),
    "stop-percent-100.json": ("$.actions[0].risk.stop_loss.percent", "insane_stop_loss"),
    "signal-type-error.json": ("$.signals[0].expr", "type_error"),
    "negative-capital.json": ("$.capital.initial", "exclusiveMinimum"),
    "leverage-forbidden.json": ("$.assets[0]", "additionalProperties"),
}


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_schema_is_draft_2020_12() -> None:
    schema = load_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_ir_version_const_is_string_1_0() -> None:
    assert load_schema()["properties"]["ir_version"]["const"] == "1.0"


def test_fixture_counts() -> None:
    assert len(list(EXAMPLES.glob("*.json"))) == 3
    assert len(list(INVALID.glob("*.json"))) >= 5
    assert set(EXPECTED_FAILURES) == {path.name for path in INVALID.glob("*.json")}


@pytest.mark.parametrize(
    "path",
    sorted(EXAMPLES.glob("*.json")),
    ids=lambda path: path.name,
)
def test_valid_examples(path: Path) -> None:
    issues = validate_ir(_load(path))
    assert issues == []
    assert format_issues(issues) == ""


@pytest.mark.parametrize(
    ("name", "expected_path", "expected_keyword"),
    [(name, path, keyword) for name, (path, keyword) in sorted(EXPECTED_FAILURES.items())],
)
def test_invalid_fixture(name: str, expected_path: str, expected_keyword: str) -> None:
    issues = validate_ir(_load(INVALID / name))
    assert issues, name
    match = [
        issue
        for issue in issues
        if issue.path == expected_path and issue.keyword == expected_keyword
    ]
    assert match, format_issues(issues)
    rendered = format_issues(match)
    assert expected_path in rendered
    assert expected_keyword in rendered
    assert match[0].message


def test_unknown_signal_message_names_the_reference() -> None:
    issues = validate_ir(_load(INVALID / "unknown-signal-ref.json"))
    issue = next(issue for issue in issues if issue.keyword == "unknown_signal")
    assert issue.path == "$.rules[0].when"
    assert "not_a_signal" in issue.message
    assert "unknown signal reference" in issue.message


def test_non_object_document() -> None:
    issues = validate_ir(["not", "an", "object"])
    assert issues[0].path == "$"
    assert issues[0].keyword == "type"


def test_earlier_signal_is_boolean_inside_a_later_signal() -> None:
    document = _load(EXAMPLES / "rsi-cross.json")
    document["signals"][1]["expr"] = {
        "op": "not",
        "arg": {"op": "ref", "name": "rsi_buy"},
    }
    assert validate_ir(document) == []


def test_signal_used_as_a_number_is_a_type_error() -> None:
    document = _load(EXAMPLES / "rsi-cross.json")
    document["signals"][1]["expr"] = {
        "op": "add",
        "left": {"op": "ref", "name": "rsi_buy"},
        "right": {"op": "lit", "value": 1},
    }
    issues = validate_ir(document)
    typed = next(issue for issue in issues if issue.keyword == "type_error")
    assert typed.path == "$.signals[1].expr.left"
    assert "BOOLEAN" in typed.message


def test_series_used_as_a_condition_is_a_type_error() -> None:
    document = _load(EXAMPLES / "rsi-cross.json")
    document["signals"][0]["expr"] = {"op": "not", "arg": {"op": "ref", "name": "close"}}
    issues = validate_ir(document)
    typed = next(issue for issue in issues if issue.keyword == "type_error")
    assert typed.path == "$.signals[0].expr.arg"
    assert "NUMERIC" in typed.message


def test_unknown_expression_name_is_unknown_series() -> None:
    document = _load(EXAMPLES / "rsi-cross.json")
    document["signals"][0]["expr"] = {"op": "ref", "name": "nope"}
    issues = validate_ir(document)
    issue = next(issue for issue in issues if issue.keyword == "unknown_series")
    assert issue.path == "$.signals[0].expr.name"
    assert "nope" in issue.message
