"""CRUD and versioned saves for one user's strategies.

``PUT /strategies/{strategy_id}`` appends an immutable version. It does not
edit version 1. When the body includes both ``expected_version`` and
``expected_ir_hash``, a stale pair is HTTP 409 and writes nothing. Omitting
both still appends. ``POST /strategies/{strategy_id}/validate`` compiles and
does not write. ``DELETE`` archives. An archived strategy is 404 on every
route except ``GET /strategies?include_archived=true``.

Sync SQLite and the Phase 1 parser run in a thread pool. The store is
opened under ``_init_lock`` and assigned to ``app.state`` only after the
file is ready.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import QueryParams

from app.auth import LOCAL_USER_ID, CurrentUser, CurrentUserDep
from app.strategies.compile import compile_body
from app.strategies.errors import (
    Rejected,
    StoreUnavailable,
    StrategyNotFound,
    VersionConflict,
    VersionNotFound,
)
from app.strategies.store import (
    StrategyPage,
    StrategyRecord,
    StrategyStore,
    VersionPage,
    VersionRecord,
)

router = APIRouter()

_DEFAULT_DB = Path("data/strategies.sqlite")
_DEFAULT_MAX_BODY = 65_536
_MAX_CONFIG_BODY = 1_048_576
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100
_MAX_OFFSET = 10_000
_MAX_VERSION = 1_000_000

_UNAVAILABLE = "strategy storage is unavailable"
_FORBIDDEN = "this process serves one user"
_NOT_FOUND = "strategy was not found"
_VERSION_NOT_FOUND = "strategy version was not found"
_TOO_LARGE = "request body is too large"
_INTERNAL = "the request could not be completed"

_NUMBER = re.compile(r"^[0-9]+$")
_STRATEGY_ID = re.compile(r"^[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")

# Serializes the first open. A failed open does not write app.state.
_init_lock = threading.Lock()


class QueryRejected(Exception):
    """A query or path parameter is not in the documented set."""

    def __init__(self, code: str, message: str, path: str) -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(code)


def error_response(
    status: int,
    code: str,
    message: str,
    path: str | None,
    diagnostics: list[dict[str, object]] | None = None,
    *,
    current_version: int | None = None,
    current_ir_hash: str | None = None,
) -> JSONResponse:
    """JSON ``{error}`` envelope. ``message`` is a fixed sentence."""
    error: dict[str, object] = {"code": code, "message": message, "path": path}
    if diagnostics:
        error["diagnostics"] = diagnostics
    if current_version is not None:
        error["current_version"] = current_version
    if current_ir_hash is not None:
        error["current_ir_hash"] = current_ir_hash
    return JSONResponse(status_code=status, content={"error": error})


def open_store(app: Any) -> StrategyStore:
    """Return the process store, opening it once.

    The assignment happens after a successful constructor. An unwritable
    path, a corrupt file, or a bad env value leaves ``app.state.strategies``
    as it was.
    """
    current = getattr(app.state, "strategies", None)
    if isinstance(current, StrategyStore):
        return current
    with _init_lock:
        current = getattr(app.state, "strategies", None)
        if isinstance(current, StrategyStore):
            return current
        store = _connect()
        app.state.strategies = store
        return store


def db_path() -> Path:
    """``STRATEGIES_DB_PATH``, or ``data/strategies.sqlite``."""
    raw = os.environ.get("STRATEGIES_DB_PATH")
    if raw is None or raw.strip() == "":
        return _DEFAULT_DB
    text = raw.strip()
    if len(text) > 4096 or "\x00" in text:
        raise ValueError
    return Path(text)


def busy_timeout_ms() -> int:
    """``STRATEGIES_BUSY_TIMEOUT_MS``, or 5000. Range 0 to 60000."""
    raw = os.environ.get("STRATEGIES_BUSY_TIMEOUT_MS")
    if raw is None or raw.strip() == "":
        return 5000
    return _env_int(raw, low=0, high=60_000)


def body_limit() -> int:
    """``STRATEGIES_MAX_BODY_BYTES``, or 65536. Range 1 to 1048576."""
    raw = os.environ.get("STRATEGIES_MAX_BODY_BYTES")
    if raw is None or raw.strip() == "":
        return _DEFAULT_MAX_BODY
    return _env_int(raw, low=1, high=_MAX_CONFIG_BODY)


@router.post("/strategies")
async def create_strategy(request: Request, user: CurrentUserDep) -> JSONResponse:
    ready = await _ready(request, user)
    if isinstance(ready, JSONResponse):
        return ready
    raw = await _read_body(request)
    if isinstance(raw, JSONResponse):
        return raw
    result = await _call(ready, _create, user.user_id, raw)
    if isinstance(result, JSONResponse):
        return result
    strategy_id = result["strategy_id"]
    return JSONResponse(
        status_code=201,
        content=result,
        headers={"Location": f"/strategies/{strategy_id}"},
    )


@router.get("/strategies")
async def list_strategies(request: Request, user: CurrentUserDep) -> JSONResponse:
    ready = await _ready(request, user)
    if isinstance(ready, JSONResponse):
        return ready
    try:
        limit, offset, include_archived = _list_query(request.query_params)
    except QueryRejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    result = await _call(ready, _list, user.user_id, limit, offset, include_archived)
    if isinstance(result, JSONResponse):
        return result
    return JSONResponse(status_code=200, content=result)


@router.get("/strategies/{strategy_id}")
async def get_strategy(strategy_id: str, request: Request, user: CurrentUserDep) -> JSONResponse:
    ready = await _ready(request, user)
    if isinstance(ready, JSONResponse):
        return ready
    try:
        ident = _strategy_id(strategy_id)
    except QueryRejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    result = await _call(ready, _get, user.user_id, ident)
    if isinstance(result, JSONResponse):
        return result
    return JSONResponse(status_code=200, content=result)


@router.put("/strategies/{strategy_id}")
async def save_version(
    strategy_id: str,
    request: Request,
    user: CurrentUserDep,
) -> JSONResponse:
    ready = await _ready(request, user)
    if isinstance(ready, JSONResponse):
        return ready
    try:
        ident = _strategy_id(strategy_id)
    except QueryRejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    raw = await _read_body(request)
    if isinstance(raw, JSONResponse):
        return raw
    result = await _call(ready, _save, user.user_id, ident, raw)
    if isinstance(result, JSONResponse):
        return result
    return JSONResponse(status_code=200, content=result)


@router.post("/strategies/{strategy_id}/validate")
async def validate_strategy(
    strategy_id: str,
    request: Request,
    user: CurrentUserDep,
) -> JSONResponse:
    """Compile a candidate and write nothing."""
    ready = await _ready(request, user)
    if isinstance(ready, JSONResponse):
        return ready
    try:
        ident = _strategy_id(strategy_id)
    except QueryRejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    raw = await _read_body(request)
    if isinstance(raw, JSONResponse):
        return raw
    result = await _call(ready, _validate, user.user_id, ident, raw)
    if isinstance(result, JSONResponse):
        return result
    return JSONResponse(status_code=200, content=result)


@router.delete("/strategies/{strategy_id}")
async def archive_strategy(
    strategy_id: str,
    request: Request,
    user: CurrentUserDep,
) -> JSONResponse:
    ready = await _ready(request, user)
    if isinstance(ready, JSONResponse):
        return ready
    try:
        ident = _strategy_id(strategy_id)
    except QueryRejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    result = await _call(ready, _archive, user.user_id, ident)
    if isinstance(result, JSONResponse):
        return result
    return JSONResponse(status_code=200, content=result)


@router.get("/strategies/{strategy_id}/versions")
async def list_versions(
    strategy_id: str,
    request: Request,
    user: CurrentUserDep,
) -> JSONResponse:
    ready = await _ready(request, user)
    if isinstance(ready, JSONResponse):
        return ready
    try:
        ident = _strategy_id(strategy_id)
        limit, offset = _page_query(request.query_params)
    except QueryRejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    result = await _call(ready, _versions, user.user_id, ident, limit, offset)
    if isinstance(result, JSONResponse):
        return result
    return JSONResponse(status_code=200, content=result)


@router.get("/strategies/{strategy_id}/versions/{version}")
async def get_version(
    strategy_id: str,
    version: str,
    request: Request,
    user: CurrentUserDep,
) -> JSONResponse:
    ready = await _ready(request, user)
    if isinstance(ready, JSONResponse):
        return ready
    try:
        ident = _strategy_id(strategy_id)
        number = _version_number(version)
    except QueryRejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    result = await _call(ready, _one_version, user.user_id, ident, number)
    if isinstance(result, JSONResponse):
        return result
    return JSONResponse(status_code=200, content=result)


@router.get("/strategies/{strategy_id}/diff")
async def diff_versions(
    strategy_id: str,
    request: Request,
    user: CurrentUserDep,
) -> JSONResponse:
    ready = await _ready(request, user)
    if isinstance(ready, JSONResponse):
        return ready
    try:
        ident = _strategy_id(strategy_id)
        from_version, to_version = _diff_query(request.query_params)
    except QueryRejected as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    result = await _call(ready, _diff, user.user_id, ident, from_version, to_version)
    if isinstance(result, JSONResponse):
        return result
    return JSONResponse(status_code=200, content=result)


def _connect() -> StrategyStore:
    try:
        path = db_path()
        timeout = busy_timeout_ms()
        if path.exists() and not path.is_file():
            raise StoreUnavailable()
        path.parent.mkdir(parents=True, exist_ok=True)
        return StrategyStore(path, busy_timeout_ms=timeout)
    except StoreUnavailable:
        raise
    except (OSError, sqlite3.Error, ValueError):
        raise StoreUnavailable from None


def _env_int(raw: str, *, low: int, high: int) -> int:
    text = raw.strip()
    if not _NUMBER.fullmatch(text) or len(text) > 8 or (len(text) > 1 and text.startswith("0")):
        raise ValueError
    value = int(text)
    if value < low or value > high:
        raise ValueError
    return value


async def _ready(request: Request, user: CurrentUser) -> StrategyStore | JSONResponse:
    if user.user_id != LOCAL_USER_ID:
        return error_response(403, "forbidden", _FORBIDDEN, None)
    try:
        return await run_in_threadpool(open_store, request.app)
    except StoreUnavailable:
        return _unavailable()
    except (sqlite3.Error, OSError):
        return _unavailable()


async def _call(store: StrategyStore, fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return await run_in_threadpool(fn, store, *args)
    except StoreUnavailable:
        return _unavailable()
    except StrategyNotFound:
        return error_response(404, "not_found", _NOT_FOUND, None)
    except VersionNotFound:
        return error_response(404, "not_found", _VERSION_NOT_FOUND, None)
    except VersionConflict as exc:
        return error_response(
            409,
            "version_conflict",
            "strategy version conflict",
            "$.expected_version",
            current_version=exc.version,
            current_ir_hash=exc.ir_hash,
        )
    except Rejected as exc:
        return error_response(422, exc.code, exc.message, exc.path, exc.diagnostics)
    except RecursionError:
        return error_response(422, "expr_too_deep", "expression nested too deeply", None)
    except (sqlite3.Error, OSError):
        return _unavailable()
    except AssertionError:
        raise
    except Exception:
        return error_response(500, "internal", _INTERNAL, None)


def _create(store: StrategyStore, user_id: str, raw: bytes) -> dict[str, Any]:
    compiled = compile_body(raw)
    record, version = store.create(user_id, compiled)
    return _detail(record, version)


def _save(store: StrategyStore, user_id: str, strategy_id: str, raw: bytes) -> dict[str, Any]:
    compiled = compile_body(raw)
    expected_version, expected_ir_hash = _concurrency(raw)
    record, version = store.add_version(
        user_id,
        strategy_id,
        compiled,
        expected_version,
        expected_ir_hash,
    )
    return _detail(record, version)


def _validate(
    store: StrategyStore,
    user_id: str,
    strategy_id: str,
    raw: bytes,
) -> dict[str, Any]:
    """Confirm the owner, then compile. Neither step writes a version."""
    store.get(user_id, strategy_id)
    compiled = compile_body(raw)
    return {
        "valid": True,
        "strategy_id": strategy_id,
        "name": compiled.name,
        "ir_hash": compiled.ir_hash,
    }


def _concurrency(raw: bytes) -> tuple[int | None, str | None]:
    """Return the version guard, or ``(None, None)`` for a legacy append.

    The body has already passed ``compile_body``. A malformed pair is
    rejected here, before ``add_version``, so nothing is written.
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise Rejected("invalid_json", "request body is not JSON", None) from None
    if not isinstance(payload, dict):
        raise Rejected("invalid_json", "request body must be a JSON object", None)
    version_set = "expected_version" in payload and payload["expected_version"] is not None
    hash_set = "expected_ir_hash" in payload and payload["expected_ir_hash"] is not None
    if not version_set and not hash_set:
        return None, None
    if version_set != hash_set:
        raise Rejected(
            "invalid_request",
            "expected_version and expected_ir_hash must be sent together",
            "$",
        )
    version = payload["expected_version"]
    digest = payload["expected_ir_hash"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
        or version > _MAX_VERSION
    ):
        raise Rejected(
            "invalid_request",
            "expected_version must be a whole number from 1 to 1000000",
            "$.expected_version",
        )
    if not isinstance(digest, str) or _HASH.fullmatch(digest) is None:
        raise Rejected(
            "invalid_request",
            "expected_ir_hash must be 64 lowercase hex characters",
            "$.expected_ir_hash",
        )
    return version, digest


