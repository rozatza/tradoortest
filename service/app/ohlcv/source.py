"""Mint-keyed OHLCV source.

Callers ask for a Solana mint, a timeframe, and a half-open unix window.
They do not name a pool, a CEX symbol, or a vendor. A later source (ROZ-92)
implements this same method and the backfill does not change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.ohlcv.bars import Bar, BarRejection


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Parsed bars plus candles that could not be parsed.

    ``bars`` may still fail OHLC checks. Ingest runs those checks, flags the
    failures, and does not store them. Missing minutes are simply absent.
    """

    bars: list[Bar]
    rejected: list[BarRejection]


class OhlcvSource(Protocol):
    """``fetch_bars(mint, timeframe, start, end)`` -> bars in ``[start, end)``."""

    name: str

    def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult:
        """Return candles for ``mint`` with ``start <= open_time < end``.

        ``start`` and ``end`` are unix seconds. Bars are sorted by open time.
        The source does not forward-fill an empty interval.
        """
