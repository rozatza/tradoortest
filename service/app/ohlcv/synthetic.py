"""Flagged no-trade minutes. These are not silent forward-fill.

Inside a span the provider has already answered, a 1m slot with no candle
becomes a flat bar: open, high, low, and close are the previous real close,
volume is 0, and ``synthetic`` is true. Slots before the first real bar in
that span stay missing. Nothing is copied in from before the span.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.ohlcv.bars import Bar
from app.ohlcv.gaps import covered_spans

_MINUTE = 60


def synthetic_minutes(
    *,
    mint: str,
    real: dict[int, tuple[float, int]],
    rejected: set[int],
    covered: Sequence[tuple[int, int]],
    start: int,
    end: int,
) -> list[tuple[Bar, int]]:
    """Return ``(bar, stored_at)`` for no-trade minutes that have a prior close.

    ``real`` maps a minute open time to ``(close, stored_at)`` of a real bar.
    ``stored_at`` on the synthetic bar is the real bar's, so the flat bar
    expires with the trade it copies. ``rejected`` minutes are left empty.
    """
    built: list[tuple[Bar, int]] = []
    for lo, hi in covered_spans(covered, start, end):
        prior_close: float | None = None
        prior_stored = 0
        cursor = lo + (_MINUTE - lo % _MINUTE) % _MINUTE
        while cursor < hi:
            trade = real.get(cursor)
            if trade is not None:
                prior_close, prior_stored = trade
            elif prior_close is not None and cursor not in rejected and start <= cursor < end:
                built.append(
                    (
                        Bar(
                            mint,
                            "1m",
                            cursor,
                            prior_close,
                            prior_close,
                            prior_close,
                            prior_close,
                            0.0,
                            True,
                        ),
                        prior_stored,
                    )
                )
            cursor += _MINUTE
    return built
