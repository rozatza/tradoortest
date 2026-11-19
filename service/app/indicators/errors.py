"""Errors from the indicator library."""

from __future__ import annotations


class IndicatorError(ValueError):
    """An indicator argument is not usable, or a result is not a finite number.

    The message names the parameter and the constraint. The call does not
    return a partial series. ``code`` is ``indicator_error``,
    ``indicator_overflow``, or ``indicator_non_finite``.
    """

    def __init__(self, message: str, *, code: str = "indicator_error") -> None:
        self.code = code
        super().__init__(message)


class IndicatorLint(UserWarning):
    """A defined edge this library returns as NaN.

    TA-Lib returns 0 for a fully flat RSI window and for a zero-range
    stochastic. Callers that want those zeros are reading a different rule.
    """
