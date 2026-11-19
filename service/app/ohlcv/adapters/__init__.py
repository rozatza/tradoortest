"""Provider adapters. The backfill talks to ``OhlcvSource``, not to these classes."""

from __future__ import annotations

from app.ohlcv.errors import SourceError
from app.ohlcv.source import OhlcvSource


def open_source(name: str = "geckoterminal") -> OhlcvSource:
    """Build a named source. ``geckoterminal`` is the free on-chain adapter.

    Adding a source is a new branch here plus a class with
    ``fetch_bars(mint, timeframe, start, end)``. Callers of the backfill
    keep passing a mint.
    """
    if name == "geckoterminal":
        from app.ohlcv.adapters.geckoterminal import GeckoTerminalSource

        return GeckoTerminalSource()
    raise SourceError(
        f"unknown OHLCV source {name!r}. "
        "Add an adapter that implements fetch_bars(mint, timeframe, start, end)."
    )
