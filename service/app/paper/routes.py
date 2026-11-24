"""Deploy and undeploy a pinned strategy version on paper.

``POST /strategies/{strategy_id}/versions/{version}/paper`` reads that saved
version and registers it. The request body is not an IR and is ignored.
``DELETE`` on the same path undeploys it. ``GET /paper`` reads the running
registrations, the position rows, one page of the event log, and a
backtest-versus-paper drift report computed on that read. The report is
not stored. The running-paper cap is ``PAPER_MAX_RUNNING`` (default 3).
There is no live deploy and no wallet.

The bar file is the OHLCV runtime's file: ``ohlcv_db_path()``.
"""

from __future__ import annotations

import re
import sqlite3
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.auth import LOCAL_USER_ID, CurrentUserDep
from app.jobs.routes import error_response
from app.ohlcv.routes import OhlcvRuntime, ohlcv_db_path
from app.ohlcv.store import SqliteBarStore
from app.paper.deploy import deploy_version, undeploy_version
from app.paper.drift import (
    PAPER_DRIFT_THRESHOLD_NAME,
    drift_reports,
    format_amount,
    paper_drift_threshold,
)
from app.paper.errors import PaperError
from app.paper.limits import paper_max_running
from app.paper.snapshot import paper_snapshot
from app.strategies.errors import StoreUnavailable, StrategyNotFound, VersionNotFound
from app.strategies.routes import open_store

router = APIRouter()

# Serializes the first open of the shared bar file. A failed open does not
# assign ``app.state.paper_bars``.
_bar_lock = threading.Lock()

_STRATEGY_ID = re.compile(r"^[0-9a-f]{32}$")
_NUMBER = re.compile(r"^[0-9]+$")
_MAX_VERSION = 1_000_000
_VERSION_MESSAGE = "version must be a whole number from 1 to 1000000"
_UNAVAILABLE = "strategy storage is unavailable"
_LIMIT_UNAVAILABLE = "paper strategy limit is not configured"
_FORBIDDEN = "this process serves one user"
_DRIFT_UNAVAILABLE = "paper drift threshold is not configured"
_DEFAULT_EVENT_LIMIT = 100
_MAX_EVENT_LIMIT = 200
_MAX_EVENT_CURSOR = 9_007_199_254_740_991
_LIMIT_MESSAGE = "events_limit must be a whole number from 1 to 200"
_SINCE_MESSAGE = "events_since must be a whole number from 0 to 9007199254740991"


def _bar_store(request: Request) -> SqliteBarStore:
    """The scheduler store, the OHLCV runtime store, or that runtime's path.

    All three are the same ``ohlcv.sqlite``. The fallback opens
    ``ohlcv_db_path()`` once, which is what ``OhlcvRuntime`` opens.
    """
    scheduled = _scheduled_store(request)
    if scheduled is not None:
        return scheduled
    runtime = _runtime_store(request)
    if runtime is not None:
        return runtime
    current = getattr(request.app.state, "paper_bars", None)
    if isinstance(current, SqliteBarStore):
        return current
    with _bar_lock:
        scheduled = _scheduled_store(request)
        if scheduled is not None:
            return scheduled
        runtime = _runtime_store(request)
        if runtime is not None:
            return runtime
        current = getattr(request.app.state, "paper_bars", None)
        if isinstance(current, SqliteBarStore):
            return current
        database = ohlcv_db_path()
        database.parent.mkdir(parents=True, exist_ok=True)
        opened = SqliteBarStore(database)
        request.app.state.paper_bars = opened
        return opened


def _scheduled_store(request: Request) -> SqliteBarStore | None:
    scheduler = getattr(request.app.state, "live_scheduler", None)
    scheduled = getattr(scheduler, "store", None)
    if isinstance(scheduled, SqliteBarStore):
        return scheduled
    return None


def _runtime_store(request: Request) -> SqliteBarStore | None:
    runtime = getattr(request.app.state, "ohlcv", None)
    if isinstance(runtime, OhlcvRuntime):
        return runtime.store
    return None


@router.get("/paper")
async def read_paper(request: Request, user: CurrentUserDep) -> JSONResponse:
    """Running paper strategies, positions, a paged event log, and drift.

    Drift is computed on this read from rows still inside the 24-hour purge.
    It is not stored. ``events_limit`` defaults to 100. Omit ``events_since``
    for the newest page. An ``events_since`` id returns later events only.
    """
    if user.user_id != LOCAL_USER_ID:
        return error_response(403, "forbidden", _FORBIDDEN, None)
    query = _events_query(request)
    if isinstance(query, JSONResponse):
        return query
    events_since, events_limit = query
    try:
        threshold = paper_drift_threshold()
    except ValueError:
        return error_response(503, "service_unavailable", _DRIFT_UNAVAILABLE, None)
    try:
        strategies = await run_in_threadpool(open_store, request.app)
    except StoreUnavailable:
        return error_response(503, "store_unavailable", _UNAVAILABLE, None)
    except (sqlite3.Error, OSError):
        return error_response(503, "store_unavailable", _UNAVAILABLE, None)
    try:
        bars = await run_in_threadpool(_bar_store, request)
    except (OSError, sqlite3.Error, ValueError):
        return error_response(503, "store_unavailable", _UNAVAILABLE, None)
    try:
        body = await run_in_threadpool(
            paper_snapshot,
            bars,
            strategies,
            user.user_id,
            events_since=events_since,
            events_limit=events_limit,
        )
        body["drift"] = await run_in_threadpool(
            drift_reports, bars, user.user_id, threshold
        )
    except (sqlite3.Error, OSError):
        return error_response(503, "store_unavailable", _UNAVAILABLE, None)
    body["drift_threshold"] = format_amount(threshold)
    body["drift_threshold_name"] = PAPER_DRIFT_THRESHOLD_NAME
    return JSONResponse(status_code=200, content=body)


