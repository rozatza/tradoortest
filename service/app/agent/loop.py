"""One author turn: model, tools, proposals, and a bounded step budget.

Blocking work belongs in this module. HTTP handlers call it from a worker
thread. The model cannot apply an existing strategy or start a backtest
that has not been validated.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.agent.config import (
    DEFAULT_MAX_STEPS,
    DEFAULT_MAX_TOOL_CALLS,
    MAX_TOOL_RESULT_CHARS,
)
from app.agent.errors import AgentError
from app.agent.guardrails import run_tool
from app.agent.providers import ChatMessage, LLMProvider, ProviderRequest, ToolCall
from app.agent.sanitize import sanitize_tool_payload
from app.agent.store import AgentStore, MessageRecord, ProposalRecord
from app.auth import CurrentUser
from app.strategies.errors import StrategyNotFound, VersionNotFound
from app.tools.dispatch import Dispatcher
from app.tools.errors import http_status, safe_text
from app.tools.registry import export_specs
from app.tools.runtime import ToolRuntime

_SYSTEM = (
    "You are the strategy author for one local user. "
    "Use the tools to read the reference, validate a strategy, and preview edits. "
    "update_strategy never writes. An edit becomes a pending proposal the user accepts or rejects. "
    "Call validate and wait until it returns valid true for the exact strategy "
    "before backtest_start. "
    "Do not ask for wallets, keys, signing, or swaps."
)
_STEP_LIMIT = "the step limit was reached"
_TOOL_LIMIT = "the tool limit was reached"
_UNCHANGED = "strategy is unchanged"
_CONFLICT = "strategy version conflict"
_DECIDED = "proposal is already decided"
_MISSING_PROPOSAL = "proposal was not found"
_MISMATCH = "applied strategy does not match the proposal"
_INTERNAL = "the request could not be completed"
_proposal_locks: dict[str, threading.Lock] = {}
_proposal_locks_guard = threading.Lock()

OnEvent = Callable[[dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class TurnLimits:
    """Caps for one turn. Values above the hard max are rejected by the route."""

    max_steps: int = DEFAULT_MAX_STEPS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS


def llm_tools() -> list[dict[str, Any]]:
    """Tool definitions for a model. Schemas are the exported ECMA-262 documents."""
    tools: list[dict[str, Any]] = []
    for spec in export_specs():
        description = str(spec["description"])
        if spec["name"] == "update_strategy":
            description += " Edits create a pending proposal for user approval and do not write."
        tools.append(
            {
                "name": spec["name"],
                "description": description,
                "parameters": spec["input_schema"],
            }
        )
    return tools


def run_turn(
    *,
    store: AgentStore,
    runtime: ToolRuntime,
    provider: LLMProvider,
    user: CurrentUser,
    session_id: str,
    content: str,
    tier: str,
    provider_name: str,
    model: str,
    limits: TurnLimits,
    on_event: OnEvent | None = None,
) -> dict[str, Any]:
    """Run one user message to completion, a limit, or a provider error."""
    session = store.get_session(user.user_id, session_id)
    if session is None:
        raise AgentError("not_found", "session was not found", status=404)
    turn_id = _new_id()
    dispatcher = Dispatcher(runtime)
    events: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []

    def emit(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = store.append_event(
            user.user_id,
            session_id,
            turn_id=turn_id,
            event_type=event_type,
            payload=payload,
        )
        events.append(event)
        if on_event is not None:
            on_event(event)
        return event

    store.add_message(
        user.user_id,
        session_id,
        turn_id=turn_id,
        role="user",
        content=content,
    )
    emit("user_message", {"role": "user", "content": content})
    context = _history(store.list_messages(user.user_id, session_id))
    tools = tuple(llm_tools())
    steps = 0
    used_tools = 0
    assistant_text: str | None = None
    error: dict[str, Any] | None = None
    status = "completed"
    seen_provider = provider_name
    seen_model = model

    while True:
        if steps >= limits.max_steps:
            error = _error("step_limit_exceeded", _STEP_LIMIT)
            emit("error", error)
            status = "stopped_limit"
            break
        steps += 1
        try:
            response = provider.complete(
                ProviderRequest(
                    system_prompt=_SYSTEM,
                    messages=tuple(context),
                    tools=tools,
                    tier=tier,
                    model=model,
                )
            )
        except AgentError as exc:
            error = _error(exc.code, exc.message, exc.path)
            emit("error", error)
            status = "error"
            break
        seen_provider = response.provider or seen_provider
        seen_model = response.model or seen_model
        if response.tool_calls:
            room = limits.max_tool_calls - used_tools
            planned = response.tool_calls[:room] if room > 0 else ()
            overflow = len(response.tool_calls) > len(planned)
            if planned:
                _remember_assistant(
                    store=store,
                    context=context,
                    user=user,
                    session_id=session_id,
                    turn_id=turn_id,
                    text=response.assistant_text,
                    calls=planned,
                )
            for call in planned:
                used_tools += 1
                _execute_call(
                    call=call,
                    fallback_id=f"call{used_tools}",
                    context=context,
                    store=store,
                    user=user,
                    session_id=session_id,
                    turn_id=turn_id,
                    dispatcher=dispatcher,
                    emit=emit,
                    proposals=proposals,
                )
            if overflow or (not planned and response.tool_calls):
                error = _error("tool_limit_exceeded", _TOOL_LIMIT)
                emit("error", error)
                status = "stopped_limit"
                break
            continue
        saved = store.add_message(
            user.user_id,
            session_id,
            turn_id=turn_id,
            role="assistant",
            content=response.assistant_text,
        )
        assistant_text = saved.content
        emit("assistant_message", {"role": "assistant", "content": saved.content})
        status = "completed"
        break

    emit("turn_complete", {"status": status, "turn_id": turn_id, "error": error})
    return {
        "turn_id": turn_id,
        "session_id": session_id,
        "status": status,
        "tier": tier,
        "provider": seen_provider,
        "model": seen_model,
        "assistant_message": assistant_text,
        "events": events,
        "proposals": proposals,
        "error": error,
    }


def accept_proposal(
    *,
    store: AgentStore,
    runtime: ToolRuntime,
    user: CurrentUser,
    proposal_id: str,
) -> dict[str, Any]:
    """Apply a pending proposal once. A second accept does not write again."""
    with _proposal_lock(proposal_id):
        return _accept_locked(store=store, runtime=runtime, user=user, proposal_id=proposal_id)


def _accept_locked(
    *,
    store: AgentStore,
    runtime: ToolRuntime,
    user: CurrentUser,
    proposal_id: str,
) -> dict[str, Any]:
    current = store.get_proposal(user.user_id, proposal_id)
    if current is None:
        raise AgentError("not_found", _MISSING_PROPOSAL, status=404)
    if current.status == "applying":
        current, event = _reconcile_applying(store, runtime, user, current)
        if current.status == "accepted":
            return _accepted_body(current, event)
    if current.status != "pending":
        raise AgentError("proposal_already_decided", _DECIDED, status=409)
    claimed = store.settle_proposal(
        user.user_id,
        proposal_id,
        expect="pending",
        status="applying",
        decided_at=None,
        applied_version=None,
        applied_ir_hash=None,
        event_type=None,
        event_payload=None,
        turn_id=None,
    )
    if claimed is None:
        raise AgentError("not_found", _MISSING_PROPOSAL, status=404)
    if not claimed.changed:
        raise AgentError("proposal_already_decided", _DECIDED, status=409)
    proposal = claimed.proposal
    envelope = Dispatcher(runtime).invoke(
        "update_strategy",
        {
            "strategy_id": proposal.strategy_id,
            "dsl": proposal.candidate_dsl,
            "mode": "apply",
            "confirm": True,
            "expected_version": proposal.from_version,
            "expected_ir_hash": proposal.from_ir_hash,
        },
        user,
    )
    if not envelope.get("ok"):
        _fail_accept(store, user, proposal, envelope)
    data = envelope.get("data")
    if not isinstance(data, dict) or "version" not in data or "ir_hash" not in data:
        _release_claim(store, user, proposal)
        raise AgentError("internal_error", _INTERNAL, status=500)
    version = int(data["version"])
    ir_hash = str(data["ir_hash"])
    _assert_applied_hash(proposal, ir_hash)
    settled = store.settle_proposal(
        user.user_id,
        proposal.proposal_id,
        expect="applying",
        status="accepted",
        decided_at=store.now(),
        applied_version=version,
        applied_ir_hash=ir_hash,
        event_type="proposal_decided",
        event_payload={
            "proposal_id": proposal.proposal_id,
            "decision": "accepted",
            "status": "accepted",
            "strategy_id": proposal.strategy_id,
            "applied_version": version,
            "applied_ir_hash": ir_hash,
        },
        turn_id=proposal.turn_id,
    )
    if settled is None or not settled.changed:
        raise AgentError("internal_error", _INTERNAL, status=500)
    return {
        "proposal": settled.proposal.detail(),
        "strategy": {
            "strategy_id": str(data["strategy_id"]),
            "version": version,
            "ir_hash": ir_hash,
        },
        "event": settled.event,
    }


def reject_proposal(
    *,
    store: AgentStore,
    runtime: ToolRuntime,
    user: CurrentUser,
    proposal_id: str,
) -> dict[str, Any]:
    """Mark a pending proposal rejected. The strategy store is not touched."""
    with _proposal_lock(proposal_id):
        return _reject_locked(store=store, runtime=runtime, user=user, proposal_id=proposal_id)


def _reject_locked(
    *,
    store: AgentStore,
    runtime: ToolRuntime,
    user: CurrentUser,
    proposal_id: str,
) -> dict[str, Any]:
    current = store.get_proposal(user.user_id, proposal_id)
    if current is None:
        raise AgentError("not_found", _MISSING_PROPOSAL, status=404)
    if current.status == "applying":
        current, _event = _reconcile_applying(store, runtime, user, current)
    if current.status != "pending":
        raise AgentError("proposal_already_decided", _DECIDED, status=409)
    settled = store.settle_proposal(
        user.user_id,
        proposal_id,
        expect="pending",
        status="rejected",
        decided_at=store.now(),
        applied_version=None,
        applied_ir_hash=None,
        event_type="proposal_decided",
        event_payload={
            "proposal_id": current.proposal_id,
            "decision": "rejected",
            "status": "rejected",
            "strategy_id": current.strategy_id,
        },
        turn_id=current.turn_id,
    )
    if settled is None:
        raise AgentError("not_found", _MISSING_PROPOSAL, status=404)
    if not settled.changed:
        raise AgentError("proposal_already_decided", _DECIDED, status=409)
    return {"proposal": settled.proposal.detail(), "event": settled.event}


def _remember_assistant(
    *,
    store: AgentStore,
    context: list[ChatMessage],
    user: CurrentUser,
    session_id: str,
    turn_id: str,
    text: str,
    calls: tuple[ToolCall, ...],
) -> None:
    safe_calls = tuple(_sanitized_call(call) for call in calls)
    store.add_message(
        user.user_id,
        session_id,
        turn_id=turn_id,
        role="assistant",
        content=text,
        tool_calls=[_stored_call(call) for call in safe_calls],
    )
    context.append(ChatMessage(role="assistant", content=text, tool_calls=safe_calls))


def _execute_call(
    *,
    call: ToolCall,
    fallback_id: str,
    context: list[ChatMessage],
    store: AgentStore,
    user: CurrentUser,
    session_id: str,
    turn_id: str,
    dispatcher: Dispatcher,
    emit: Callable[[str, dict[str, Any]], dict[str, Any]],
    proposals: list[dict[str, Any]],
) -> None:
    call_id = call.call_id or fallback_id
    parsed, ok = _parse_arguments(call.arguments)
    arguments = parsed if ok and isinstance(parsed, dict) else {}
    emit(
        "tool_start",
        {
            "tool": call.name,
            "call_id": call_id,
            "arguments": sanitize_tool_payload(call.name, "arguments", arguments),
        },
    )
    proposal: ProposalRecord | None
    if not ok or not isinstance(parsed, dict):
        envelope = {
            "ok": False,
            "tool": call.name,
            "error": {
                "code": "validation_error",
                "message": "arguments are not valid JSON",
                "path": "$",
                "retryable": False,
                "details": None,
            },
        }
        proposal = None
    else:
        envelope, proposal = run_tool(
            name=call.name,
            arguments=parsed,
            user=user,
            session_id=session_id,
            turn_id=turn_id,
            dispatcher=dispatcher,
            store=store,
        )
    emit(
        "tool_complete",
        {
            "tool": call.name,
            "call_id": call_id,
            "ok": bool(envelope.get("ok")),
            "output": sanitize_tool_payload(call.name, "result", envelope),
        },
    )
    if proposal is not None:
        proposals.append(proposal.detail())
        emit("proposal_created", _proposal_event(proposal))
    store.add_message(
        user.user_id,
        session_id,
        turn_id=turn_id,
        role="tool",
        content=_stored_tool_content(call.name, envelope),
        tool_call_id=call_id,
        tool_name=call.name,
    )
    context.append(
        ChatMessage(
            role="tool",
            content=_model_json(call.name, envelope),
            tool_call_id=call_id,
            tool_name=call.name,
        )
    )


def _stored_tool_content(tool: str, envelope: dict[str, Any]) -> str:
    return json.dumps(
        sanitize_tool_payload(tool, "result", envelope),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _sanitized_call(call: ToolCall) -> ToolCall:
    parsed, ok = _parse_arguments(call.arguments)
    arguments = parsed if ok and isinstance(parsed, dict) else {}
    return ToolCall(
        call.call_id,
        call.name,
        sanitize_tool_payload(call.name, "arguments", arguments),
    )


def _stored_call(call: ToolCall) -> dict[str, Any]:
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    return {
        "call_id": call.call_id,
        "name": call.name,
        "arguments": sanitize_tool_payload(call.name, "arguments", arguments),
    }


def _proposal_event(proposal: ProposalRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "proposal_id": proposal.proposal_id,
        "strategy_id": proposal.strategy_id,
        "from_version": proposal.from_version,
        "to_version": proposal.to_version,
        "from_ir_hash": proposal.from_ir_hash,
        "to_ir_hash": proposal.to_ir_hash,
        "status": proposal.status,
        "dsl_diff": proposal.dsl_diff,
        "ir_diff": proposal.ir_diff,
    }
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    if len(encoded) <= 24_000:
        return payload
    return {
        "proposal_id": proposal.proposal_id,
        "strategy_id": proposal.strategy_id,
        "from_version": proposal.from_version,
        "to_version": proposal.to_version,
        "from_ir_hash": proposal.from_ir_hash,
        "to_ir_hash": proposal.to_ir_hash,
        "status": proposal.status,
        "diffs_omitted": True,
    }


def _fail_accept(
    store: AgentStore,
    user: CurrentUser,
    proposal: ProposalRecord,
    envelope: dict[str, Any],
) -> None:
    error = envelope.get("error")
    code = "internal_error"
    message = _INTERNAL
    path = None
    if isinstance(error, dict):
        if isinstance(error.get("code"), str):
            code = error["code"]
        if isinstance(error.get("message"), str):
            message = safe_text(error["message"], _INTERNAL)
        if isinstance(error.get("path"), str) or error.get("path") is None:
            path = error.get("path")
    if code == "version_conflict":
        store.settle_proposal(
            user.user_id,
            proposal.proposal_id,
            expect="applying",
            status="conflict",
            decided_at=store.now(),
            applied_version=None,
            applied_ir_hash=None,
            event_type="proposal_decided",
            event_payload={
                "proposal_id": proposal.proposal_id,
                "decision": "conflict",
                "status": "conflict",
                "strategy_id": proposal.strategy_id,
            },
            turn_id=proposal.turn_id,
        )
        raise AgentError("version_conflict", _CONFLICT, path="$.expected_version", status=409)
    store.settle_proposal(
        user.user_id,
        proposal.proposal_id,
        expect="applying",
        status="pending",
        decided_at=None,
        applied_version=None,
        applied_ir_hash=None,
        event_type=None,
        event_payload=None,
        turn_id=proposal.turn_id,
    )
    if code == "no_changes":
        raise AgentError("no_changes", _UNCHANGED, status=409)
    raise AgentError(
        code, message, path=path if isinstance(path, str) else None, status=http_status(code)
    )


def _history(messages: list[MessageRecord]) -> list[ChatMessage]:
    context: list[ChatMessage] = []
    for message in messages:
        context.append(
            ChatMessage(
                role=message.role,
                content=message.content,
                tool_calls=_calls_from_json(message.tool_calls_json),
                tool_call_id=message.tool_call_id,
                tool_name=message.tool_name,
            )
        )
    return context


def _calls_from_json(text: str | None) -> tuple[ToolCall, ...]:
    if not text:
        return ()
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return ()
    if not isinstance(loaded, list):
        return ()
    calls: list[ToolCall] = []
    for item in loaded:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        call_id = item.get("call_id")
        if not isinstance(name, str) or not isinstance(call_id, str):
            continue
        calls.append(ToolCall(call_id, name, item.get("arguments") or {}))
    return tuple(calls)


def _parse_arguments(raw: object) -> tuple[object, bool]:
    if isinstance(raw, dict):
        return raw, True
    if isinstance(raw, str):
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return None, False
        return loaded, True
    return None, False


def _model_json(tool: str, payload: object) -> str:
    safe = sanitize_tool_payload(tool, "result", payload) if isinstance(payload, dict) else payload
    try:
        text = json.dumps(safe, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return "{}"
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return json.dumps(
        {"truncated": True, "preview": text[:MAX_TOOL_RESULT_CHARS]},
        ensure_ascii=False,
    )


def _proposal_lock(proposal_id: str) -> threading.Lock:
    with _proposal_locks_guard:
        lock = _proposal_locks.get(proposal_id)
        if lock is None:
            lock = threading.Lock()
            _proposal_locks[proposal_id] = lock
        return lock


def _reconcile_applying(
    store: AgentStore,
    runtime: ToolRuntime,
    user: CurrentUser,
    proposal: ProposalRecord,
) -> tuple[ProposalRecord, dict[str, Any] | None]:
    """Finish a proposal left in ``applying`` by a crashed accept.

    A saved target version whose hash is the proposal hash is accepted.
    A missing target version goes back to pending. A different hash is
    ``conflict`` and is not accepted.
    """
    loaded = _stored_target(runtime, user, proposal)
    if loaded is not None and loaded[1] == proposal.to_ir_hash:
        version, ir_hash = loaded
        settled = store.settle_proposal(
            user.user_id,
            proposal.proposal_id,
            expect="applying",
            status="accepted",
            decided_at=store.now(),
            applied_version=version,
            applied_ir_hash=ir_hash,
            event_type="proposal_decided",
            event_payload={
                "proposal_id": proposal.proposal_id,
                "decision": "accepted",
                "status": "accepted",
                "strategy_id": proposal.strategy_id,
                "applied_version": version,
                "applied_ir_hash": ir_hash,
            },
            turn_id=proposal.turn_id,
        )
    elif loaded is None:
        settled = store.settle_proposal(
            user.user_id,
            proposal.proposal_id,
            expect="applying",
            status="pending",
            decided_at=None,
            applied_version=None,
            applied_ir_hash=None,
            event_type=None,
            event_payload=None,
            turn_id=proposal.turn_id,
        )
    else:
        settled = store.settle_proposal(
            user.user_id,
            proposal.proposal_id,
            expect="applying",
            status="conflict",
            decided_at=store.now(),
            applied_version=None,
            applied_ir_hash=None,
            event_type="proposal_decided",
            event_payload={
                "proposal_id": proposal.proposal_id,
                "decision": "conflict",
                "status": "conflict",
                "strategy_id": proposal.strategy_id,
            },
            turn_id=proposal.turn_id,
        )
    if settled is None:
        raise AgentError("not_found", _MISSING_PROPOSAL, status=404)
    return settled.proposal, settled.event


def _stored_target(
    runtime: ToolRuntime,
    user: CurrentUser,
    proposal: ProposalRecord,
) -> tuple[int, str] | None:
    try:
        loaded = runtime.strategies().get_version(
            user.user_id, proposal.strategy_id, proposal.to_version
        )
    except (StrategyNotFound, VersionNotFound):
        return None
    return loaded.version, loaded.ir_hash


def _accepted_body(proposal: ProposalRecord, event: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "proposal": proposal.detail(),
        "strategy": {
            "strategy_id": proposal.strategy_id,
            "version": proposal.applied_version,
            "ir_hash": proposal.applied_ir_hash,
        },
        "event": event,
    }


def _assert_applied_hash(proposal: ProposalRecord, ir_hash: str) -> None:
    """Refuse to record an accept when the write is not the candidate hash."""
    if ir_hash != proposal.to_ir_hash:
        raise AgentError("internal_error", _MISMATCH, status=500)


def _release_claim(store: AgentStore, user: CurrentUser, proposal: ProposalRecord) -> None:
    store.settle_proposal(
        user.user_id,
        proposal.proposal_id,
        expect="applying",
        status="pending",
        decided_at=None,
        applied_version=None,
        applied_ir_hash=None,
        event_type=None,
        event_payload=None,
        turn_id=proposal.turn_id,
    )


def _error(code: str, message: str, path: str | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "path": path}


def _new_id() -> str:
    return uuid.uuid4().hex
