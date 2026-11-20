"""Run one tool call and always return a JSON envelope.

Domain exceptions from the wrapped services are mapped here. An unexpected
exception becomes ``internal_error`` with a fixed sentence.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.auth import CurrentUser
from app.backtest.errors import BacktestError
from app.dsl.errors import StrategyError
from app.expr.errors import ExprError
from app.indicators.errors import IndicatorError
from app.ir.depth import JsonShapeError
from app.jobs.queue import JobExpired, KeyReused, LimitReached, QueueBusy
from app.ohlcv.errors import (
    InvalidMint,
    NoDexPoolYet,
    NoPool,
    RateLimitExhausted,
    SourceError,
    StoreError,
)
from app.ohlcv.errors import StoreUnavailable as OhlcvStoreUnavailable
from app.strategies.errors import (
    Rejected,
    StoreUnavailable,
    StrategyNotFound,
    VersionConflict,
    VersionNotFound,
)
from app.tools.errors import ToolError, error_envelope, safe_text, success_envelope
from app.tools.registry import BY_NAME
from app.tools.runtime import ToolRuntime
from app.tools.strict import require_object, validate_schema

_NAME_OK = set("abcdefghijklmnopqrstuvwxyz_")


class Dispatcher:
    """Validate, call, and validate again. ``user`` is the only owner."""

    def __init__(self, runtime: ToolRuntime) -> None:
        self.runtime = runtime

    def invoke(self, name: str, arguments: object, user: CurrentUser) -> dict[str, Any]:
        tool = _public_name(name)
        spec = BY_NAME.get(name) if name == tool else None
        if spec is None:
            return error_envelope(
                tool,
                "unknown_tool",
                "unknown tool",
                path="$.tool",
                retryable=False,
                details=None,
            )
        try:
            payload = require_object(arguments)
            validate_schema(spec.input_schema, payload)
            data = spec.handler(payload, user, self.runtime)
            checked = _json_ready(data)
            try:
                validate_schema(spec.output_schema, checked)
            except ToolError:
                raise ToolError("internal_error", "the tool could not finish") from None
        except ToolError as exc:
            return error_envelope(
                tool,
                exc.code,
                safe_text(exc.message, "the tool could not finish"),
                path=exc.path,
                retryable=exc.retryable,
                details=_details(exc.details),
            )
        except Exception as exc:
            mapped = _map(exc)
            return error_envelope(
                tool,
                mapped.code,
                mapped.message,
                path=mapped.path,
                retryable=mapped.retryable,
                details=_details(mapped.details),
            )
        return success_envelope(tool, checked)


def _public_name(name: object) -> str:
    if not isinstance(name, str) or not 1 <= len(name) <= 64:
        return "unknown"
    if any(char not in _NAME_OK for char in name):
        return "unknown"
    return name


def _json_ready(data: object) -> dict[str, Any]:
    try:
        encoded = json.dumps(data, allow_nan=False)
        loaded = json.loads(encoded)
    except (TypeError, ValueError):
        raise ToolError("internal_error", "the tool could not finish") from None
    if not isinstance(loaded, dict):
        raise ToolError("internal_error", "the tool could not finish")
    return loaded


def _details(value: object) -> dict[str, Any] | list[Any] | None:
    if value is None:
        return None
    try:
        loaded = json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError):
        return None
    if isinstance(loaded, dict | list):
        return loaded
    return None


def _map(exc: BaseException) -> ToolError:
    if isinstance(exc, AssertionError):
        raise exc
    if isinstance(exc, Rejected):
        return ToolError(
            exc.code or "validation_error",
            safe_text(exc.message, "strategy is not valid"),
            path=exc.path,
            details=exc.diagnostics,
        )
    if isinstance(exc, VersionConflict):
        return ToolError(
            "version_conflict",
            "strategy version conflict",
            path="$.expected_version",
            details={"current_version": exc.version, "current_ir_hash": exc.ir_hash},
        )
    if isinstance(exc, (StrategyNotFound, VersionNotFound)):
        message = (
            "strategy version was not found"
            if isinstance(exc, VersionNotFound)
            else "strategy was not found"
        )
        return ToolError("not_found", message)
    if isinstance(exc, (StoreUnavailable, OhlcvStoreUnavailable, StoreError, QueueBusy)):
        return ToolError("service_unavailable", "the service is unavailable", retryable=True)
    if isinstance(exc, JobExpired):
        return ToolError("expired", "the backtest result has expired")
    if isinstance(exc, LimitReached):
        if exc.code == "rate_limit":
            return ToolError("rate_limited", "too many backtests were started", retryable=True)
        return ToolError("limit_reached", "too many backtests are running", retryable=True)
    if isinstance(exc, KeyReused):
        return ToolError(
            "conflict",
            "request_key was already used with a different body",
            path="$.request_key",
        )
    if isinstance(exc, InvalidMint):
        return ToolError("invalid_mint", "invalid mint", path="$.query")
    if isinstance(exc, NoPool):
        return ToolError("no_pool", safe_text(exc, "no pool"), path="$.query")
    if isinstance(exc, NoDexPoolYet):
        return ToolError("no_dex_pool_yet", "no DEX pool yet", path="$.query")
    if isinstance(exc, RateLimitExhausted):
        return ToolError("rate_limited", "the asset source is rate limited", retryable=True)
    if isinstance(exc, SourceError):
        return ToolError("service_unavailable", "the asset source is unavailable", retryable=True)
    if isinstance(exc, JsonShapeError):
        return ToolError(exc.code, safe_text(exc.message, "arguments are not valid"), path=exc.path)
    if isinstance(exc, (BacktestError, StrategyError, ExprError)):
        code = getattr(exc, "code", None) or "validation_error"
        message = getattr(exc, "message", None) or str(exc)
        path = getattr(exc, "path", None)
        return ToolError(
            str(code),
            safe_text(message, "the request is not valid"),
            path=path if isinstance(path, str) or path is None else None,
        )
    if isinstance(exc, IndicatorError):
        return ToolError("validation_error", safe_text(exc, "indicator is not valid"))
    if isinstance(exc, RecursionError):
        return ToolError("expr_too_deep", "expression nested too deeply")
    if isinstance(exc, (sqlite3.Error, OSError)):
        return ToolError("service_unavailable", "the service is unavailable", retryable=True)
    if isinstance(exc, (TypeError, ValueError, UnicodeError)):
        return ToolError("validation_error", "arguments are not valid")
    return ToolError("internal_error", "the tool could not finish")