@router.post("/strategies/{strategy_id}/versions/{version}/paper")
async def deploy_paper(
    strategy_id: str,
    version: str,
    request: Request,
    user: CurrentUserDep,
) -> JSONResponse:
    if user.user_id != LOCAL_USER_ID:
        return error_response(403, "forbidden", _FORBIDDEN, None)
    ident = _strategy_id(strategy_id)
    if isinstance(ident, JSONResponse):
        return ident
    number = _version_number(version)
    if isinstance(number, JSONResponse):
        return number
    try:
        limit = paper_max_running()
    except ValueError:
        return error_response(503, "service_unavailable", _LIMIT_UNAVAILABLE, None)
    try:
        strategies = await run_in_threadpool(open_store, request.app)
    except StoreUnavailable:
        return error_response(503, "store_unavailable", _UNAVAILABLE, None)
    except (sqlite3.Error, OSError):
        return error_response(503, "store_unavailable", _UNAVAILABLE, None)
    try:
        bars = await run_in_threadpool(_bar_store, request)
    except (OSError, sqlite3.Error, ValueError):
        return error_response(503, "store_unavailable", _UNAVAILABLE, None)
    try:
        result = await run_in_threadpool(
            deploy_version, strategies, bars, user.user_id, ident, number, limit=limit
        )
    except PaperError as exc:
        return error_response(exc.status, exc.code, exc.message, None)
    except StrategyNotFound:
        return error_response(404, "not_found", "strategy was not found", None)
    except VersionNotFound:
        return error_response(404, "not_found", "strategy version was not found", None)
    except StoreUnavailable:
        return error_response(503, "store_unavailable", _UNAVAILABLE, None)
    except (sqlite3.Error, OSError):
        return error_response(503, "store_unavailable", _UNAVAILABLE, None)
    body = {
        "strategy_id": result.strategy_id,
        "version": result.version,
        "ir_hash": result.ir_hash,
        "registered_at": result.registered_at,
        "status": "running",
    }
    if not result.created:
        return JSONResponse(status_code=200, content=body)
    location = f"/strategies/{result.strategy_id}/versions/{result.version}/paper"
    return JSONResponse(status_code=201, content=body, headers={"Location": location})


@router.delete("/strategies/{strategy_id}/versions/{version}/paper")
async def undeploy_paper(
    strategy_id: str,
    version: str,
    request: Request,
    user: CurrentUserDep,
) -> JSONResponse:
    if user.user_id != LOCAL_USER_ID:
        return error_response(403, "forbidden", _FORBIDDEN, None)
    ident = _strategy_id(strategy_id)
    if isinstance(ident, JSONResponse):
        return ident
    number = _version_number(version)
    if isinstance(number, JSONResponse):
        return number
    try:
        bars = await run_in_threadpool(_bar_store, request)
    except (OSError, sqlite3.Error, ValueError):
        return error_response(503, "store_unavailable", _UNAVAILABLE, None)
    try:
        result = await run_in_threadpool(
            undeploy_version, bars, user.user_id, ident, number
        )
    except PaperError as exc:
        return error_response(exc.status, exc.code, exc.message, None)
    except (sqlite3.Error, OSError):
        return error_response(503, "store_unavailable", _UNAVAILABLE, None)
    return JSONResponse(
        status_code=200,
        content={
            "strategy_id": result.strategy_id,
            "version": result.version,
            "status": "undeployed",
            "orders_canceled": result.orders_canceled,
            "position_state": result.position_state,
        },
    )


class _QueryRejected(Exception):
    def __init__(self, message: str, path: str) -> None:
        self.message = message
        self.path = path


def _events_query(request: Request) -> tuple[int | None, int] | JSONResponse:
    try:
        since = _optional_whole(
            request,
            "events_since",
            low=0,
            high=_MAX_EVENT_CURSOR,
            message=_SINCE_MESSAGE,
        )
        limit = _optional_whole(
            request,
            "events_limit",
            low=1,
            high=_MAX_EVENT_LIMIT,
            message=_LIMIT_MESSAGE,
        )
    except _QueryRejected as exc:
        return error_response(422, "invalid_query", exc.message, exc.path)
    return since, _DEFAULT_EVENT_LIMIT if limit is None else limit


def _optional_whole(
    request: Request, name: str, *, low: int, high: int, message: str
) -> int | None:
    values = request.query_params.getlist(name)
    if not values:
        return None
    if len(values) > 1:
        raise _QueryRejected(f"{name} must be sent once", f"$.{name}")
    raw = values[0]
    if (
        not _NUMBER.fullmatch(raw)
        or len(raw) > 16
        or (len(raw) > 1 and raw.startswith("0"))
    ):
        raise _QueryRejected(message, f"$.{name}")
    value = int(raw)
    if value < low or value > high:
        raise _QueryRejected(message, f"$.{name}")
    return value


def _strategy_id(raw: str) -> str | JSONResponse:
    if not _STRATEGY_ID.fullmatch(raw):
        return error_response(422, "invalid_id", "strategy_id is invalid", "$.strategy_id")
    return raw


def _version_number(raw: str) -> int | JSONResponse:
    if (
        not _NUMBER.fullmatch(raw)
        or len(raw) > 8
        or (len(raw) > 1 and raw.startswith("0"))
    ):
        return error_response(422, "invalid_query", _VERSION_MESSAGE, "$.version")
    value = int(raw)
    if value < 1 or value > _MAX_VERSION:
        return error_response(422, "invalid_query", _VERSION_MESSAGE, "$.version")
    return value
