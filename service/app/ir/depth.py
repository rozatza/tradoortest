"""Iterative limits for JSON values the IR pipeline is about to walk.

``validate_ir`` (through jsonschema), ``canonicalize``, and ``ir_hash`` all
recurse. A few hundred nested objects crash that walk with ``RecursionError``.
The check here uses an explicit stack, so the rejection is a normal error
with a JSONPath. A lone surrogate is rejected in the same walk: it would
otherwise pass the schema and then crash ``ir_hash`` with
``UnicodeEncodeError``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

# A valid signal expression is at most ``MAX_EXPR_DEPTH`` nodes under
# ``$.signals[n].expr``. That lands near depth 85. jsonschema raises
# ``RecursionError`` around 140. 96 sits between those two.
MAX_JSON_DEPTH = 96


class JsonShapeError(ValueError):
    """The document is too deep, or a string is not Unicode text.

    ``code`` is ``expr_too_deep``, ``invalid_string``, or ``non_finite_number``.
    ``path`` is a JSONPath to the value that failed.
    """

    def __init__(self, message: str, *, code: str, path: str) -> None:
        self.message = message
        self.code = code
        self.path = path
        super().__init__(f"{path}: {message} [{code}]")


def check_json_value(value: Any, *, limit: int = MAX_JSON_DEPTH) -> None:
    """Reject a too-deep tree, a non-finite float, or a lone surrogate.

    The walk does not recurse. A bool is an int, not a float, so it is not
    treated as a number here.
    """
    stack: list[tuple[Any, str, int]] = [(value, "$", 0)]
    while stack:
        node, path, depth = stack.pop()
        if depth > limit:
            raise JsonShapeError(
                f"value nested too deeply (limit {limit})",
                code="expr_too_deep",
                path=path,
            )
        if isinstance(node, float):
            if not math.isfinite(node):
                raise JsonShapeError(
                    "number is not finite",
                    code="non_finite_number",
                    path=path,
                )
            continue
        if isinstance(node, str):
            _reject_lone_surrogate(node, path)
            continue
        if isinstance(node, Mapping):
            for key, child in node.items():
                key_text = key if isinstance(key, str) else str(key)
                if isinstance(key, str):
                    _reject_lone_surrogate(key, path)
                stack.append((child, _join(path, key_text), depth + 1))
            continue
        if isinstance(node, list):
            for index, child in enumerate(node):
                stack.append((child, f"{path}[{index}]", depth + 1))


def lone_surrogate_at(text: str) -> int | None:
    """Index of the first lone surrogate, or ``None`` when the text is clean.

    A surrogate pair that Python has already combined into one code point is
    not a lone surrogate. A single ``\\ud800`` is.
    """
    for index, char in enumerate(text):
        if 0xD800 <= ord(char) <= 0xDFFF:
            return index
    return None


def _reject_lone_surrogate(text: str, path: str) -> None:
    index = lone_surrogate_at(text)
    if index is None:
        return
    raise JsonShapeError(
        f"lone surrogate at string index {index} is not valid text",
        code="invalid_string",
        path=path,
    )


def _join(base: str, key: str) -> str:
    if base == "$":
        return f"$.{key}"
    return f"{base}.{key}"
