"""Strict JSON checks that run before a tool handler.

``jsonschema`` rejects a shape. This module rejects what that check would
still allow: a bool used as a number, a non-finite float, a NUL byte, a
string past the length cap, and a document past the IR depth limit.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from jsonschema.validators import extend

from app.ir.depth import JsonShapeError, check_json_value
from app.tools.errors import ToolError, safe_text

MAX_ARGUMENT_BYTES = 1_048_576
MAX_STRING_LENGTH = 100_000
_SCHEMA_ERRORS = 20
_UNEXPECTED = re.compile(r"\('([^']+)' was unexpected\)")


def _is_integer(_checker: object, instance: object) -> bool:
    return isinstance(instance, int) and not isinstance(instance, bool)


def _is_number(_checker: object, instance: object) -> bool:
    if isinstance(instance, bool):
        return False
    if isinstance(instance, int):
        return True
    return isinstance(instance, float) and math.isfinite(instance)


ToolValidator = extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine_many(
        {"integer": _is_integer, "number": _is_number}
    ),
)


def require_object(value: object) -> Mapping[str, Any]:
    """Return a JSON object argument mapping, or raise ``ToolError``."""
    if isinstance(value, bool) or not isinstance(value, dict):
        raise ToolError("validation_error", "arguments must be a JSON object", path="$")
    try:
        check_json_value(value)
    except JsonShapeError as exc:
        raise ToolError(
            exc.code,
            safe_text(exc.message, "arguments are not valid"),
            path=exc.path,
        ) from None
    _bounded(value, "$")
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise ToolError("validation_error", "arguments are not valid JSON", path="$") from None
    if len(encoded) > MAX_ARGUMENT_BYTES:
        raise ToolError("payload_too_large", "arguments are too large", path="$")
    return value


def validate_schema(schema: Mapping[str, Any], value: Mapping[str, Any]) -> None:
    """Raise ``ToolError`` when ``value`` does not match ``schema``."""
    errors = sorted(
        ToolValidator(schema).iter_errors(value),
        key=lambda error: (_error_path(error), error.message),
    )
    if not errors:
        return
    details = [
        {"message": _error_message(error), "path": _error_path(error)}
        for error in errors[:_SCHEMA_ERRORS]
    ]
    first = details[0]
    raise ToolError(
        "validation_error",
        str(first["message"]),
        path=str(first["path"]),
        details=details,
    )


def _bounded(value: object, path: str) -> None:
    if isinstance(value, str):
        if "\x00" in value:
            raise ToolError("invalid_string", "text contains a NUL byte", path=path)
        if len(value) > MAX_STRING_LENGTH:
            raise ToolError("payload_too_large", "a string is too long", path=path)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and "\x00" in key:
                raise ToolError("invalid_string", "text contains a NUL byte", path=path)
            child_path = f"$.{key}" if path == "$" else f"{path}.{key}"
            _bounded(child, child_path)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _bounded(child, f"{path}[{index}]")


def _error_path(error: ValidationError) -> str:
    base = error.json_path or "$"
    if error.validator == "additionalProperties":
        match = _UNEXPECTED.search(error.message)
        if match:
            name = match.group(1)
            if not name or "\x00" in name or len(name) > 80:
                return base
            return f"$.{name}" if base == "$" else f"{base}.{name}"
    return base


def _error_message(error: ValidationError) -> str:
    if error.validator in {"oneOf", "anyOf", "allOf", "not"}:
        return "arguments do not match the tool schema"
    return safe_text(error.message, "arguments do not match the tool schema")
