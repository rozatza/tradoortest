"""Mint-keyed OHLCV source.

Callers ask for a Solana mint, a timeframe, and a half-open unix window.
They do not name a pool, a CEX symbol, or a vendor. A later source (ROZ-92)
implements this same method and the backfill does not change.
"""

from __future__ import annotations

from collections.abc import Iterator
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


@dataclass(frozen=True, slots=True)
class FetchPage:
    """One committed chunk of a window.

    ``start`` and ``end`` are the half-open span this page actually covered.
    A minute inside that span with no candle was fetched and had no trade.
    It is not a hole to request again. The next page starts at ``end``.

    ``provider_empty`` is true only when the provider returned no candles at
    all. A page that returned older candles is not empty: ``seed`` is the
    latest usable older candle, and it is the prior close for the quiet range.
    A rejected candle and a conflicting timestamp are not that seed.
    ``seed`` sits before ``start``. It is not one of ``bars``.

    A source that never sets ``provider_empty`` cannot report an empty-page
    gap. The flag defaults to false, including the one-page result from a
    source that only implements ``fetch_bars``. An empty response is then an
    ordinary fetched span. A prior close already stored in an abutting span
    is copied across it as synthetic bars, and the report does not call that
    range a gap. A later source has to set the flag when the provider
    returned no candles.
    """

    bars: list[Bar]
    rejected: list[BarRejection]
    start: int
    end: int
    provider_empty: bool = False
    seed: Bar | None = None


class OhlcvSource(Protocol):
    """``fetch_bars(mint, timeframe, start, end)`` -> bars in ``[start, end)``."""

    name: str

    def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult:
        """Return candles for ``mint`` with ``start <= open_time < end``.

        ``start`` and ``end`` are unix seconds. Bars are sorted by open time.
        The source does not forward-fill an empty interval.
        """


def iter_pages(
    source: OhlcvSource, mint: str, timeframe: str, start: int, end: int
) -> Iterator[FetchPage]:
    """Yield pages a caller can commit one at a time.

    A source with ``fetch_pages`` is checkpointed per page. A source that
    only implements ``fetch_bars`` is one page covering ``[start, end)``,
    and that page is yielded only after the call returns.
    """
    fetch_pages = getattr(source, "fetch_pages", None)
    if fetch_pages is not None:
        yield from fetch_pages(mint, timeframe, start, end)
        return
    result = source.fetch_bars(mint, timeframe, start, end)
    # No provider_empty and no seed. This source cannot tell a quiet page
    # from a provider that returned nothing, so it cannot report that gap.
    yield FetchPage(list(result.bars), list(result.rejected), start, end)
