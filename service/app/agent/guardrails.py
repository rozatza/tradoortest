"""Hard guards around the ROZ-76 dispatcher.

An agent edit is always a preview. A backtest starts only after ``validate``
has passed for the same canonical ``ir_hash`` in this session.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from app.agent.store import AgentStore, ProposalRecord
from app.auth import CurrentUser
from app.strategies.compile import compile_body
from app.strategies.errors import Rejected, StrategyNotFound, VersionNotFound
from app.tools.dispatch import Dispatcher
from app.tools.errors import ToolError, error_envelope
from app.tools.registry import BY_NAME
from app.tools.runtime import ToolRuntime
from app.tools.strict import require_object, validate_schema

_STRIPPED = frozenset({"mode", "confirm", "expected_version", "expected_ir_hash"})
_VALIDATE_FIRST = "validate must pass for this exact strategy before running a backtest"


def run_tool(
    *,
    name: str,
    arguments: object,
    user: CurrentUser,
    session_id: str,
    turn_id: str,
    dispatcher: Dispatcher,
    store: AgentStore,
) -> tuple[dict[str, Any], ProposalRecord | None]:
    """Run one tool call. A proposal is returned only when one was stored."""
    if name == "update_strategy":
        return _update(arguments, user, session_id, turn_id, dispatcher, store)
    if name == "backtest_start":
        blocked = _backtest_block(arguments, user, session_id, dispatcher.runtime, store)
        if blocked is not None:
            return blocked, None
    envelope = dispatcher.invoke(
        name, arguments if isinstance(arguments, dict) else arguments, user
    )
    if name == "validate":
        _remember_validation(envelope, user, session_id, store)
    return envelope, None


def _update(
    arguments: object,
    user: CurrentUser,
    session_id: str,
    turn_id: str,
    dispatcher: Dispatcher,
    store: AgentStore,
) -> tuple[dict[str, Any], ProposalRecord | None]:
    preview_args = _preview_args(arguments)
    envelope = dispatcher.invoke("update_strategy", preview_args, user)
    if not envelope.get("ok"):
        return envelope, None
    data = envelope.get("data")
    if not isinstance(data, dict):
        return _failed("update_strategy", "internal_error", "the tool could not finish"), None
    if data.get("changed") is not True:
        return (
            {
                "ok": True,
                "tool": "update_strategy",
                "data": {
                    "changed": False,
                    "mutated": False,
                    "proposal_id": None,
                    "message": "no changes were detected; storage was not modified",
                    "strategy_id": data.get("strategy_id"),
                    "from_version": data.get("from_version"),
                    "from_ir_hash": data.get("from_ir_hash"),
                    "to_ir_hash": data.get("to_ir_hash"),
                },
            },
            None,
        )
    compiled = _compile_candidate(preview_args)
    if compiled is None or compiled.ir_hash != data.get("to_ir_hash"):
        return _failed("update_strategy", "internal_error", "the tool could not finish"), None
    ir_diff = data.get("ir_diff")
    if not isinstance(ir_diff, list):
        return _failed("update_strategy", "internal_error", "the tool could not finish"), None
    proposal = ProposalRecord(
        proposal_id=uuid.uuid4().hex,
        session_id=session_id,
        turn_id=turn_id,
        user_id=user.user_id,
        strategy_id=str(data.get("strategy_id")),
        from_version=int(data["from_version"]),
        to_version=int(data["to_version"]),
        from_ir_hash=str(data["from_ir_hash"]),
        to_ir_hash=str(data["to_ir_hash"]),
        dsl_diff=str(data.get("dsl_diff") or ""),
        ir_diff=ir_diff,
        candidate_dsl=compiled.dsl,
        candidate_name=compiled.name,
        candidate_ir_strategy_id=compiled.ir_strategy_id,
        status="pending",
        created_at=store.now(),
        decided_at=None,
        applied_version=None,
        applied_ir_hash=None,
    )
    store.add_proposal(proposal)
    return (
        {
            "ok": True,
            "tool": "update_strategy",
            "data": {
                "changed": True,
                "mutated": False,
                "proposal_id": proposal.proposal_id,
                "message": (
                    "a pending proposal was created for explicit user review; "
                    "storage was not modified"
                ),
                "strategy_id": proposal.strategy_id,
                "from_version": proposal.from_version,
                "to_version": proposal.to_version,
                "from_ir_hash": proposal.from_ir_hash,
                "to_ir_hash": proposal.to_ir_hash,
            },
        },
        proposal,
    )


def _preview_args(arguments: object) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {"mode": "preview"}
    preview = {key: value for key, value in arguments.items() if key not in _STRIPPED}
    preview["mode"] = "preview"
    return preview


def _compile_candidate(arguments: Mapping[str, Any]):
    payload: dict[str, Any] = {}
    if "dsl" in arguments:
        payload["dsl"] = arguments["dsl"]
    if "ir" in arguments:
        payload["ir"] = arguments["ir"]
    try:
        raw = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        return compile_body(raw)
    except (Rejected, ToolError, TypeError, ValueError):
        return None


def _backtest_block(
    arguments: object,
    user: CurrentUser,
    session_id: str,
    runtime: ToolRuntime,
    store: AgentStore,
) -> dict[str, Any] | None:
    spec = BY_NAME["backtest_start"]
    try:
        payload = require_object(arguments)
        validate_schema(spec.input_schema, payload)
    except ToolError as exc:
        return error_envelope(
            "backtest_start",
            exc.code,
            exc.message,
            path=exc.path,
            retryable=exc.retryable,
            details=exc.details if isinstance(exc.details, dict | list) else None,
        )
    ir_hash, blocked = _target_hash(payload, user, runtime)
    if blocked is not None:
        return blocked
    if ir_hash is None or not store.has_validation(user.user_id, session_id, ir_hash):
        return _failed("backtest_start", "validation_required", _VALIDATE_FIRST)
    return None


def _target_hash(
    payload: Mapping[str, Any],
    user: CurrentUser,
    runtime: ToolRuntime,
) -> tuple[str | None, dict[str, Any] | None]:
    if "strategy_id" in payload:
        strategies = runtime.strategies()
        record = strategies.find(user.user_id, str(payload["strategy_id"]))
        if record is None:
            return None, _failed("backtest_start", "not_found", "strategy was not found")
        if record.archived_at is not None:
            return None, _failed("backtest_start", "strategy_archived", "strategy is archived")
        number = payload.get("version", record.latest_version)
        try:
            loaded = strategies.get_version(user.user_id, record.strategy_id, int(number))
        except (StrategyNotFound, VersionNotFound):
            return None, _failed("backtest_start", "not_found", "strategy version was not found")
        return loaded.ir_hash, None
    compiled = _compile_candidate(payload)
    if compiled is None:
        return None, _failed("backtest_start", "validation_required", _VALIDATE_FIRST)
    return compiled.ir_hash, None


def _remember_validation(
    envelope: Mapping[str, Any],
    user: CurrentUser,
    session_id: str,
    store: AgentStore,
) -> None:
    if envelope.get("ok") is not True:
        return
    data = envelope.get("data")
    if not isinstance(data, dict) or data.get("valid") is not True:
        return
    ir_hash = data.get("ir_hash")
    if isinstance(ir_hash, str):
        store.record_validation(user.user_id, session_id, ir_hash)


def _failed(tool: str, code: str, message: str) -> dict[str, Any]:
    return error_envelope(tool, code, message, path=None, retryable=False, details=None)
