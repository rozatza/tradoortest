"""Deploy and undeploy a pinned strategy version on paper.

``POST /strategies/{strategy_id}/versions/{version}/paper`` reads that saved
version and registers it. The request body is not an IR and is ignored.
``DELETE`` on the same path undeploys it. The running-paper cap is
``PAPER_MAX_RUNNING`` (default 3). There is no live deploy and no wallet.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.auth import LOCAL_USER_ID, CurrentUserDep
from app.jobs.routes import error_response
from app.ohlcv.routes import OhlcvRuntime
from app.ohlcv.store import SqliteBarStore
from app.paper.deploy import deploy_version, undeploy_version
from app.paper.errors import PaperError
from app.paper.limits import paper_max_running
from app.strategies.errors import StoreUnavailable, StrategyNotFound, VersionNotFound
from app.strategies.routes import open_store

router = APIRouter()

_STRATEGY_ID = re.compile(r"^[0-9a-f]{32}$")
_NUMBER = re.compile(r"^[0-9]+$")
_MAX_VERSION = 1_000_000
_VERSION_MESSAGE = "version must be a whole number from 1 to 1000000"
_UNAVAILABLE = "strategy storage is unavailable"
_LIMIT_UNAVAILABLE = "paper strategy limit is not configured"
_FORBIDDEN = "this process serves one user"


def _bar_store(request: Request) -> SqliteBarStore:
    """The store the scheduler ticks, or the OHLCV runtime, or the default file."""
    scheduler = getattr(request.app.state, "live_scheduler", None)
    scheduled = getattr(scheduler, "store", None)
    if isinstance(scheduled, SqliteBarStore):
        return scheduled
    runtime = getattr(request.app.state, "ohlcv", None)
    if isinstance(runtime, OhlcvRuntime):
        return runtime.store
    current = getattr(request.app.state, "paper_bars", None)
    if isinstance(current, SqliteBarStore):
        return current
    directory = Path(os.environ.get("OHLCV_DATA_DIR") or "data")
    directory.mkdir(parents=True, exist_ok=True)
    opened = SqliteBarStore(directory / "ohlcv.sqlite")
    request.app.state.paper_bars = opened
    return opened


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
