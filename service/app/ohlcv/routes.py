"""HTTP ingest for any Solana mint.

``POST /ohlcv/jobs`` runs the ingest off the event loop and returns the
result. The same document is ``GET /ohlcv/jobs/{job_id}`` until the
24-hour TTL deletes it. Invalid mints, a missing pool, a bonding curve,
the 180-day cap, and an exhausted rate limit or compute budget are 4xx.
A locked, corrupt, or unwritable store is 503 with a fixed message. The
caller is the local user. A ``user_id`` in the body is ignored.

``OHLCV_GT_MAX_CALLS`` and ``BIRDEYE_CU_BUDGET`` are read when the process
opens its store. The GeckoTerminal cap resets on every ingest. The
compute-unit budget is the monthly ledger. ``OHLCV_MAX_MINTS`` (default 8)
and ``OHLCV_MAX_BODY_BYTES`` (default 16384) bound one request.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.auth import LOCAL_USER_ID, CurrentUserDep
from app.jobs.routes import error_response
from app.ohlcv.adapters.any_mint import DEFAULT_GT_MAX_CALLS, AnyMintSource
from app.ohlcv.adapters.birdeye import DEFAULT_CU_BUDGET
from app.ohlcv.adapters.geckoterminal import PUBLIC_HISTORY_DAYS
from app.ohlcv.backfill import plan_window
from app.ohlcv.bars import TIMEFRAME_SECONDS
from app.ohlcv.errors import (
    HISTORY_LIMIT_MESSAGE,
    STORE_UNAVAILABLE_MESSAGE,
    HistoryLimitError,
    IngestError,
    IngestExpired,
    SourceError,
    StoreError,
    StoreUnavailable,
)
from app.ohlcv.ingest import run_mint_ingest
from app.ohlcv.registry import assets_from_mints
from app.ohlcv.store import SqliteBarStore

router = APIRouter()

_DEFAULT_DB = Path("data/ohlcv.sqlite")
_DEFAULT_MAX_MINTS = 8
_DEFAULT_MAX_BODY_BYTES = 16384
_init_lock = threading.Lock()


class OhlcvRuntime:
    """Store, source, and clock for one process. Tests pass their own."""

    def __init__(self, store: SqliteBarStore, source: Any, now: Callable[[], int]) -> None:
        self.store = store
        self.source = source
        self.now = now


class _TooManyMints(IngestError):
    """One request listed more mints than ``OHLCV_MAX_MINTS``."""

    code = "too_many_mints"
    status = 422


def _runtime(request: Request) -> OhlcvRuntime:
    current = getattr(request.app.state, "ohlcv", None)
    if isinstance(current, OhlcvRuntime):
        return current
    with _init_lock:
        current = getattr(request.app.state, "ohlcv", None)
        if isinstance(current, OhlcvRuntime):
            return current
        max_calls = _positive_env("OHLCV_GT_MAX_CALLS", DEFAULT_GT_MAX_CALLS)
        budget = _positive_env("BIRDEYE_CU_BUDGET", DEFAULT_CU_BUDGET)
        try:
            _DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
            store = SqliteBarStore(_DEFAULT_DB)
            source = AnyMintSource(
                max_calls=max_calls,
                birdeye_budget=budget,
                birdeye_ledger=_DEFAULT_DB.parent / "birdeye-cu.json",
            )
        except ValueError:
            raise
        except (OSError, sqlite3.Error):
            raise StoreUnavailable(STORE_UNAVAILABLE_MESSAGE) from None
        current = OhlcvRuntime(store, source, lambda: int(time.time()))
        request.app.state.ohlcv = current
        return current


@router.post("/ohlcv/jobs")
async def start_ingest(request: Request, user: CurrentUserDep) -> JSONResponse:
    if user.user_id != LOCAL_USER_ID:
        return error_response(403, "forbidden", "this process serves one user", None)
    try:
        body_limit = _positive_env("OHLCV_MAX_BODY_BYTES", _DEFAULT_MAX_BODY_BYTES)
        mint_limit = _positive_env("OHLCV_MAX_MINTS", _DEFAULT_MAX_MINTS)
    except ValueError as exc:
        return _value_error(exc)
    too_big = _declared_body_too_big(request, body_limit)
    if too_big is not None:
        return too_big
    try:
        raw = await request.body() or b"null"
    except (OSError, ValueError):
        return error_response(400, "invalid_request", "request body could not be read", None)
    if len(raw) > body_limit:
        return _too_large()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return error_response(422, "invalid_json", "request body is not JSON", None)
    if not isinstance(payload, dict):
        return error_response(422, "invalid_json", "request body must be a JSON object", None)
    try:
        mints, timeframes, days, warmup = _fields(payload, mint_limit)
        assets = assets_from_mints(mints)
    except HistoryLimitError as exc:
        return error_response(exc.status, exc.code, str(exc), "$.days")
    except IngestError as exc:
        return error_response(exc.status, exc.code, str(exc), "$.mints")
    except ValueError as exc:
        return _value_error(exc)
    try:
        runtime = _runtime(request)
    except StoreUnavailable:
        return _unavailable()
    except ValueError as exc:
        return _value_error(exc)
    except (sqlite3.Error, OSError):
        return _unavailable()

    def _run() -> dict[str, object]:
        start, end = plan_window(runtime.now(), days, timeframes)
        return run_mint_ingest(
            source=runtime.source,
            store=runtime.store,
            assets=assets,
            start=start,
            end=end,
            timeframes=timeframes,
            user_id=user.user_id,
            generated_at=runtime.now(),
            warmup_bars=warmup,
        )

    try:
        report = await run_in_threadpool(_run)
    except HistoryLimitError as exc:
        return error_response(exc.status, exc.code, str(exc), "$.days")
    except IngestError as exc:
        return error_response(exc.status, exc.code, str(exc), "$.mints")
    except IngestExpired:
        return error_response(410, "expired", "the ingest result has expired", None)
    except StoreUnavailable:
        return _unavailable()
    except (sqlite3.Error, OSError):
        return _unavailable()
    except SourceError as exc:
        return _source_error(exc)
    except (StoreError, ValueError) as exc:
        return _value_error(exc)
    except AssertionError:
        raise
    except Exception:
        return error_response(422, "ingest_failed", "ingest failed", None)
    return JSONResponse(status_code=200, content=report)


@router.get("/ohlcv/jobs/{job_id}")
def ingest_result(job_id: str, request: Request, user: CurrentUserDep) -> JSONResponse:
    if user.user_id != LOCAL_USER_ID:
        return error_response(403, "forbidden", "this process serves one user", None)
    try:
        payload = _runtime(request).store.read_ingest_result(user_id=user.user_id, job_id=job_id)
    except IngestExpired:
        return error_response(410, "expired", "the ingest result has expired", None)
    except StoreUnavailable:
        return _unavailable()
    except ValueError as exc:
        return _value_error(exc)
    except (sqlite3.Error, OSError):
        return _unavailable()
    if payload is None:
        return error_response(404, "not_found", "ingest job was not found", None)
    return JSONResponse(status_code=200, content=payload)


def _fields(
    payload: dict[str, Any], max_mints: int
) -> tuple[list[str], tuple[str, ...], int, int]:
    mints = payload.get("mints")
    if not isinstance(mints, list) or not mints or any(not isinstance(item, str) for item in mints):
        raise ValueError("mints must be a non-empty list of mint addresses")
    if len(mints) > max_mints:
        raise _TooManyMints("too many mints in one request")
    cleaned = [item.strip() for item in mints if item.strip()]
    if not cleaned:
        raise ValueError("mints must be a non-empty list of mint addresses")
    raw_timeframes = payload.get("timeframes", ["1m"])
    if (
        not isinstance(raw_timeframes, list)
        or not raw_timeframes
        or any(not isinstance(item, str) or item.strip() == "" for item in raw_timeframes)
    ):
        raise ValueError("timeframes must be a non-empty list")
    timeframes = tuple(item.strip() for item in raw_timeframes)
    if any(name not in TIMEFRAME_SECONDS for name in timeframes):
        raise ValueError("unsupported timeframe")
    days = payload.get("days", 1)
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("days must be a whole number, at least 1")
    if days > PUBLIC_HISTORY_DAYS:
        raise HistoryLimitError(HISTORY_LIMIT_MESSAGE)
    warmup = payload.get("warmup_bars", 0)
    if isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 0:
        raise ValueError("warmup bars must be a whole number, at least 0")
    return cleaned, timeframes, days, warmup


def _positive_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive whole number") from None
    if value < 1:
        raise ValueError(f"{name} must be a positive whole number")
    return value


def _declared_body_too_big(request: Request, limit: int) -> JSONResponse | None:
    declared = request.headers.get("content-length")
    if declared is None:
        return None
    try:
        size = int(declared)
    except ValueError:
        return error_response(400, "invalid_request", "content length is invalid", None)
    if size > limit:
        return _too_large()
    return None


def _too_large() -> JSONResponse:
    return error_response(413, "payload_too_large", "request body is too large", None)


def _unavailable() -> JSONResponse:
    return error_response(503, "store_unavailable", STORE_UNAVAILABLE_MESSAGE, None)


def _source_error(exc: SourceError) -> JSONResponse:
    return error_response(422, "source_error", _one_line(exc), None)


def _value_error(exc: BaseException) -> JSONResponse:
    return error_response(422, "invalid_request", _one_line(exc), None)


def _one_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return text[0] if text else exc.__class__.__name__
