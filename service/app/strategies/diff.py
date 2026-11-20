"""Deterministic diffs between two saved strategy versions.

DSL output is a unified diff. IR output is a structural diff of the
canonical documents. Both depend on the values, not on ``PYTHONHASHSEED``
or on the order keys were inserted.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

_SEGMENT = re.compile(r"\.([^.\[\]]+)|\[(\d+)\]")


_NO_NEWLINE = "\\ No newline at end of file\n"


def dsl_unified_diff(before: str, after: str, from_version: int, to_version: int) -> str:
    """Unified diff of two DSL texts.

    Identical texts return an empty string. Otherwise the result starts
    with ``--- vN`` and ``+++ vN``. A body line that has no trailing
    newline is ended, then marked ``\\ No newline at end of file``,
    matching ``diff -u``.
    """
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    rendered = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"v{from_version}",
        tofile=f"v{to_version}",
        n=3,
        lineterm="\n",
    )
    parts: list[str] = []
    for line in rendered:
        if line.endswith("\n"):
            parts.append(line)
            continue
        parts.append(line + "\n")
        parts.append(_NO_NEWLINE)
    return "".join(parts)


def ir_structural_diff(before: Any, after: Any) -> list[dict[str, Any]]:
    """Structural diff of two JSON values.

    Each item is ``replace`` (``from`` / ``to``), ``add``, or ``remove``.
    Paths are JSONPaths. The list is sorted by path, with list indexes
    compared as numbers, then by ``op``.
    """
    changes: list[dict[str, Any]] = []
    _walk(before, after, "$", changes)
    changes.sort(key=lambda item: (_path_key(item["path"]), str(item["op"])))
    return changes


def _walk(left: Any, right: Any, path: str, changes: list[dict[str, Any]]) -> None:
    if _same(left, right):
        return
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}"
            if key not in left:
                changes.append({"op": "add", "path": child, "value": right[key]})
            elif key not in right:
                changes.append({"op": "remove", "path": child, "value": left[key]})
            else:
                _walk(left[key], right[key], child, changes)
        return
    if isinstance(left, list) and isinstance(right, list):
        count = max(len(left), len(right))
        for index in range(count):
            child = f"{path}[{index}]"
            if index >= len(left):
                changes.append({"op": "add", "path": child, "value": right[index]})
            elif index >= len(right):
                changes.append({"op": "remove", "path": child, "value": left[index]})
            else:
                _walk(left[index], right[index], child, changes)
        return
    changes.append({"op": "replace", "path": path, "from": left, "to": right})


def _same(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if set(left) != set(right):
            return False
        return all(_same(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same(item, other) for item, other in zip(left, right, strict=True)
        )
    return bool(left == right)


def _path_key(path: str) -> tuple[tuple[int, int | str], ...]:
    parts: list[tuple[int, int | str]] = []
    for match in _SEGMENT.finditer(path):
        name, index = match.group(1), match.group(2)
        if index is not None:
            parts.append((0, int(index)))
        else:
            parts.append((1, name))
    return tuple(parts)
