"""Backtest failures.

Messages name the problem. They do not include provider data, account
identifiers, or anything that would move money. The engine only simulates.
"""

from __future__ import annotations


class BacktestError(ValueError):
    """The run could not start, or a bar was not usable.

    ``code`` is a stable token for the job API. ``path`` is a JSONPath when
    the failure points at one place in the document.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "backtest_error",
        path: str | None = None,
    ) -> None:
        self.message = message
        self.code = code
        self.path = path
        super().__init__(message)
