"""Positioned errors for the expression parser.

`offset` is a 0-based index into the source string (Unicode code points).
`line` and `column` are 1-based. A tab counts as one column.
"""

from __future__ import annotations


class ExprError(Exception):
    """A parse or type error that points at the source.

    `message` is the reason without the location prefix. `str(error)` is
    `line:column: message (offset N)`.
    """

    def __init__(self, message: str, *, offset: int, line: int, column: int) -> None:
        self.message = message
        self.offset = offset
        self.line = line
        self.column = column
        super().__init__(f"{line}:{column}: {message} (offset {offset})")
