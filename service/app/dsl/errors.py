"""Positioned errors for the strategy DSL."""

from __future__ import annotations


class StrategyError(Exception):
    """A strategy-file error with a source position and a stable code.

    `str(error)` is `line:column: message [code] (offset N)`. `offset` is a
    0-based index into the strategy source. `line` and `column` are 1-based.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        offset: int,
        line: int,
        column: int,
        path: str | None = None,
    ) -> None:
        self.message = message
        self.code = code
        self.offset = offset
        self.line = line
        self.column = column
        self.path = path
        super().__init__(f"{line}:{column}: {message} [{code}] (offset {offset})")
