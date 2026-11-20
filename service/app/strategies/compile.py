"""Validate one strategy request body with the Phase 1 parser.

``parse_strategy`` and ``validate_ir`` are the only checks. Invalid DSL or
IR raises ``Rejected`` and the caller writes nothing.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.dsl.errors import StrategyError
from app.dsl.format import format_strategy
from app.dsl.parser import parse_strategy
from app.expr.errors import ExprError
from app.ir.canonical import canonical_json, canonicalize, ir_hash
from app.ir.depth import JsonShapeError, check_json_value
from app.ir.validate import IRIssue, validate_ir
from app.strategies.errors import Rejected

_MAX_DIAGNOSTICS = 20
_MAX_MESSAGE = 200


@dataclass(frozen=True, slots=True)
class CompiledStrategy:
    """A strategy that passed the parser and ``validate_ir``."""

    dsl: str
    ir: dict[str, Any]
    ir_json: str
    ir_hash: str
    name: str
    ir_strategy_id: str


def compile_body(raw: bytes) -> CompiledStrategy:
    """Parse ``raw`` and return canonical IR plus the DSL to store.

    DSL input is stored as submitted, so a comment or blank line can change
    the text without changing ``ir_hash``. IR input is stored as the DSL
    ``format_strategy`` prints for that canonical document.
    """
    payload = _json_object(raw)
    has_ir = "ir" in payload and payload["ir"] is not None
    has_dsl = "dsl" in payload and payload["dsl"] is not None
    if has_ir == has_dsl:
        raise Rejected("invalid_request", "send exactly one of dsl or ir", "$")
    if has_dsl:
        source = payload["dsl"]
        if not isinstance(source, str):
            raise Rejected("invalid_request", "dsl must be a string", "$.dsl")
        if "\x00" in source:
            raise Rejected("invalid_string", "text contains a NUL byte", "$.dsl")
        ir = _parse_dsl(source)
        dsl = source
    else:
        document = payload["ir"]
        if not isinstance(document, dict):
            raise Rejected("invalid_request", "ir must be a JSON object", "$.ir")
        ir = _canonicalize(document)
        dsl = None
    issues = validate_ir(ir)
    if issues:
        raise _from_issues(issues)
    canonical = canonicalize(ir)
    if dsl is None:
        dsl = _format_dsl(canonical)
    strategy = canonical.get("strategy")
    name = strategy.get("name") if isinstance(strategy, dict) else None
    ident = strategy.get("id") if isinstance(strategy, dict) else None
    if not isinstance(name, str) or not isinstance(ident, str):
        raise Rejected("invalid_strategy", "strategy name or id is missing", "$.strategy")
    text = canonical_json(canonical)
    return CompiledStrategy(
        dsl=dsl,
        ir=canonical,
        ir_json=text,
        ir_hash=ir_hash(canonical),
        name=name,
        ir_strategy_id=ident,
    )


def _json_object(raw: bytes) -> dict[str, Any]:
    if b"\x00" in raw:
        raise Rejected("invalid_json", "request body is not JSON", None)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected("invalid_json", "request body is not JSON", None) from None
    if "\x00" in text:
        raise Rejected("invalid_json", "request body is not JSON", None)
    try:
        payload = json.loads(text, parse_float=_parse_float, parse_constant=_reject_non_finite)
    except RecursionError:
        raise Rejected("expr_too_deep", "expression nested too deeply", None) from None
    except (json.JSONDecodeError, ValueError, UnicodeError):
        raise Rejected("invalid_json", "request body is not JSON", None) from None
    if not isinstance(payload, dict):
        raise Rejected("invalid_json", "request body must be a JSON object", None)
    try:
        check_json_value(payload)
    except JsonShapeError as exc:
        raise Rejected(exc.code, _clip(exc.message), exc.path) from None
    return payload


def _parse_dsl(source: str) -> dict[str, Any]:
    try:
        return parse_strategy(source)
    except RecursionError:
        raise Rejected("expr_too_deep", "expression nested too deeply", None) from None
    except StrategyError as exc:
        raise _from_strategy(exc) from None
    except ExprError as exc:
        raise _from_expr(exc) from None
    except JsonShapeError as exc:
        raise Rejected(exc.code, _clip(exc.message), exc.path) from None
    except (TypeError, ValueError):
        raise Rejected("invalid_request", "strategy could not be read", "$.dsl") from None


def _canonicalize(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return canonicalize(document)
    except RecursionError:
        raise Rejected("expr_too_deep", "expression nested too deeply", None) from None
    except JsonShapeError as exc:
        raise Rejected(exc.code, _clip(exc.message), exc.path) from None
    except (TypeError, ValueError):
        raise Rejected("invalid_request", "strategy could not be read", "$.ir") from None


def _format_dsl(document: Mapping[str, Any]) -> str:
    try:
        return format_strategy(document)
    except RecursionError:
        raise Rejected("expr_too_deep", "expression nested too deeply", None) from None
    except (JsonShapeError, StrategyError, ExprError, TypeError, ValueError):
        raise Rejected("invalid_request", "strategy could not be read", "$.ir") from None


def _from_issues(issues: list[IRIssue]) -> Rejected:
    diagnostics = [
        {"code": issue.keyword, "message": _clip(issue.message), "path": issue.path}
        for issue in issues[:_MAX_DIAGNOSTICS]
    ]
    first = diagnostics[0]
    return Rejected(str(first["code"]), str(first["message"]), _path(first["path"]), diagnostics)


def _from_strategy(exc: StrategyError) -> Rejected:
    diagnostic: dict[str, object] = {
        "code": exc.code or "invalid_strategy",
        "message": _clip(exc.message),
        "path": exc.path,
        "line": exc.line,
        "column": exc.column,
    }
    return Rejected(
        exc.code or "invalid_strategy",
        _clip(exc.message),
        exc.path,
        [diagnostic],
    )


def _from_expr(exc: ExprError) -> Rejected:
    code = exc.code or "invalid_strategy"
    diagnostic: dict[str, object] = {
        "code": code,
        "message": _clip(exc.message),
        "path": exc.path,
        "line": exc.line,
        "column": exc.column,
    }
    return Rejected(code, _clip(exc.message), exc.path, [diagnostic])


def _path(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) > _MAX_MESSAGE:
        return flat[:_MAX_MESSAGE]
    return flat


def _parse_float(text: str) -> float:
    """Reject a magnitude JSON would turn into infinity, such as ``1e400``."""
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("non-finite number")
    return number


def _reject_non_finite(_text: str) -> float:
    """``NaN`` and ``Infinity`` are not JSON numbers for this API."""
    raise ValueError("non-finite number")
