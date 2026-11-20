"""Backtest tools wrap ``JobQueue`` and ``app.jobs.routes._prepare``.

Bars stay inside the job body until the worker finishes, then the existing
24-hour result row is the only copy. These tools do not open a second store.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.auth import CurrentUser
from app.jobs.queue import JobExpired, JobRecord
from app.jobs.routes import _prepare
from app.strategies.store import StrategyStore
from app.tools.errors import ToolError
from app.tools.runtime import ToolRuntime

_NOT_READY = {"queued", "running"}
_FAILED_ERROR = {
    "code": "backtest_error",
    "message": "the backtest could not finish",
    "path": None,
}


def backtest_start(
    arguments: Mapping[str, Any],
    user: CurrentUser,
    runtime: ToolRuntime,
) -> dict[str, Any]:
    store = runtime.strategies() if "strategy_id" in arguments else None
    payload = _payload(arguments, user, store)
    key = arguments.get("request_key")
    if not isinstance(key, str):
        key = uuid.uuid4().hex
    queue = runtime.jobs()
    job = queue.start(user_id=user.user_id, request_key=key, body=payload)
    created_at, expires_at = _times(job, queue.result_ttl_seconds)
    return {
        "job_id": job.job_id,
        "request_key": job.request_key,
        "status": job.status,
        "created": job.created,
        "created_at": created_at,
        "expires_at": expires_at,
    }


def backtest_status(
    arguments: Mapping[str, Any],
    user: CurrentUser,
    runtime: ToolRuntime,
) -> dict[str, Any]:
    queue = runtime.jobs()
    job = _job(queue, user.user_id, str(arguments["job_id"]))
    return _status(job, queue.result_ttl_seconds)


def backtest_get(
    arguments: Mapping[str, Any],
    user: CurrentUser,
    runtime: ToolRuntime,
) -> dict[str, Any]:
    queue = runtime.jobs()
    job = _job(queue, user.user_id, str(arguments["job_id"]))
    body = _status(job, queue.result_ttl_seconds)
    if job.status in _NOT_READY:
        body["ready"] = False
        body["result"] = None
        return body
    if job.status == "failed":
        body["ready"] = False
        body["error"] = job.error or dict(_FAILED_ERROR)
        body["result"] = None
        return body
    text = queue.result_json(user.user_id, job.job_id)
    if text is None:
        body["ready"] = False
        body["result"] = None
        return body
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise ToolError(
            "service_unavailable",
            "the job database is unavailable",
            retryable=True,
        ) from None
    if not isinstance(result, dict):
        raise ToolError("service_unavailable", "the job database is unavailable", retryable=True)
    body["ready"] = True
    body["error"] = None
    body["result"] = result
    return body


def _payload(
    arguments: Mapping[str, Any],
    user: CurrentUser,
    store: StrategyStore | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"bars": arguments["bars"]}
    if "long_no_trade_minutes" in arguments:
        payload["long_no_trade_minutes"] = arguments["long_no_trade_minutes"]
    if store is None:
        if "dsl" in arguments:
            payload["dsl"] = arguments["dsl"]
        else:
            payload["ir"] = arguments["ir"]
        return _prepare(payload)
    record = store.find(user.user_id, str(arguments["strategy_id"]))
    if record is None:
        raise ToolError("not_found", "strategy was not found")
    if record.archived_at is not None:
        raise ToolError("strategy_archived", "strategy is archived")
    number = arguments.get("version", record.latest_version)
    loaded = store.get_version(user.user_id, record.strategy_id, int(number))
    payload["ir"] = loaded.ir
    return _prepare(payload)


def _job(queue: Any, user_id: str, job_id: str) -> JobRecord:
    try:
        return queue.get(user_id, job_id)
    except KeyError:
        raise ToolError("not_found", "backtest job was not found") from None
    except JobExpired:
        raise ToolError("expired", "the backtest result has expired") from None


def _status(job: JobRecord, ttl: int) -> dict[str, Any]:
    created_at, expires_at = _times(job, ttl)
    return {
        "job_id": job.job_id,
        "status": job.status,
        "created_at": created_at,
        "started_at": None,
        "finished_at": None,
        "expires_at": expires_at,
        "error": job.error,
    }


def _times(job: JobRecord, ttl: int) -> tuple[str, str]:
    if job.created_at is None:
        raise ToolError("service_unavailable", "the job database is unavailable", retryable=True)
    return _iso(job.created_at), _iso(job.created_at + ttl)


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