def _get(store: StrategyStore, user_id: str, strategy_id: str) -> dict[str, Any]:
    record, version = store.get(user_id, strategy_id)
    return _detail(record, version)


def _list(
    store: StrategyStore,
    user_id: str,
    limit: int,
    offset: int,
    include_archived: bool,
) -> dict[str, Any]:
    page = store.list_for(user_id, limit, offset, include_archived)
    return _page(page)


def _archive(store: StrategyStore, user_id: str, strategy_id: str) -> dict[str, Any]:
    return _metadata(store.archive(user_id, strategy_id))


def _versions(
    store: StrategyStore,
    user_id: str,
    strategy_id: str,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    page = store.list_versions(user_id, strategy_id, limit, offset)
    return _version_page(page)


def _one_version(
    store: StrategyStore,
    user_id: str,
    strategy_id: str,
    version: int,
) -> dict[str, Any]:
    found = store.get_version(user_id, strategy_id, version)
    body = _version_body(found)
    body["strategy_id"] = found.strategy_id
    body["user_id"] = found.user_id
    return body


def _diff(
    store: StrategyStore,
    user_id: str,
    strategy_id: str,
    from_version: int,
    to_version: int,
) -> dict[str, Any]:
    return store.diff(user_id, strategy_id, from_version, to_version)


async def _read_body(request: Request) -> bytes | JSONResponse:
    try:
        limit = body_limit()
    except ValueError:
        return _unavailable()
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
            return _too_large()
        buf.extend(chunk)
    return bytes(buf)


def _declared_too_big(raw: str, limit: int) -> JSONResponse | None:
    if not _NUMBER.fullmatch(raw) or (len(raw) > 1 and raw.startswith("0")):
        return error_response(400, "invalid_request", "content length is invalid", None)
    if len(raw) > 8:
        return _too_large()
    if int(raw) > limit:
        return _too_large()
    return None


def _list_query(params: QueryParams) -> tuple[int, int, bool]:
    limit, offset = _page_query(params)
    raw = _single(params, "include_archived")
    if raw is None or raw == "false":
        return limit, offset, False
    if raw == "true":
        return limit, offset, True
    raise QueryRejected(
        "invalid_query",
        "include_archived must be true or false",
        "$.include_archived",
    )


def _page_query(params: QueryParams) -> tuple[int, int]:
    limit = _bounded(
        _single(params, "limit"),
        default=_DEFAULT_LIMIT,
        low=1,
        high=_MAX_LIMIT,
        path="$.limit",
        message="limit must be a whole number from 1 to 100",
    )
    offset = _bounded(
        _single(params, "offset"),
        default=0,
        low=0,
        high=_MAX_OFFSET,
        path="$.offset",
        message="offset must be a whole number from 0 to 10000",
    )
    return limit, offset


def _diff_query(params: QueryParams) -> tuple[int, int]:
    message = "version must be a whole number from 1 to 1000000"
    from_version = _bounded(
        _single(params, "from"),
        default=None,
        low=1,
        high=_MAX_VERSION,
        path="$.from",
        message=message,
    )
    to_version = _bounded(
        _single(params, "to"),
        default=None,
        low=1,
        high=_MAX_VERSION,
        path="$.to",
        message=message,
    )
    return from_version, to_version


def _single(params: QueryParams, name: str) -> str | None:
    values = params.getlist(name)
    if not values:
        return None
    if len(values) > 1:
        raise QueryRejected("invalid_query", f"{name} must be sent once", f"$.{name}")
    return values[0]


def _bounded(
    raw: str | None,
    *,
    default: int | None,
    low: int,
    high: int,
    path: str,
    message: str,
) -> int:
    if raw is None:
        if default is None:
            raise QueryRejected("invalid_query", message, path)
        return default
    if (
        not _NUMBER.fullmatch(raw)
        or len(raw) > 8
        or (len(raw) > 1 and raw.startswith("0"))
    ):
        raise QueryRejected("invalid_query", message, path)
    value = int(raw)
    if value < low or value > high:
        raise QueryRejected("invalid_query", message, path)
    return value


def _strategy_id(raw: str) -> str:
    if not _STRATEGY_ID.fullmatch(raw):
        raise QueryRejected("invalid_id", "strategy_id is invalid", "$.strategy_id")
    return raw


def _version_number(raw: str) -> int:
    return _bounded(
        raw,
        default=None,
        low=1,
        high=_MAX_VERSION,
        path="$.version",
        message="version must be a whole number from 1 to 1000000",
    )


def _detail(record: StrategyRecord, version: VersionRecord) -> dict[str, Any]:
    body = _metadata(record)
    body["version"] = _version_body(version)
    return body


def _metadata(record: StrategyRecord) -> dict[str, Any]:
    return {
        "strategy_id": record.strategy_id,
        "user_id": record.user_id,
        "name": record.name,
        "ir_strategy_id": record.ir_strategy_id,
        "archived": record.archived_at is not None,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "archived_at": record.archived_at,
        "latest_version": record.latest_version,
        "ir_hash": record.ir_hash,
    }


def _page(page: StrategyPage) -> dict[str, Any]:
    return {
        "strategies": [_metadata(item) for item in page.items],
        "limit": page.limit,
        "offset": page.offset,
        "total": page.total,
    }


def _version_page(page: VersionPage) -> dict[str, Any]:
    return {
        "strategy_id": page.strategy_id,
        "versions": [
            {"version": number, "ir_hash": digest, "created_at": created}
            for number, digest, created in page.items
        ],
        "limit": page.limit,
        "offset": page.offset,
        "total": page.total,
    }


def _version_body(version: VersionRecord) -> dict[str, Any]:
    return {
        "version": version.version,
        "dsl": version.dsl,
        "ir": version.ir,
        "ir_hash": version.ir_hash,
        "created_at": version.created_at,
    }


def _unavailable() -> JSONResponse:
    return error_response(503, "store_unavailable", _UNAVAILABLE, None)


def _too_large() -> JSONResponse:
    return error_response(413, "payload_too_large", _TOO_LARGE, None)
