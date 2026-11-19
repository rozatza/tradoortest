"""Errors from the indicator library."""

from __future__ import annotations


class IndicatorError(ValueError):
    """An indicator argument is not usable.

    The message names the parameter and the constraint. The call does not
    return a partial series.
    """
