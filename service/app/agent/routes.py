"""HTTP and SSE surface for the author agent.

Sync SQLite, model calls, and tool execution run off the event loop.
``GET /health`` does not open the agent database. Spoofed owner headers
and query parameters are ignored. A non-local user is HTTP 403.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import threading
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.agent.config import (
    DEFAULT_MAX_STEPS,
    DEFAULT_MAX_TOOL_CALLS,
    HARD_MAX_STEPS,
    HARD_MAX_TOOL_CALLS,
    MAX_MESSAGE_CHARS,
    body_limit,
    busy_timeout_ms,
    db_path,
    default_tier,
)
from app.agent.errors import AgentError, StoreUnavailable
from app.agent.loop import TurnLimits, accept_proposal, reject_proposal, run_turn
from app.agent.providers import LLMProvider, provider_for
from app.agent.store import AgentStore
from app.auth import LOCAL_USER_ID, CurrentUser, CurrentUserDep
from app.jobs.routes import _queue as open_job_queue
from app.jobs.routes import error_response
from app.ohlcv.routes import _runtime as open_ohlcv_runtime
from app.strategies.routes import open_store
from app.tools.runtime import ToolRuntime

router = APIRouter()

_FORBIDDEN = "this process serves one user"
_UNAVAILABLE = "agent storage is unavailable"
_INTERNAL = "the request could not be completed"
_NOT_SESSION = "session was not found"
_NOT_PROPOSAL = "proposal was not found"
_TOO_LARGE = "request body is too large"
_INVALID_JSON = "request body is not JSON"
_INVALID_OBJECT = "request body must be a JSON object"
_NUMBER = re.compile(r"^[0-9]+$")
_HEX_ID = re.compile(r"^[0-9a-f]{32}$")
_STATUSES = frozenset({"pending", "applying", "accepted", "rejected", "conflict"})
_TIERS = frozenset({"fast", "balanced", "max"})
_SESSION_FIELDS = frozenset({"title", "tier", "strategy_id"})
_MESSAGE_FIELDS = frozenset({"content", "max_steps", "max_tool_calls"})
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100
_MAX_OFFSET = 10_000
_init_lock = threading.Lock()
_DONE = object()


class _Rejected(Exception):
    def __init__(self, code: str, message: str, path: str) -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(code)


def open_agent_store(app: Any) -> AgentStore:
    """Return the process store. A failed open does not stick on ``app.state``."""
    current = getattr(app.state, "agent_store", None)
    if isinstance(current, AgentStore):
        return current
    with _init_lock:
        current = getattr(app.state, "agent_store", None)
        if isinstance(current, AgentStore):
            return current
        try:
            path = db_path()
            timeout = busy_timeout_ms()
            if path.exists() and not path.is_file():
                raise StoreUnavailable()
            path.parent.mkdir(parents=True, exist_ok=True)
            store = AgentStore(path, busy_timeout_ms=timeout)
        except StoreUnavailable:
            raise
        except (OSError, sqlite3.Error, ValueError, AgentError):
            raise StoreUnavailable() from None
        app.state.agent_store = store
        return store


@router.post("/agent/sessions")
async def create_session(request: Request, user: CurrentUserDep) -> JSONResponse:
    denied = _local(user)
    if denied is not None:
        return denied
    raw = await _read_body(request, allow_empty=True)
    if isinstance(raw, JSONResponse):
        return raw
    parsed = _object(raw, allow_empty=True)
    if isinstance(parsed, JSONResponse):
        return parsed
    try:
        fields = _session_fields(parsed)
    except _Rejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    opened = await _open(request)
    if isinstance(opened, JSONResponse):
        return opened
    status, body = await run_in_threadpool(_guard, _create, opened, user, fields)
    response = JSONResponse(status_code=status, content=body)
    if status == 201 and isinstance(body, dict) and "session_id" in body:
        response.headers["Location"] = f"/agent/sessions/{body['session_id']}"
    return response


@router.get("/agent/sessions")
async def list_sessions(request: Request, user: CurrentUserDep) -> JSONResponse:
    denied = _local(user)
    if denied is not None:
        return denied
    try:
        limit, offset = _page(request.query_params)
    except _Rejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    opened = await _open(request)
    if isinstance(opened, JSONResponse):
        return opened
    status, body = await run_in_threadpool(_guard, _list, opened, user, limit, offset)
    return JSONResponse(status_code=status, content=body)


@router.get("/agent/sessions/{session_id}")
async def get_session(session_id: str, request: Request, user: CurrentUserDep) -> JSONResponse:
    denied = _local(user)
    if denied is not None:
        return denied
    ident = _ident(session_id, "session_id")
    if isinstance(ident, JSONResponse):
        return ident
    opened = await _open(request)
    if isinstance(opened, JSONResponse):
        return opened
    status, body = await run_in_threadpool(_guard, _session_detail, opened, user, ident)
    return JSONResponse(status_code=status, content=body)


@router.get("/agent/sessions/{session_id}/events")
async def list_events(session_id: str, request: Request, user: CurrentUserDep) -> JSONResponse:
    denied = _local(user)
    if denied is not None:
        return denied
    ident = _ident(session_id, "session_id")
    if isinstance(ident, JSONResponse):
        return ident
    try:
        after_seq = _after_seq(request.query_params)
    except _Rejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    opened = await _open(request)
    if isinstance(opened, JSONResponse):
        return opened
    status, body = await run_in_threadpool(_guard, _events, opened, user, ident, after_seq)
    return JSONResponse(status_code=status, content=body)


@router.post("/agent/sessions/{session_id}/messages")
async def post_message(session_id: str, request: Request, user: CurrentUserDep) -> JSONResponse:
    denied = _local(user)
    if denied is not None:
        return denied
    ident = _ident(session_id, "session_id")
    if isinstance(ident, JSONResponse):
        return ident
    raw = await _read_body(request, allow_empty=False)
    if isinstance(raw, JSONResponse):
        return raw
    parsed = _object(raw, allow_empty=False)
    if isinstance(parsed, JSONResponse):
        return parsed
    try:
        content, limits = _message_fields(parsed)
    except _Rejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    opened = await _open(request)
    if isinstance(opened, JSONResponse):
        return opened
    status, body = await run_in_threadpool(
        _guard,
        _message,
        opened,
        user,
        ident,
        content,
        limits,
        _injected(request),
        _runtime(request),
    )
    return JSONResponse(status_code=status, content=body)


@router.post("/agent/sessions/{session_id}/messages/stream")
async def stream_message(session_id: str, request: Request, user: CurrentUserDep):
    denied = _local(user)
    if denied is not None:
        return denied
    ident = _ident(session_id, "session_id")
    if isinstance(ident, JSONResponse):
        return ident
    raw = await _read_body(request, allow_empty=False)
    if isinstance(raw, JSONResponse):
        return raw
    parsed = _object(raw, allow_empty=False)
    if isinstance(parsed, JSONResponse):
        return parsed
    try:
        content, limits = _message_fields(parsed)
    except _Rejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    opened = await _open(request)
    if isinstance(opened, JSONResponse):
        return opened
    try:
        prepared = await run_in_threadpool(
            _prepare,
            opened,
            user,
            ident,
            _injected(request),
        )
    except AgentError as exc:
        return error_response(exc.status, exc.code, exc.message, exc.path)
    except (StoreUnavailable, sqlite3.Error, OSError):
        return error_response(503, "service_unavailable", _UNAVAILABLE, None)
    except AssertionError:
        return error_response(500, "internal_error", _INTERNAL, None)
    except Exception:
        return error_response(500, "internal_error", _INTERNAL, None)
    session, provider, provider_name, model = prepared
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()

    def worker() -> None:
        try:
            run_turn(
                store=opened,
                runtime=_runtime(request),
                provider=provider,
                user=user,
                session_id=session.session_id,
                content=content,
                tier=session.tier,
                provider_name=provider_name,
                model=model,
                limits=limits,
                on_event=lambda event: loop.call_soon_threadsafe(queue.put_nowait, event),
            )
            loop.call_soon_threadsafe(queue.put_nowait, _DONE)
        except Exception as exc:
            for event in _worker_failure_events(opened, user, ident, exc):
                loop.call_soon_threadsafe(queue.put_nowait, event)
            loop.call_soon_threadsafe(queue.put_nowait, _DONE)

    thread = threading.Thread(target=worker, name="agent-turn", daemon=True)
    thread.start()

    async def generate():
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                if isinstance(item, dict):
                    yield _sse(item)
        finally:
            # Join off the event loop. A disconnect cancels this task; a
            # synchronous join here blocked /health for the timeout.
            await asyncio.shield(run_in_threadpool(thread.join, 5))

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/agent/proposals")
async def list_proposals(request: Request, user: CurrentUserDep) -> JSONResponse:
    denied = _local(user)
    if denied is not None:
        return denied
    try:
        limit, offset = _page(request.query_params)
        session_id, status = _proposal_filters(request.query_params)
    except _Rejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    opened = await _open(request)
    if isinstance(opened, JSONResponse):
        return opened
    status_code, body = await run_in_threadpool(
        _guard,
        _proposals,
        opened,
        user,
        session_id,
        status,
        limit,
        offset,
    )
    return JSONResponse(status_code=status_code, content=body)


@router.get("/agent/proposals/{proposal_id}")
async def get_proposal(proposal_id: str, request: Request, user: CurrentUserDep) -> JSONResponse:
    denied = _local(user)
    if denied is not None:
        return denied
    ident = _ident(proposal_id, "proposal_id")
    if isinstance(ident, JSONResponse):
        return ident
    opened = await _open(request)
    if isinstance(opened, JSONResponse):
        return opened
    status, body = await run_in_threadpool(_guard, _proposal, opened, user, ident)
    return JSONResponse(status_code=status, content=body)


@router.post("/agent/proposals/{proposal_id}/accept")
async def post_accept(proposal_id: str, request: Request, user: CurrentUserDep) -> JSONResponse:
    return await _decide(proposal_id, request, user, accept=True)


@router.post("/agent/proposals/{proposal_id}/reject")
async def post_reject(proposal_id: str, request: Request, user: CurrentUserDep) -> JSONResponse:
    return await _decide(proposal_id, request, user, accept=False)


async def _decide(
    proposal_id: str,
    request: Request,
    user: CurrentUser,
    *,
    accept: bool,
) -> JSONResponse:
    denied = _local(user)
    if denied is not None:
        return denied
    ident = _ident(proposal_id, "proposal_id")
    if isinstance(ident, JSONResponse):
        return ident
    raw = await _read_body(request, allow_empty=True)
    if isinstance(raw, JSONResponse):
        return raw
    parsed = _object(raw, allow_empty=True)
    if isinstance(parsed, JSONResponse):
        return parsed
    try:
        _exact_fields(parsed, frozenset())
    except _Rejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    opened = await _open(request)
    if isinstance(opened, JSONResponse):
        return opened
    status, body = await run_in_threadpool(
        _guard,
        _accept if accept else _reject,
        opened,
        user,
        ident,
        _runtime(request),
    )
    return JSONResponse(status_code=status, content=body)


def _create(
    store: AgentStore, user: CurrentUser, fields: Mapping[str, Any]
) -> tuple[int, dict[str, Any]]:
    tier = fields.get("tier")
    if tier is None:
        tier = default_tier()
    record = store.create_session(
        user.user_id,
        title=fields.get("title"),
        tier=str(tier),
        strategy_id=fields.get("strategy_id"),
    )
    return 201, record.public()


def _list(
    store: AgentStore,
    user: CurrentUser,
    limit: int,
    offset: int,
) -> tuple[int, dict[str, Any]]:
    rows, total = store.list_sessions(user.user_id, limit=limit, offset=offset)
    return 200, {
        "sessions": [row.public() for row in rows],
        "limit": limit,
        "offset": offset,
        "total": total,
    }


def _session_detail(
    store: AgentStore, user: CurrentUser, session_id: str
) -> tuple[int, dict[str, Any]]:
    session = store.get_session(user.user_id, session_id)
    if session is None:
        raise AgentError("not_found", _NOT_SESSION, status=404)
    messages = store.list_messages(user.user_id, session_id)
    events = store.list_events(user.user_id, session_id, after_seq=None) or []
    proposals, _total = store.list_proposals(
        user.user_id,
        session_id=session_id,
        status=None,
        limit=_MAX_LIMIT,
        offset=0,
    )
    return 200, {
        "session": session.public(),
        "messages": [item.public() for item in messages],
        "events": events,
        "proposals": [item.detail() for item in proposals],
    }


def _events(
    store: AgentStore,
    user: CurrentUser,
    session_id: str,
    after_seq: int | None,
) -> tuple[int, dict[str, Any]]:
    events = store.list_events(user.user_id, session_id, after_seq=after_seq)
    if events is None:
        raise AgentError("not_found", _NOT_SESSION, status=404)
    return 200, {"session_id": session_id, "events": events}


def _message(
    store: AgentStore,
    user: CurrentUser,
    session_id: str,
    content: str,
    limits: TurnLimits,
    injected: LLMProvider | None,
    runtime: ToolRuntime,
) -> tuple[int, dict[str, Any]]:
    session, provider, provider_name, model = _prepare(store, user, session_id, injected)
    body = run_turn(
        store=store,
        runtime=runtime,
        provider=provider,
        user=user,
        session_id=session.session_id,
        content=content,
        tier=session.tier,
        provider_name=provider_name,
        model=model,
        limits=limits,
    )
    return 200, body


def _proposals(
    store: AgentStore,
    user: CurrentUser,
    session_id: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[int, dict[str, Any]]:
    if session_id is not None and store.get_session(user.user_id, session_id) is None:
        raise AgentError("not_found", _NOT_SESSION, status=404)
    rows, total = store.list_proposals(
        user.user_id,
        session_id=session_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return 200, {
        "proposals": [row.summary() for row in rows],
        "limit": limit,
        "offset": offset,
        "total": total,
    }


def _proposal(store: AgentStore, user: CurrentUser, proposal_id: str) -> tuple[int, dict[str, Any]]:
    row = store.get_proposal(user.user_id, proposal_id)
    if row is None:
        raise AgentError("not_found", _NOT_PROPOSAL, status=404)
    return 200, row.detail()


def _accept(
    store: AgentStore,
    user: CurrentUser,
    proposal_id: str,
    runtime: ToolRuntime,
) -> tuple[int, dict[str, Any]]:
    return 200, accept_proposal(store=store, runtime=runtime, user=user, proposal_id=proposal_id)


def _reject(
    store: AgentStore,
    user: CurrentUser,
    proposal_id: str,
    runtime: ToolRuntime,
) -> tuple[int, dict[str, Any]]:
    return 200, reject_proposal(store=store, runtime=runtime, user=user, proposal_id=proposal_id)


def _prepare(
    store: AgentStore,
    user: CurrentUser,
    session_id: str,
    injected: LLMProvider | None,
):
    session = store.get_session(user.user_id, session_id)
    if session is None:
        raise AgentError("not_found", _NOT_SESSION, status=404)
    provider, provider_name, model = provider_for(session.tier, injected)
    return session, provider, provider_name, model


def _guard(fn: Any, *args: Any) -> tuple[int, dict[str, Any]]:
    try:
        return fn(*args)
    except AgentError as exc:
        return exc.status, {"error": {"code": exc.code, "message": exc.message, "path": exc.path}}
    except (StoreUnavailable, sqlite3.Error, OSError):
        return 503, {
            "error": {"code": "service_unavailable", "message": _UNAVAILABLE, "path": None}
        }
    except AssertionError:
        return 500, {"error": {"code": "internal_error", "message": _INTERNAL, "path": None}}
    except Exception:
        return 500, {"error": {"code": "internal_error", "message": _INTERNAL, "path": None}}


def _runtime(request: Request) -> ToolRuntime:
    return ToolRuntime(
        open_strategies=lambda: open_store(request.app),
        open_jobs=lambda: open_job_queue(request),
        open_ohlcv=lambda: open_ohlcv_runtime(request),
    )


def _injected(request: Request) -> LLMProvider | None:
    current = getattr(request.app.state, "llm_provider", None)
    if current is None or not callable(getattr(current, "complete", None)):
        return None
    return current


async def _open(request: Request) -> AgentStore | JSONResponse:
    try:
        return await run_in_threadpool(open_agent_store, request.app)
    except StoreUnavailable:
        return error_response(503, "service_unavailable", _UNAVAILABLE, None)
    except (sqlite3.Error, OSError):
        return error_response(503, "service_unavailable", _UNAVAILABLE, None)


def _local(user: CurrentUser) -> JSONResponse | None:
    if user.user_id != LOCAL_USER_ID:
        return error_response(403, "forbidden", _FORBIDDEN, None)
    return None


def _ident(raw: str, name: str) -> str | JSONResponse:
    if not _HEX_ID.fullmatch(raw):
        return error_response(422, "invalid_id", f"{name} is invalid", f"$.{name}")
    return raw


async def _read_body(request: Request, *, allow_empty: bool) -> bytes | JSONResponse:
    try:
        limit = body_limit()
    except AgentError as exc:
        return error_response(exc.status, exc.code, exc.message, exc.path)
    declared = request.headers.getlist("content-length")
    if len(declared) > 1:
        return error_response(400, "invalid_request", "content length is invalid", None)
    if len(declared) == 1:
        invalid = _declared_too_big(declared[0], limit)
        if invalid is not None:
            return invalid
    buf = bytearray()
    async for chunk in request.stream():
        if len(chunk) > limit - len(buf):
            return error_response(413, "payload_too_large", _TOO_LARGE, None)
        buf.extend(chunk)
    if b"\x00" in buf:
        return error_response(422, "invalid_string", "text contains a NUL byte", None)
    if buf == b"" and not allow_empty:
        return error_response(422, "invalid_json", _INVALID_JSON, None)
    return bytes(buf)


def _object(raw: bytes, *, allow_empty: bool) -> Mapping[str, Any] | JSONResponse:
    if raw == b"" and allow_empty:
        return {}
    if raw == b"":
        return error_response(422, "invalid_json", _INVALID_JSON, None)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return error_response(422, "invalid_json", _INVALID_JSON, None)
    try:
        payload = json.loads(text, parse_float=_parse_float, parse_constant=_reject_non_finite)
    except RecursionError:
        return error_response(422, "expr_too_deep", "expression nested too deeply", None)
    except (json.JSONDecodeError, ValueError, UnicodeError):
        return error_response(422, "invalid_json", _INVALID_JSON, None)
    if isinstance(payload, bool) or not isinstance(payload, dict):
        return error_response(422, "invalid_json", _INVALID_OBJECT, None)
    return payload


def _session_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    _exact_fields(payload, _SESSION_FIELDS)
    fields: dict[str, Any] = {}
    if "title" in payload:
        title = payload["title"]
        if not isinstance(title, str) or not 1 <= len(title) <= 200 or "\x00" in title:
            raise _Rejected("validation_error", "title is not valid", "$.title")
        fields["title"] = title
    if "tier" in payload:
        tier = payload["tier"]
        if not isinstance(tier, str) or tier not in _TIERS:
            raise _Rejected("validation_error", "tier is not valid", "$.tier")
        fields["tier"] = tier
    if "strategy_id" in payload:
        strategy_id = payload["strategy_id"]
        if not isinstance(strategy_id, str) or not _HEX_ID.fullmatch(strategy_id):
            raise _Rejected("validation_error", "strategy_id is not valid", "$.strategy_id")
        fields["strategy_id"] = strategy_id
    return fields


def _message_fields(payload: Mapping[str, Any]) -> tuple[str, TurnLimits]:
    _exact_fields(payload, _MESSAGE_FIELDS)
    content = payload.get("content")
    if not isinstance(content, str) or content == "" or "\x00" in content:
        raise _Rejected("validation_error", "content is not valid", "$.content")
    if len(content) > MAX_MESSAGE_CHARS:
        raise _Rejected("validation_error", "content is too long", "$.content")
    max_steps = _optional_int(
        payload.get("max_steps"),
        present="max_steps" in payload,
        default=DEFAULT_MAX_STEPS,
        high=HARD_MAX_STEPS,
        path="$.max_steps",
    )
    max_tools = _optional_int(
        payload.get("max_tool_calls"),
        present="max_tool_calls" in payload,
        default=DEFAULT_MAX_TOOL_CALLS,
        high=HARD_MAX_TOOL_CALLS,
        path="$.max_tool_calls",
    )
    return content, TurnLimits(max_steps=max_steps, max_tool_calls=max_tools)


def _optional_int(value: object, *, present: bool, default: int, high: int, path: str) -> int:
    if not present:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= high:
        raise _Rejected("validation_error", f"{path[2:]} is not valid", path)
    return value


def _exact_fields(payload: Mapping[str, Any], allowed: frozenset[str]) -> None:
    for key in payload:
        if key not in allowed:
            raise _Rejected("validation_error", "unknown field", f"$.{key}")


def _page(params: Any) -> tuple[int, int]:
    limit = _query_int(
        params,
        "limit",
        default=_DEFAULT_LIMIT,
        low=1,
        high=_MAX_LIMIT,
        message="limit must be a whole number from 1 to 100",
    )
    offset = _query_int(
        params,
        "offset",
        default=0,
        low=0,
        high=_MAX_OFFSET,
        message="offset must be a whole number from 0 to 10000",
    )
    return limit, offset


def _after_seq(params: Any) -> int | None:
    raw = _single(params, "after_seq")
    if raw is None:
        return None
    return _bounded_text(
        raw,
        low=0,
        high=1_000_000_000,
        path="$.after_seq",
        message="after_seq must be a whole number from 0 to 1000000000",
    )


def _proposal_filters(params: Any) -> tuple[str | None, str | None]:
    session_id = _single(params, "session_id")
    if session_id is not None and not _HEX_ID.fullmatch(session_id):
        raise _Rejected("validation_error", "session_id is not valid", "$.session_id")
    status = _single(params, "status")
    if status is not None and status not in _STATUSES:
        raise _Rejected("validation_error", "status is not valid", "$.status")
    return session_id, status


def _query_int(params: Any, name: str, *, default: int, low: int, high: int, message: str) -> int:
    raw = _single(params, name)
    if raw is None:
        return default
    return _bounded_text(raw, low=low, high=high, path=f"$.{name}", message=message)


def _single(params: Any, name: str) -> str | None:
    values = params.getlist(name)
    if not values:
        return None
    if len(values) > 1:
        raise _Rejected("invalid_query", f"{name} must be sent once", f"$.{name}")
    return values[0]


def _bounded_text(raw: str, *, low: int, high: int, path: str, message: str) -> int:
    if not _NUMBER.fullmatch(raw) or len(raw) > 10 or (len(raw) > 1 and raw.startswith("0")):
        raise _Rejected("invalid_query", message, path)
    value = int(raw)
    if value < low or value > high:
        raise _Rejected("invalid_query", message, path)
    return value


def _declared_too_big(raw: str, limit: int) -> JSONResponse | None:
    if not _NUMBER.fullmatch(raw) or (len(raw) > 1 and raw.startswith("0")):
        return error_response(400, "invalid_request", "content length is invalid", None)
    if len(raw) > 8 or int(raw) > limit:
        return error_response(413, "payload_too_large", _TOO_LARGE, None)
    return None


def _parse_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("non-finite number")
    return number


def _reject_non_finite(_text: str) -> float:
    raise ValueError("non-finite number")


def _worker_failure_events(
    store: AgentStore,
    user: CurrentUser,
    session_id: str,
    exc: Exception,
) -> list[dict[str, Any]]:
    """Persist the worker failure at the next sequence, then close the turn."""
    code, message = _public_error(exc)
    payload = {"code": code, "message": message, "path": None}
    try:
        existing = store.list_events(user.user_id, session_id, after_seq=None) or []
        turn_id = existing[-1]["turn_id"] if existing else None
        error_event = store.append_event(
            user.user_id,
            session_id,
            turn_id=turn_id,
            event_type="error",
            payload=payload,
        )
        complete = store.append_event(
            user.user_id,
            session_id,
            turn_id=turn_id,
            event_type="turn_complete",
            payload={"status": "error", "turn_id": turn_id, "error": payload},
        )
        return [error_event, complete]
    except Exception:
        return [
            _unpersisted_event(session_id, "error", payload),
            _unpersisted_event(
                session_id,
                "turn_complete",
                {"status": "error", "turn_id": None, "error": payload},
            ),
        ]


def _public_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, AgentError):
        return exc.code, exc.message
    if isinstance(exc, (StoreUnavailable, sqlite3.Error, OSError)):
        return "service_unavailable", _UNAVAILABLE
    return "internal_error", _INTERNAL


def _unpersisted_event(session_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": 0,
        "event_id": event_type,
        "session_id": session_id,
        "turn_id": None,
        "type": event_type,
        "created_at": "1970-01-01T00:00:00Z",
        "payload": payload,
    }


def _sse(event: Mapping[str, Any]) -> str:
    data = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"
