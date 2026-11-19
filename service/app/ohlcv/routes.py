"""HTTP ingest for any Solana mint.

``POST /ohlcv/jobs`` runs the ingest and returns the result. The same
document is ``GET /ohlcv/jobs/{job_id}`` until the 24-hour TTL deletes it.
Invalid mints, a missing pool, a bonding curve, the 180-day cap, and an
exhausted rate limit or compute budget are 4xx. The caller is the local
user. A ``user_id`` in the body is ignored.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.auth import LOCAL_USER_ID, CurrentUserDep
from app.jobs.routes import error_response
from app.ohlcv.adapters.any_mint import AnyMintSource
from app.ohlcv.backfill import plan_window
from app.ohlcv.errors import IngestError, IngestExpired, SourceError, StoreError
from app.ohlcv.ingest import run_mint_ingest
from app.ohlcv.registry import assets_from_mints
from app.ohlcv.store import SqliteBarStore

router = APIRouter()

_DEFAULT_DB = Path("data/ohlcv.sqlite")


class OhlcvRuntime:
    """Store, source, and clock for one process. Tests pass their own."""

    def __init__(self, store: SqliteBarStore, source: Any, now: Callable[[], int]) -> None:
        self.store = store
        self.source = source
        self.now = now


def _runtime(request: Request) -> OhlcvRuntime:
    current = getattr(request.app.state, "ohlcv", None)
    if current is None:
        _DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
        current = OhlcvRuntime(
            SqliteBarStore(_DEFAULT_DB),
            AnyMintSource(),
            lambda: int(time.time()),
        )
        request.app.state.ohlcv = current
    return current


@router.post("/ohlcv/jobs")
async def start_ingest(request: Request, user: CurrentUserDep) -> JSONResponse:
    if user.user_id != LOCAL_USER_ID:
        return error_response(403, "forbidden", "this process serves one user", None)
    try:
        raw = await request.body() or b"null"
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return error_response(422, "invalid_json", "request body is not JSON", None)
    if not isinstance(payload, dict):
        return error_response(422, "invalid_json", "request body must be a JSON object", None)
    try:
        mints, timeframes, days, warmup = _fields(payload)
        assets = assets_from_mints(mints)
    except IngestError as exc:
        return error_response(exc.status, exc.code, str(exc), "$.mints")
    except ValueError as exc:
        return _value_error(exc)
    runtime = _runtime(request)
    try:
        start, end = plan_window(runtime.now(), days, timeframes)
        report = run_mint_ingest(
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
    except IngestError as exc:
        return error_response(exc.status, exc.code, str(exc), "$.mints")
    except SourceError as exc:
        return _source_error(exc)
    except (StoreError, ValueError, OSError) as exc:
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
    if payload is None:
        return error_response(404, "not_found", "ingest job was not found", None)
    return JSONResponse(status_code=200, content=payload)


def _fields(payload: dict[str, Any]) -> tuple[list[str], tuple[str, ...], int, int]:
    mints = payload.get("mints")
    if not isinstance(mints, list) or not mints or any(not isinstance(item, str) for item in mints):
        raise ValueError("mints must be a non-empty list of mint addresses")
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
    days = payload.get("days", 1)
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("days must be a whole number, at least 1")
    warmup = payload.get("warmup_bars", 0)
    if isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 0:
        raise ValueError("warmup bars must be a whole number, at least 0")
    return cleaned, tuple(item.strip() for item in raw_timeframes), days, warmup


def _source_error(exc: SourceError) -> JSONResponse:
    text = _one_line(exc)
    if "180" in text:
        return error_response(400, "history_limit", text, "$.days")
    return error_response(422, "source_error", text, None)


def _value_error(exc: BaseException) -> JSONResponse:
    text = _one_line(exc)
    if "180" in text:
        return error_response(400, "history_limit", text, "$.days")
    return error_response(422, "invalid_request", text, None)


def _one_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return text[0] if text else exc.__class__.__name__
