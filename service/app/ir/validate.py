"""Validate strategy IR v1 documents.

JSON Schema (draft 2020-12) runs first. Semantic reference checks run only
after the document matches the schema, so a reported path is meaningful.
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

SCHEMA_FILENAME = "strategy-ir-v1.schema.json"
REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "schemas" / SCHEMA_FILENAME

BUILTIN_SERIES = frozenset({"open", "high", "low", "close", "volume"})
_REQUIRED_MESSAGE = re.compile(r"^'([^']+)' is a required property$")

_UNARY = frozenset({"not", "neg"})
_BINARY = frozenset(
    {"add", "sub", "mul", "div", "gt", "gte", "lt", "lte", "eq", "neq", "and", "or"}
)
_CALLS = frozenset({"cross_above", "cross_below", "bars_since", "entry_price"})


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
    issues = [_from_schema_error(error) for error in compiled.iter_errors(document)]
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


def _semantic(document: Mapping[str, Any]) -> list[IRIssue]:
    issues: list[IRIssue] = []
    asset_ids = _collect_ids(document["assets"], "$.assets", issues)
    series_ids = _check_series(document["derived_series"], issues)
    known_series = BUILTIN_SERIES | set(series_ids)
    signal_ids = _check_signals(document["signals"], known_series, issues)
    action_ids = _check_actions(document["actions"], set(asset_ids), issues)
    _check_rules(document["rules"], set(signal_ids), set(action_ids), issues)
    return issues


def _collect_ids(items: list[dict[str, Any]], path: str, issues: list[IRIssue]) -> list[str]:
    seen: list[str] = []
    for index, item in enumerate(items):
        ident = item["id"]
        if ident in seen:
            issues.append(
                IRIssue(
                    f"{path}[{index}].id",
                    "duplicate_id",
                    f"duplicate id '{ident}'",
                )
            )
        seen.append(ident)
    return seen


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
        if ident in seen:
            issues.append(IRIssue(f"{path}.id", "duplicate_id", f"duplicate id '{ident}'"))
        known = set(known_series)
        known.update(seen)
        _walk_expr(signal["expr"], f"{path}.expr", known, issues)
        seen.append(ident)
    return seen


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


def _walk_expr(expr: Any, path: str, known: set[str], issues: list[IRIssue]) -> None:
    if not isinstance(expr, dict):
        return
    op = expr.get("op")
    if op == "ref":
        name = expr.get("name")
        if name not in known:
            issues.append(
                IRIssue(
                    f"{path}.name",
                    "unknown_series",
                    f"unknown series reference '{name}'",
                )
            )
        return
    if op in _UNARY:
        _walk_expr(expr.get("arg"), f"{path}.arg", known, issues)
        return
    if op in _BINARY:
        _walk_expr(expr.get("left"), f"{path}.left", known, issues)
        _walk_expr(expr.get("right"), f"{path}.right", known, issues)
        return
    if op in _CALLS:
        args = expr.get("args")
        if isinstance(args, list):
            for index, arg in enumerate(args):
                _walk_expr(arg, f"{path}.args[{index}]", known, issues)
