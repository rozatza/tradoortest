"""Strategy tools. Validation and diffs are the Phase 1 and ROZ-75 functions.

``validate`` reports strategy problems as ``valid: false``. ``write_strategy``
and ``update_strategy`` report those same problems as a tool error and do
not write. Preview never calls ``add_version``. Apply does, and only with
``confirm`` and ``expected_version``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from app.auth import CurrentUser
from app.ir.constants import IR_VERSION
from app.strategies.compile import CompiledStrategy, compile_body
from app.strategies.diff import dsl_unified_diff, ir_structural_diff
from app.strategies.errors import Rejected
from app.strategies.store import StrategyRecord, VersionRecord
from app.tools.errors import ToolError, safe_text
from app.tools.runtime import ToolRuntime


def write_strategy(
    arguments: Mapping[str, Any],
    user: CurrentUser,
    runtime: ToolRuntime,
) -> dict[str, Any]:
    compiled = _compile(arguments)
    record, version = runtime.strategies().create(user.user_id, compiled)
    return _saved(record, version)


def update_strategy(
    arguments: Mapping[str, Any],
    user: CurrentUser,
    runtime: ToolRuntime,
) -> dict[str, Any]:
    compiled = _compile(arguments)
    store = runtime.strategies()
    record = store.find(user.user_id, str(arguments["strategy_id"]))
    if record is None:
        raise ToolError("not_found", "strategy was not found")
    if record.archived_at is not None:
        raise ToolError("strategy_archived", "strategy is archived")
    current = store.get_version(user.user_id, record.strategy_id, record.latest_version)
    mode = str(arguments.get("mode", "preview"))
    dsl_diff = dsl_unified_diff(current.dsl, compiled.dsl, current.version, current.version + 1)
    ir_diff = ir_structural_diff(current.ir, compiled.ir)
    changed = dsl_diff != "" or bool(ir_diff)
    if mode == "preview":
        return {
            "mode": "preview",
            "mutated": False,
            "strategy_id": record.strategy_id,
            "from_version": current.version,
            "to_version": current.version + 1,
            "from_ir_hash": current.ir_hash,
            "to_ir_hash": compiled.ir_hash,
            "changed": changed,
            "dsl_diff": dsl_diff,
            "ir_diff": ir_diff,
            "candidate_name": compiled.name,
            "candidate_ir_strategy_id": compiled.ir_strategy_id,
        }
    if arguments.get("confirm") is not True:
        raise ToolError(
            "confirmation_required",
            "apply requires confirm true",
            path="$.confirm",
        )
    if "expected_version" not in arguments:
        raise ToolError(
            "validation_error",
            "apply requires expected_version",
            path="$.expected_version",
        )
    updated, saved = store.add_version(
        user.user_id,
        record.strategy_id,
        compiled,
        expected_version=arguments["expected_version"],
        expected_ir_hash=arguments.get("expected_ir_hash"),
    )
    return {
        "mode": "apply",
        "mutated": True,
        "strategy_id": updated.strategy_id,
        "from_version": current.version,
        "version": saved.version,
        "from_ir_hash": current.ir_hash,
        "ir_hash": saved.ir_hash,
        "dsl_diff": dsl_diff,
        "ir_diff": ir_diff,
        "dsl": saved.dsl,
        "ir": saved.ir,
        "updated_at": updated.updated_at,
    }


def validate(
    arguments: Mapping[str, Any],
    _user: CurrentUser,
    _runtime: ToolRuntime,
) -> dict[str, Any]:
    try:
        compiled = _compile(arguments)
    except ToolError as exc:
        if exc.details is None:
            raise
        return _invalid(exc.details)
    return {
        "valid": True,
        "ir_hash": compiled.ir_hash,
        "ir_version": IR_VERSION,
        "strategy_id": compiled.ir_strategy_id,
        "name": compiled.name,
        "canonical_ir": compiled.ir,
        "dsl": compiled.dsl,
        "diagnostics": [],
    }


def _compile(arguments: Mapping[str, Any]) -> CompiledStrategy:
    payload: dict[str, Any] = {}
    if "dsl" in arguments:
        payload["dsl"] = arguments["dsl"]
    if "ir" in arguments:
        payload["ir"] = arguments["ir"]
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        raw = text.encode("utf-8")
    except (TypeError, ValueError):
        raise ToolError("validation_error", "strategy could not be read", path="$") from None
    try:
        return compile_body(raw)
    except Rejected as exc:
        raise ToolError(
            exc.code or "validation_error",
            safe_text(exc.message, "strategy is not valid"),
            path=exc.path,
            details=_diagnostics(exc.diagnostics),
        ) from None


def _invalid(details: dict[str, Any] | list[Any]) -> dict[str, Any]:
    diagnostics = details if isinstance(details, list) else []
    return {
        "valid": False,
        "ir_hash": None,
        "ir_version": None,
        "strategy_id": None,
        "name": None,
        "canonical_ir": None,
        "dsl": None,
        "diagnostics": [_diagnostic(item) for item in diagnostics],
    }


def _diagnostics(items: list[dict[str, object]] | None) -> list[dict[str, Any]]:
    if not items:
        return []
    return [_diagnostic(item) for item in items]


def _diagnostic(item: object) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {
            "code": "validation_error",
            "message": "strategy is not valid",
            "path": None,
            "line": None,
            "column": None,
        }
    line = item.get("line")
    column = item.get("column")
    path = item.get("path")
    return {
        "code": safe_text(item.get("code", "validation_error"), "validation_error", limit=80),
        "message": safe_text(item.get("message", "strategy is not valid"), "strategy is not valid"),
        "path": None if path is None else safe_text(path, "$.", limit=200),
        "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
        "column": column if isinstance(column, int) and not isinstance(column, bool) else None,
    }


def _saved(record: StrategyRecord, version: VersionRecord) -> dict[str, Any]:
    return {
        "strategy_id": record.strategy_id,
        "version": version.version,
        "name": record.name,
        "ir_strategy_id": record.ir_strategy_id,
        "ir_hash": version.ir_hash,
        "dsl": version.dsl,
        "ir": version.ir,
        "status": "active",
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
