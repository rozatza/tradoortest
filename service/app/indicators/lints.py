"""Per-run indicator lints.

The engine collects these on a context-local list. That list belongs to the
run that set it, including when two runs share a process. Direct callers of
the indicator functions still get ``IndicatorLint`` warnings. Nothing here
installs a process-wide warnings filter.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.indicators.errors import IndicatorLint

_SINK: ContextVar[list[str] | None] = ContextVar("indicator_lint_sink", default=None)


@contextmanager
def collect_lints() -> Iterator[list[str]]:
    """Collect lint texts for the current thread or task.

    The list is empty at entry. Nested collection is a fresh list. Leaving
    the block restores the previous sink, including ``None``.
    """
    notes: list[str] = []
    token = _SINK.set(notes)
    try:
        yield notes
    finally:
        _SINK.reset(token)


def emit_lint(message: str, *, stacklevel: int = 2) -> None:
    """Record ``message`` on the active sink, or warn when there is none."""
    sink = _SINK.get()
    if sink is not None:
        if message not in sink:
            sink.append(message)
        return
    warnings.warn(message, IndicatorLint, stacklevel=stacklevel)
