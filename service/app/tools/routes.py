"""HTTP surface for the tool catalog and one tool call.

``GET /tools`` lists descriptors. ``POST /tools/{tool_name}`` runs that tool
for the current user. The dispatcher is synchronous, so it runs in a worker
thread and cannot stall ``GET /health``. A bad body, a huge body, or an
unknown name uses the ``{error}`` envelope. A tool result uses the ``ok``
envelope, including a failed tool call. Spoofed owner headers and query
parameters are ignored.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.auth import LOCAL_USER_ID, CurrentUserDep
from app.jobs.routes import _queue as open_job_queue
from app.jobs.routes import error_response
from app.ohlcv.routes import _runtime as open_ohlcv_runtime
from app.strategies.routes import open_store
from app.tools.dispatch import Dispatcher
from app.tools.errors import http_status
from app.tools.registry import export_specs
from app.tools.runtime import ToolRuntime
from app.tools.strict import MAX_ARGUMENT_BYTES

router = APIRouter()

MAX_BODY_BYTES = MAX_ARGUMENT_BYTES
_NUMBER = re.compile(r"^[0-9]+$")
_TOOL_NAME = re.compile(r"^[a-z_]{1,64}$")
_FORBIDDEN = "this process serves one user"
_TOO_LARGE = "request body is too large"
_INVALID_JSON = "request body is not JSON"
_INVALID_OBJECT = "request body must be a JSON object"


def _runtime(request: Request) -> ToolRuntime:
    return ToolRuntime(
        open_strategies=lambda: open_store(request.app),
        open_jobs=lambda: open_job_queue(request),
        open_ohlcv=lambda: open_ohlcv_runtime(request),
    )


@router.get("/tools")
async def list_tools(user: CurrentUserDep) -> JSONResponse:
    if user.user_id != LOCAL_USER_ID:
        return error_response(403, "forbidden", _FORBIDDEN, None)
    return JSONResponse(status_code=200, content={"tools": export_specs()})


@router.post("/tools/{tool_name}")
async def invoke_tool(tool_name: str, request: Request, user: CurrentUserDep) -> JSONResponse:
    if user.user_id != LOCAL_USER_ID:
        return error_response(403, "forbidden", _FORBIDDEN, None)
    if not _TOOL_NAME.fullmatch(tool_name):
        return error_response(404, "unknown_tool", "unknown tool", "$.tool")
    raw = await _read_body(request)
    if isinstance(raw, JSONResponse):
        return raw
    parsed = _parse(raw)
    if isinstance(parsed, JSONResponse):
        return parsed
    envelope = await run_in_threadpool(
        Dispatcher(_runtime(request)).invoke,
        tool_name,
        parsed,
        user,
    )
    if envelope["ok"]:
        return JSONResponse(status_code=200, content=envelope)
    if envelope["error"]["code"] == "unknown_tool":
        return error_response(404, "unknown_tool", "unknown tool", "$.tool")
    status = http_status(str(envelope["error"]["code"]))
    return JSONResponse(status_code=status, content=envelope)


async def _read_body(request: Request) -> bytes | JSONResponse:
    limit = MAX_BODY_BYTES
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
    if b"\x00" in buf:
        return error_response(422, "invalid_string", "text contains a NUL byte", None)
    return bytes(buf)


def _declared_too_big(raw: str, limit: int) -> JSONResponse | None:
    if not _NUMBER.fullmatch(raw) or (len(raw) > 1 and raw.startswith("0")):
        return error_response(400, "invalid_request", "content length is invalid", None)
    if len(raw) > 8:
        return _too_large()
    if int(raw) > limit:
        return _too_large()
    return None


def _parse(raw: bytes) -> Mapping[str, Any] | JSONResponse:
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


def _parse_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("non-finite number")
    return number


def _reject_non_finite(_text: str) -> float:
    raise ValueError("non-finite number")


def _too_large() -> JSONResponse:
    return error_response(413, "payload_too_large", _TOO_LARGE, None)
