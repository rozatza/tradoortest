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


def _rewrite_dollar(pattern: str) -> str:
    """Rewrite an unescaped ``$`` to Python ``\\Z`` outside a character class.

    JSON Schema patterns are ECMA-262. There ``$`` matches only the end of
    the text. Python's ``re`` also matches immediately before a final
    newline, so ``"abc\\n"`` would pass ``^[a-z]+$``. The rewritten form is
    used only inside this process. Exported schemas keep ``$`` and never
    contain the Python-only ``\\Z`` anchor.
    """
    parts: list[str] = []
    index = 0
    in_class = False
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            parts.append(pattern[index : index + 2])
            index += 2
            continue
        if char == "[" and not in_class:
            in_class = True
            parts.append(char)
            index += 1
            continue
        if char == "]" and in_class:
            in_class = False
            parts.append(char)
            index += 1
            continue
        if char == "$" and not in_class:
            parts.append(r"\Z")
            index += 1
            continue
        parts.append(char)
        index += 1
    return "".join(parts)


def _ecma_search(pattern: str, text: str) -> bool:
    """Return whether ``text`` matches ``pattern`` under ECMA-262 ``$``."""
    try:
        return re.search(_rewrite_dollar(pattern), text) is not None
    except re.error:
        return False


def _pattern(validator: Draft202012Validator, patrn: str, instance: object, schema: object):
    if validator.is_type(instance, "string") and not _ecma_search(patrn, instance):
        yield ValidationError(f"{instance!r} does not match {patrn!r}")


def _pattern_properties(
    validator: Draft202012Validator,
    pattern_properties: Mapping[str, Any],
    instance: object,
    schema: object,
):
    if not validator.is_type(instance, "object"):
        return
    for pattern, subschema in pattern_properties.items():
        for key, value in instance.items():
            if isinstance(key, str) and _ecma_search(pattern, key):
                yield from validator.descend(value, subschema, path=key, schema_path=pattern)


def _additional_properties(
    validator: Draft202012Validator,
    additional: object,
    instance: object,
    schema: Mapping[str, Any],
):
    """Same rule as Draft 2020-12, with ECMA-262 ``$`` on pattern keys.

    A key such as ``abc\\n`` must stay an additional property. Python's
    ``re`` would treat it as a patternProperties match.
    """
    if not validator.is_type(instance, "object") or not isinstance(instance, dict):
        return
    extras = sorted(_unmatched_keys(instance, schema), key=str)
    if validator.is_type(additional, "object"):
        for key in extras:
            yield from validator.descend(instance[key], additional, path=key)
        return
    if additional or not extras:
        return
    if "patternProperties" in schema:
        verb = "does" if len(extras) == 1 else "do"
        joined = ", ".join(repr(key) for key in extras)
        patterns = ", ".join(repr(key) for key in sorted(schema["patternProperties"]))
        yield ValidationError(f"{joined} {verb} not match any of the regexes: {patterns}")
        return
    verb = "was" if len(extras) == 1 else "were"
    joined = ", ".join(repr(key) for key in extras)
    yield ValidationError(f"Additional properties are not allowed ({joined} {verb} unexpected)")


def _unmatched_keys(instance: Mapping[str, Any], schema: Mapping[str, Any]):
    properties = schema.get("properties", {})
    patterns = schema.get("patternProperties", {})
    for key in instance:
        if not isinstance(key, str) or key in properties:
            if key not in properties:
                yield key
            continue
        if any(_ecma_search(pattern, key) for pattern in patterns):
            continue
        yield key


ToolValidator = extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine_many(
        {"integer": _is_integer, "number": _is_number}
    ),
    validators={
        "pattern": _pattern,
        "patternProperties": _pattern_properties,
        "additionalProperties": _additional_properties,
    },
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
