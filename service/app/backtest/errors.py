"""Backtest failures.

Messages name the problem. They do not include provider data, account
identifiers, or anything that would move money. The engine only simulates.
"""

from __future__ import annotations


class BacktestError(ValueError):
    """The run could not start, or a bar was not usable."""
