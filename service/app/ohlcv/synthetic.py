"""Flagged no-trade minutes. These are not silent forward-fill.

Inside a span the provider has already answered, a short run of 1m slots
with no candle becomes flat bars: open, high, low, and close are the
previous real close, volume is 0, and ``synthetic`` is true. The previous
close may sit earlier in the same fetched span, including before the
caller's current window. It does not cross an unfetched hole.

A run longer than ``MAX_SYNTHETIC_RUN`` minutes is not stored. That run is
a gap (an outage), not hours of flat bars. Slots before the first real bar
in the span stay missing. A rejected minute stays rejected.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.ohlcv.bars import Bar
from app.ohlcv.gaps import spans_touching

_MINUTE = 60

# A 1m no-trade run of this many minutes is still a flagged flat bar.
# One minute more is a gap: a 3-hour outage must not become 180 synthetics.
MAX_SYNTHETIC_RUN = 15


def synthetic_minutes(
    *,
    mint: str,
    real: dict[int, tuple[float, int]],
    rejected: set[int],
    covered: Sequence[tuple[int, int]],
    start: int,
    end: int,
) -> list[tuple[Bar, int]]:
    """Return ``(bar, stored_at)`` for short no-trade runs that have a prior close.

    ``real`` maps a minute open time to ``(close, stored_at)`` of a real bar.
    Bars before ``start`` count: they seed the prior close for a window that
    begins in the middle of a span already fetched. ``stored_at`` on the
    synthetic bar is the real bar's, so the flat bar expires with the trade
    it copies. ``rejected`` minutes are left empty and they break a run.
    Only minutes inside ``[start, end)`` are returned. A run is measured
    across the whole fetched span, including minutes outside that window, so
    a long outage is not stored as a short run just because the window
    clipped it.
    """
    built: list[tuple[Bar, int]] = []
    for lo, hi in spans_touching(covered, start, end):
        prior_close: float | None = None
        prior_stored = 0
        cursor = lo + (_MINUTE - lo % _MINUTE) % _MINUTE
        run: list[tuple[int, float, int]] = []
        while cursor < hi:
            trade = real.get(cursor)
            if trade is not None:
                _flush(built, run, mint, start, end)
                run = []
                prior_close, prior_stored = trade
            elif cursor in rejected:
                _flush(built, run, mint, start, end)
                run = []
            elif prior_close is not None:
                run.append((cursor, prior_close, prior_stored))
            else:
                _flush(built, run, mint, start, end)
                run = []
            cursor += _MINUTE
        _flush(built, run, mint, start, end)
    return built


def _flush(
    built: list[tuple[Bar, int]],
    run: list[tuple[int, float, int]],
    mint: str,
    start: int,
    end: int,
) -> None:
    if not run or len(run) > MAX_SYNTHETIC_RUN:
        return
    for open_time, close, stored_at in run:
        if open_time < start or open_time >= end:
            continue
        built.append(
            (
                Bar(
                    mint,
                    "1m",
                    open_time,
                    close,
                    close,
                    close,
                    close,
                    0.0,
                    True,
                ),
                stored_at,
            )
        )
