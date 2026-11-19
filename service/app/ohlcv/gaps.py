"""Missing-bar ranges. Gaps stay gaps: nothing in here fills a minute."""

from __future__ import annotations

from dataclasses import dataclass

from app.ohlcv.bars import TIMEFRAME_SECONDS, BarRejection


@dataclass(frozen=True, slots=True)
class GapRange:
    """A half-open run of missing buckets, ``[start, end)`` in unix seconds."""

    start: int
    end: int
    missing: int


@dataclass(frozen=True, slots=True)
class SeriesReport:
    """Gap and rejection counts for one symbol and one timeframe."""

    symbol: str
    mint: str
    timeframe: str
    start: int
    end: int
    expected: int
    present: int
    missing: int
    gaps: tuple[GapRange, ...]
    rejected: tuple[BarRejection, ...]


def missing_ranges(present: set[int], start: int, end: int, step: int) -> list[tuple[int, int]]:
    """Half-open spans of missing grid points inside ``[start, end)``."""
    _check_window(start, end, step)
    ranges: list[tuple[int, int]] = []
    run: int | None = None
    cursor = start
    while cursor < end:
        if cursor not in present:
            if run is None:
                run = cursor
        elif run is not None:
            ranges.append((run, cursor))
            run = None
        cursor += step
    if run is not None:
        ranges.append((run, end))
    return ranges


def gap_ranges(present: set[int], start: int, end: int, step: int) -> tuple[GapRange, ...]:
    """Collapse missing grid points into ranges with counts."""
    return tuple(
        GapRange(lo, hi, (hi - lo) // step) for lo, hi in missing_ranges(present, start, end, step)
    )


def series_report(
    *,
    symbol: str,
    mint: str,
    timeframe: str,
    start: int,
    end: int,
    present: set[int],
    rejected: tuple[BarRejection, ...] = (),
) -> SeriesReport:
    """Count the grid. ``present`` is the set of stored open times."""
    step = TIMEFRAME_SECONDS[timeframe]
    _check_window(start, end, step)
    gaps = gap_ranges(present, start, end, step)
    expected = (end - start) // step
    missing = sum(gap.missing for gap in gaps)
    return SeriesReport(
        symbol=symbol,
        mint=mint,
        timeframe=timeframe,
        start=start,
        end=end,
        expected=expected,
        present=expected - missing,
        missing=missing,
        gaps=gaps,
        rejected=rejected,
    )


def _check_window(start: int, end: int, step: int) -> None:
    if step <= 0 or start < 0 or end < start or (end - start) % step != 0:
        raise ValueError("gap window is not aligned to the timeframe")
    if start % step != 0:
        raise ValueError("gap window is not aligned to the timeframe")
