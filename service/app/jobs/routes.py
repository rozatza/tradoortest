"""HTTP API for one user's backtest jobs.

``POST /backtests`` starts a job. The same ``request_key`` returns that job
again. ``GET /backtests/{job_id}`` is the status. ``GET /backtests/{job_id}/results``
is the persisted result. ``GET /backtests/{job_id}/exports/{name}`` is the
trade log or the ratio file for that result. Concurrency and the per-minute
cap return 429. Validation, a too-large time grid, a too-deep document, and
a failed run return 4xx. None of these routes is a 500 for those failures.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.auth import CurrentUserDep
from app.backtest.engine import MAX_GRID_BARS, grid_bar_count
from app.backtest.errors import BacktestError
from app.dsl.errors import StrategyError
from app.dsl.parser import parse_strategy
from app.expr.errors import ExprError
from app.ir.canonical import canonicalize
from app.ir.depth import JsonShapeError, check_json_value, lone_surrogate_at
from app.ir.validate import validate_ir
from app.jobs.analysis import trade_timing
from app.jobs.chart import BARS_SCHEMA, INDICATORS_SCHEMA
from app.jobs.export import EXPORT_NAMES, ExportError, render_export
from app.jobs.queue import JobExpired, JobQueue, JobRecord, KeyReused, LimitReached, QueueBusy

router = APIRouter()

_DEFAULT_DB = Path("data/backtest-jobs.sqlite")


def error_response(
    status: int,
    code: str,
    message: str,
    path: str | None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "path": path}},
    )


def _queue(request: Request) -> JobQueue:
    jobs = getattr(request.app.state, "jobs", None)
    if jobs is None:
        _DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
        jobs = JobQueue(_DEFAULT_DB)
        request.app.state.jobs = jobs
    return jobs


def _busy() -> JSONResponse:
    return error_response(503, "busy", "the job database is busy", None)


def _expired() -> JSONResponse:
    return error_response(410, "expired", "the backtest result has expired", None)


@router.post("/backtests")
async def start_backtest(request: Request, user: CurrentUserDep) -> JSONResponse:
    try:
        return await _start(request, user.user_id)
    except RecursionError:
        return error_response(422, "expr_too_deep", "expression nested too deeply", None)
    except QueueBusy:
        return _busy()
    except JobExpired:
        return _expired()


@router.get("/backtests/{job_id}")
def backtest_status(job_id: str, request: Request, user: CurrentUserDep) -> JSONResponse:
    try:
        job = _queue(request).get(user.user_id, job_id)
    except QueueBusy:
        return _busy()
    except JobExpired:
        return _expired()
    except KeyError:
        return error_response(404, "not_found", "backtest job was not found", None)
    return JSONResponse(status_code=200, content=_job_body(job))


@router.get("/backtests/{job_id}/results")
def backtest_results(job_id: str, request: Request, user: CurrentUserDep) -> JSONResponse:
    loaded = _load_result(request, user.user_id, job_id)
    if isinstance(loaded, JSONResponse):
        return loaded
    return JSONResponse(status_code=200, content=loaded)


@router.get("/backtests/{job_id}/exports/{name}")
async def backtest_export(
    job_id: str,
    name: str,
    request: Request,
    user: CurrentUserDep,
) -> Response:
    """Serve one stored export. The read and the file render stay off the event loop."""
    if name not in EXPORT_NAMES:
        return error_response(404, "not_found", "export was not found", None)
    rendered = await run_in_threadpool(_render_export, request, user.user_id, job_id, name)
    if isinstance(rendered, JSONResponse):
        return rendered
    body, media = rendered
    return Response(
        content=body,
        media_type=media,
        headers={
            "cache-control": "no-store",
            "content-disposition": f'attachment; filename="{name}"',
        },
    )


@router.get("/backtests/{job_id}/bars")
async def backtest_bars(job_id: str, request: Request, user: CurrentUserDep) -> JSONResponse:
    """Capped candles for the price chart. The read stays off the event loop."""
    loaded = await run_in_threadpool(_bars_document, request, user.user_id, job_id)
    return _chart_response(loaded)


@router.get("/backtests/{job_id}/indicators")
async def backtest_indicators(
    job_id: str,
    request: Request,
    user: CurrentUserDep,
) -> JSONResponse:
    """Capped indicator series from the server-side library. Off the event loop."""
    loaded = await run_in_threadpool(_indicators_document, request, user.user_id, job_id)
    return _chart_response(loaded)


@router.get("/backtests/{job_id}/analysis")
async def backtest_analysis(
    job_id: str,
    request: Request,
    user: CurrentUserDep,
) -> JSONResponse:
    """UTC hour and weekday counts from the stored trade log. Off the event loop."""
    loaded = await run_in_threadpool(_analysis_document, request, user.user_id, job_id)
    return _chart_response(loaded)


def _chart_response(loaded: dict[str, Any] | JSONResponse) -> JSONResponse:
    if isinstance(loaded, JSONResponse):
        return loaded
    return JSONResponse(
        status_code=200,
        content=loaded,
        headers={"cache-control": "no-store"},
    )


def _bars_document(request: Request, user_id: str, job_id: str) -> dict[str, Any] | JSONResponse:
    _refuse_event_loop()
    return _chart_document(request, user_id, job_id, "bars")


def _indicators_document(
    request: Request,
    user_id: str,
    job_id: str,
) -> dict[str, Any] | JSONResponse:
    _refuse_event_loop()
    return _chart_document(request, user_id, job_id, "indicators")


def _analysis_document(
    request: Request,
    user_id: str,
    job_id: str,
) -> dict[str, Any] | JSONResponse:
    """Read the trade log with SQLite ``json_extract``. Do not parse the result row."""
    _refuse_event_loop()
    gated = _gate_job(request, user_id, job_id)
    if isinstance(gated, JSONResponse):
        return gated
    _job, queue = gated
    try:
        kind, text = queue.trades_json(user_id, job_id)
    except QueueBusy:
        return _busy()
    except JobExpired:
        return _expired()
    if kind is None and text is None:
        return error_response(409, "not_ready", "backtest results are not ready", None)
    if kind == "missing" or kind == "null":
        return {
            "schema": "tradoortest.backtest_analysis.v1",
            "available": False,
            "timezone": "UTC",
            "trades_total": 0,
            "trades_used": 0,
            "reason": "trade log unavailable",
        }
    if kind != "array" or text is None:
        return error_response(422, "invalid_result", "backtest result could not be read", None)
    try:
        trades = json.loads(text)
        payload = trade_timing(trades)
    except (json.JSONDecodeError, ValueError):
        return error_response(422, "invalid_result", "backtest result could not be read", None)
    return payload


def _chart_document(
    request: Request,
    user_id: str,
    job_id: str,
    kind: str,
) -> dict[str, Any] | JSONResponse:
    gated = _gate_job(request, user_id, job_id)
    if isinstance(gated, JSONResponse):
        return gated
    _job, queue = gated
    try:
        if kind == "bars":
            text = queue.bars_json(user_id, job_id)
            schema = BARS_SCHEMA
            reason = "bars unavailable"
        else:
            text = queue.indicators_json(user_id, job_id)
            schema = INDICATORS_SCHEMA
            reason = "indicators unavailable"
    except QueueBusy:
        return _busy()
    except JobExpired:
        return _expired()
    if text is None:
        return {"schema": schema, "available": False, "reason": reason}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return error_response(422, "invalid_result", "backtest result could not be read", None)
    if not isinstance(payload, dict):
        return error_response(422, "invalid_result", "backtest result could not be read", None)
    return payload


def _gate_job(
    request: Request,
    user_id: str,
    job_id: str,
) -> tuple[JobRecord, JobQueue] | JSONResponse:
    try:
        queue = _queue(request)
    except QueueBusy:
        return _busy()
    try:
        job = queue.get(user_id, job_id)
    except JobExpired:
        return _expired()
    except KeyError:
        return error_response(404, "not_found", "backtest job was not found", None)
    if job.status in {"queued", "running"}:
        return error_response(409, "not_ready", "backtest results are not ready", None)
    if job.status == "failed":
        error = job.error or {
            "code": "backtest_error",
            "message": "the backtest could not finish",
            "path": None,
        }
        return JSONResponse(status_code=422, content={"error": error})
    return job, queue


def _refuse_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("chart read is on the event loop")


def _render_export(
    request: Request,
    user_id: str,
    job_id: str,
    name: str,
) -> tuple[str, str] | JSONResponse:
    """Load the stored result and render one export. Both steps are synchronous."""
    loaded = _load_result(request, user_id, job_id)
    if isinstance(loaded, JSONResponse):
        return loaded
    try:
        body = render_export(name, loaded)
    except ExportError:
        return error_response(422, "invalid_result", "backtest result could not be exported", None)
    media = "text/csv; charset=utf-8" if name.endswith(".csv") else "application/json"
    return body, media


def _load_result(request: Request, user_id: str, job_id: str) -> dict[str, Any] | JSONResponse:
    """The stored result for this owner, or the same error the results route uses."""
    try:
        queue = _queue(request)
    except QueueBusy:
        return _busy()
    try:
        job = queue.get(user_id, job_id)
    except JobExpired:
        return _expired()
    except KeyError:
        return error_response(404, "not_found", "backtest job was not found", None)
    if job.status in {"queued", "running"}:
        return error_response(409, "not_ready", "backtest results are not ready", None)
    if job.status == "failed":
        error = job.error or {
            "code": "backtest_error",
            "message": "the backtest could not finish",
            "path": None,
        }
        return JSONResponse(status_code=422, content={"error": error})
    try:
        text = queue.result_json(user_id, job_id)
    except JobExpired:
        return _expired()
    if text is None:
        return error_response(409, "not_ready", "backtest results are not ready", None)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return error_response(422, "invalid_result", "backtest result could not be read", None)
    if not isinstance(payload, dict):
        return error_response(422, "invalid_result", "backtest result could not be read", None)
    return payload


async def _start(request: Request, user_id: str) -> JSONResponse:
    try:
        raw = await request.body() or b"null"
        # RFC 8259 is UTF-8. json.loads on bytes also accepts UTF-16.
        text = raw.decode("utf-8-sig")
        payload = json.loads(text, parse_float=_parse_float, parse_constant=_reject_non_finite)
    except RecursionError:
        return error_response(422, "expr_too_deep", "expression nested too deeply", None)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return error_response(422, "invalid_json", "request body is not JSON", None)
    if not isinstance(payload, dict):
        return error_response(422, "invalid_json", "request body must be a JSON object", None)
    try:
        check_json_value(payload)
    except JsonShapeError as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    key = payload.get("request_key")
    if not isinstance(key, str) or key.strip() == "" or len(key) > 200:
        return error_response(
            422,
            "invalid_request_key",
            "request_key must be a non-empty string of at most 200 characters",
            "$.request_key",
        )
    if lone_surrogate_at(key) is not None:
        return error_response(
            422,
            "invalid_string",
            "lone surrogate is not valid text",
            "$.request_key",
        )
    try:
        body = _prepare(payload)
    except JsonShapeError as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    except BacktestError as exc:
        return error_response(422, exc.code, exc.message, exc.path)
    except (StrategyError, ExprError) as exc:
        return error_response(422, exc.code or "invalid_strategy", exc.message, exc.path)
    except TypeError:
        return error_response(422, "invalid_request", "request could not be read", None)
    except ValueError:
        return error_response(422, "invalid_json", "request body is not JSON", None)
    try:
        queue = _queue(request)
    except QueueBusy:
        return _busy()
    try:
        job = queue.start(user_id=user_id, request_key=key, body=body)
    except LimitReached as exc:
        return error_response(429, exc.code, exc.message, None)
    except KeyReused:
        return error_response(
            409,
            "request_key_reused",
            "request_key was already used with a different body",
            "$.request_key",
        )
    except sqlite3.OperationalError:
        return _busy()
    except ValueError:
        return error_response(422, "invalid_json", "request body is not JSON", None)
    status = 202 if job.created else 200
    return JSONResponse(status_code=status, content=_job_body(job))


def _prepare(payload: Mapping[str, Any]) -> dict[str, Any]:
    has_ir = "ir" in payload and payload["ir"] is not None
    has_dsl = "dsl" in payload and payload["dsl"] is not None
    if has_ir == has_dsl:
        raise BacktestError(
            "send exactly one of ir or dsl",
            code="invalid_request",
            path="$",
        )
    if has_dsl:
        source = payload["dsl"]
        if not isinstance(source, str):
            raise BacktestError("dsl must be a string", code="invalid_request", path="$.dsl")
        document = parse_strategy(source)
    else:
        document = canonicalize(payload["ir"])
    issues = validate_ir(document)
    if issues:
        first = issues[0]
        raise BacktestError(first.message, code=first.keyword, path=first.path)
    bars = payload.get("bars")
    if not isinstance(bars, dict) or not bars:
        raise BacktestError(
            "bars must be an object of bar lists",
            code="invalid_bars",
            path="$.bars",
        )
    open_times: list[int] = []
    for asset_id, rows in bars.items():
        if not isinstance(rows, list) or not rows:
            raise BacktestError(
                f"bars for {asset_id} must be a non-empty list",
                code="invalid_bars",
                path=f"$.bars.{asset_id}",
            )
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or "open_time" not in row or "close" not in row:
                raise BacktestError(
                    "each bar needs open_time and close",
                    code="invalid_bars",
                    path=f"$.bars.{asset_id}[{index}]",
                )
            stamp = row["open_time"]
            if isinstance(stamp, bool) or not isinstance(stamp, int):
                raise BacktestError(
                    "bar open_time must be an integer second",
                    code="invalid_bars",
                    path=f"$.bars.{asset_id}[{index}].open_time",
                )
            open_times.append(stamp)
    bar_size = str(document["engine"]["bar_size"])
    slots = grid_bar_count(bar_size, open_times)
    if slots > MAX_GRID_BARS:
        raise BacktestError(
            f"time grid is {slots} bars, above the limit of {MAX_GRID_BARS}",
            code="grid_too_large",
        )
    minutes = payload.get("long_no_trade_minutes", 120)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1:
        raise BacktestError(
            "long_no_trade_minutes must be a whole number of minutes",
            code="invalid_request",
            path="$.long_no_trade_minutes",
        )
    return {
        "ir": document,
        "bars": bars,
        "long_no_trade_minutes": minutes,
    }


def _parse_float(text: str) -> float:
    """Reject a magnitude JSON would turn into infinity, such as ``1e400``."""
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("non-finite number")
    return number


def _reject_non_finite(_text: str) -> float:
    """``NaN`` and ``Infinity`` are not JSON numbers for this API."""
    raise ValueError("non-finite number")


def _job_body(job: JobRecord) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "request_key": job.request_key,
        "user_id": job.user_id,
        "status": job.status,
        "error": job.error,
    }
