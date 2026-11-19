"""Validate strategy IR v1 documents.

JSON Schema (draft 2020-12) runs first. Semantic checks run only after the
document matches the schema, so a reported path is meaningful. A few schema
failures are reported under a specific code (`invalid_bar_size`,
`invalid_sizing`, `insane_stop_loss`, `insane_take_profit`) instead of the
generic JSON Schema keyword.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from app.expr import ExprError, ExprType, from_ir
from app.ir.constants import BUILTIN_SERIES

SCHEMA_FILENAME = "strategy-ir-v1.schema.json"
REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "schemas" / SCHEMA_FILENAME
_REQUIRED_MESSAGE = re.compile(r"^'([^']+)' is a required property$")


@dataclass(frozen=True)
class IRIssue:
    """One schema or semantic failure.

    `path` is a JSONPath. `keyword` is the JSON Schema keyword or a semantic
    code such as `unknown_signal`.
    """

    path: str
    keyword: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message} [{self.keyword}]"


def load_schema() -> dict[str, Any]:
    """Return the canonical IR v1 schema."""
    return _load_schema_cached()


@lru_cache(maxsize=1)
def _load_schema_cached() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    if not isinstance(schema, dict):
        raise TypeError(f"{SCHEMA_PATH} must contain a JSON object")
    return schema


def validate_ir(document: Any, schema: Mapping[str, Any] | None = None) -> list[IRIssue]:
    """Return readable issues. An empty list means the document is valid IR v1."""
    if not isinstance(document, dict):
        return [IRIssue("$", "type", "strategy IR must be a JSON object")]

    compiled = Draft202012Validator(schema if schema is not None else load_schema())
    issues = [_specialize(_from_schema_error(error)) for error in compiled.iter_errors(document)]
    if issues:
        return sorted(issues, key=lambda issue: (issue.path, issue.keyword))
    return sorted(_semantic(document), key=lambda issue: (issue.path, issue.keyword))


def format_issues(issues: list[IRIssue]) -> str:
    """Join issues as `path: message [keyword]`, one per line."""
    return "\n".join(str(issue) for issue in issues)


def _from_schema_error(error: ValidationError) -> IRIssue:
    path = error.json_path or "$"
    message = error.message
    if error.validator == "required":
        missing = _missing_property(error.message)
        if missing:
            path = _join(path, missing)
    elif error.validator == "const":
        message = f"{error.message}; got {error.instance!r}"
    return IRIssue(path=path, keyword=str(error.validator), message=message)


def _missing_property(message: str) -> str | None:
    match = _REQUIRED_MESSAGE.match(message)
    if match is None:
        return None
    return match.group(1)


def _join(base: str, prop: str) -> str:
    if base in {"", "$"}:
        return f"$.{prop}"
    return f"{base}.{prop}"


def _specialize(issue: IRIssue) -> IRIssue:
    """Replace generic schema keywords with the documented strategy codes."""
    if issue.path == "$.engine.bar_size" and issue.keyword == "enum":
        return IRIssue(
            issue.path,
            "invalid_bar_size",
            "bar_size must be one of 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d",
        )
    if issue.path.endswith(".sizing.percent") and issue.keyword in {"exclusiveMinimum", "maximum"}:
        return IRIssue(
            issue.path,
            "invalid_sizing",
            "sizing percent must be greater than 0 and at most 100",
        )
    if issue.path.endswith(".stop_loss.percent") and issue.keyword in {
        "exclusiveMinimum",
        "maximum",
    }:
        return IRIssue(
            issue.path,
            "insane_stop_loss",
            "stop_loss percent must be greater than 0 and less than 100 "
            "so a long stop stays above zero",
        )
    if issue.path.endswith(".take_profit.percent") and issue.keyword in {
        "exclusiveMinimum",
        "maximum",
    }:
        return IRIssue(
            issue.path,
            "insane_take_profit",
            "take_profit percent must be greater than 0 and at most 100",
        )
    return issue


def _semantic(document: Mapping[str, Any]) -> list[IRIssue]:
    issues: list[IRIssue] = []
    asset_ids = _check_assets(document["assets"], issues)
    series_ids = _check_series(document["derived_series"], issues)
    known_series = BUILTIN_SERIES | set(series_ids)
    signal_ids = _check_signals(document["signals"], known_series, issues)
    action_ids = _check_actions(document["actions"], set(asset_ids), issues)
    _check_rules(document["rules"], set(signal_ids), set(action_ids), issues)
    _check_entry(document["rules"], document["actions"], issues)
    _check_long_risk(document["actions"], issues)
    return issues


def _check_assets(items: list[dict[str, Any]], issues: list[IRIssue]) -> list[str]:
    seen_ids: list[str] = []
    seen_symbols: list[str] = []
    for index, item in enumerate(items):
        ident = item["id"]
        if ident in seen_ids:
            issues.append(
                IRIssue(f"$.assets[{index}].id", "duplicate_id", f"duplicate id '{ident}'")
            )
        seen_ids.append(ident)
        symbol = item["symbol"]
        if symbol in seen_symbols:
            issues.append(
                IRIssue(
                    f"$.assets[{index}].symbol",
                    "duplicate_symbol",
                    f"duplicate symbol '{symbol}'",
                )
            )
        seen_symbols.append(symbol)
    return seen_ids


def _check_series(series_items: list[dict[str, Any]], issues: list[IRIssue]) -> list[str]:
    seen: list[str] = []
    for index, series in enumerate(series_items):
        ident = series["id"]
        path = f"$.derived_series[{index}]"
        if ident in BUILTIN_SERIES:
            issues.append(
                IRIssue(
                    f"{path}.id",
                    "reserved_series",
                    f"'{ident}' is a built-in series and cannot be redefined",
                )
            )
        if ident in seen:
            issues.append(IRIssue(f"{path}.id", "duplicate_id", f"duplicate id '{ident}'"))
        source = series["source"]
        if source not in BUILTIN_SERIES and source not in seen:
            issues.append(
                IRIssue(
                    f"{path}.source",
                    "unknown_series",
                    f"unknown series reference '{source}'",
                )
            )
        seen.append(ident)
    return seen


def _check_signals(
    signals: list[dict[str, Any]],
    known_series: set[str],
    issues: list[IRIssue],
) -> list[str]:
    seen: list[str] = []
    for index, signal in enumerate(signals):
        ident = signal["id"]
        path = f"$.signals[{index}]"
        if ident in seen or ident in known_series:
            issues.append(IRIssue(f"{path}.id", "duplicate_id", f"duplicate id '{ident}'"))
        names = {name: ExprType.NUMERIC for name in known_series}
        names.update({name: ExprType.BOOLEAN for name in seen})
        _check_signal_expr(signal["expr"], f"{path}.expr", names, issues)
        seen.append(ident)
    return seen


def _check_signal_expr(
    expr: Any,
    path: str,
    names: dict[str, ExprType],
    issues: list[IRIssue],
) -> None:
    """Type-check a signal expression while resolving names.

    Series in `names` are NUMERIC and earlier signals are BOOLEAN. `from_ir`
    applies those types as it walks, so a signal used as a number fails here
    rather than in a later pass that assumed every ref was numeric.
    """
    try:
        node = from_ir(expr, names=names, path=path)
    except ExprError as err:
        issues.append(IRIssue(err.path or path, err.code or "type_error", err.message))
        return
    if node.result_type is not ExprType.BOOLEAN:
        issues.append(
            IRIssue(
                path,
                "type_error",
                "type error: signal expression expects BOOLEAN, "
                f"got {node.result_type.value}",
            )
        )


def _check_actions(
    actions: list[dict[str, Any]],
    asset_ids: set[str],
    issues: list[IRIssue],
) -> list[str]:
    seen: list[str] = []
    for index, action in enumerate(actions):
        ident = action["id"]
        path = f"$.actions[{index}]"
        if ident in seen:
            issues.append(IRIssue(f"{path}.id", "duplicate_id", f"duplicate id '{ident}'"))
        asset = action["asset"]
        if asset not in asset_ids:
            issues.append(
                IRIssue(
                    f"{path}.asset",
                    "unknown_asset",
                    f"unknown asset reference '{asset}'",
                )
            )
        seen.append(ident)
    return seen


def _check_rules(
    rules: list[dict[str, Any]],
    signal_ids: set[str],
    action_ids: set[str],
    issues: list[IRIssue],
) -> None:
    seen: list[str] = []
    for index, rule in enumerate(rules):
        ident = rule["id"]
        path = f"$.rules[{index}]"
        if ident in seen:
            issues.append(IRIssue(f"{path}.id", "duplicate_id", f"duplicate id '{ident}'"))
        when = rule["when"]
        if when not in signal_ids:
            issues.append(
                IRIssue(
                    f"{path}.when",
                    "unknown_signal",
                    f"unknown signal reference '{when}'",
                )
            )
        action = rule["do"]
        if action not in action_ids:
            issues.append(
                IRIssue(
                    f"{path}.do",
                    "unknown_action",
                    f"unknown action reference '{action}'",
                )
            )
        seen.append(ident)


def _check_entry(
    rules: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    issues: list[IRIssue],
) -> None:
    action_types = {action["id"]: action["type"] for action in actions}
    if any(action_types.get(rule["do"]) == "enter" for rule in rules):
        return
    issues.append(IRIssue("$.rules", "missing_entry", "strategy has no entry rule"))


def _check_long_risk(actions: list[dict[str, Any]], issues: list[IRIssue]) -> None:
    """Reject a long stop that schema still allows.

    Stop percent of 100 prices the stop at zero. Take-profit and stop-loss
    percents outside (0, 100] / (0, 100) are schema errors and are renamed
    in `_specialize`. This check covers the schema-valid stop of exactly 100.
    """
    for index, action in enumerate(actions):
        risk = action.get("risk")
        if not isinstance(risk, dict):
            continue
        stop = risk.get("stop_loss")
        if not isinstance(stop, dict):
            continue
        percent = stop.get("percent")
        if _is_number(percent) and percent >= 100:
            issues.append(
                IRIssue(
                    f"$.actions[{index}].risk.stop_loss.percent",
                    "insane_stop_loss",
                    "stop_loss percent must be greater than 0 and less than 100 "
                    "so a long stop stays above zero",
                )
            )


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
