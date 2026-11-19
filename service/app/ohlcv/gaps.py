"""Missing-bar ranges. Gaps stay gaps: nothing in here fills a minute."""

from __future__ import annotations

from collections.abc import Sequence
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
    real: int
    synthetic: int
    present: int
    missing: int
    coverage_pct: float
    unfetched: int
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


def covered_spans(
    covered: Sequence[tuple[int, int]], start: int, end: int
) -> list[tuple[int, int]]:
    """Clip ``covered`` to ``[start, end)`` and merge overlaps and abutments."""
    if end < start:
        raise ValueError("gap window is not aligned to the timeframe")
    merged: list[list[int]] = []
    for lo, hi in sorted(covered):
        lo = max(lo, start)
        hi = min(hi, end)
        if lo >= hi:
            continue
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]


def unfetched_spans(
    covered: Sequence[tuple[int, int]], start: int, end: int
) -> list[tuple[int, int]]:
    """Half-open holes inside ``[start, end)`` that no fetched range covers.

    A minute inside a covered range is not a hole, even when no real candle
    was stored for it.
    """
    holes: list[tuple[int, int]] = []
    cursor = start
    for lo, hi in covered_spans(covered, start, end):
        if cursor < lo:
            holes.append((cursor, lo))
        cursor = hi
    if cursor < end:
        holes.append((cursor, end))
    return holes


def series_report(
    *,
    symbol: str,
    mint: str,
    timeframe: str,
    start: int,
    end: int,
    real: set[int],
    synthetic: set[int],
    rejected: tuple[BarRejection, ...] = (),
    fetched: Sequence[tuple[int, int]] = (),
) -> SeriesReport:
    """Count the grid.

    ``gaps`` are only the grid points the provider has not answered. A minute
    inside a fetched span is not a gap, whether it holds a real bar, a
    flagged synthetic bar, a rejection, or nothing (no prior close yet).
    ``coverage_pct`` is real bars divided by the expected grid, as a percent.
    """
    step = TIMEFRAME_SECONDS[timeframe]
    _check_window(start, end, step)
    expected = (end - start) // step
    unfetched_times: list[int] = []
    real_count = 0
    synthetic_count = 0
    cursor = start
    while cursor < end:
        if cursor in real:
            real_count += 1
        elif cursor in synthetic:
            synthetic_count += 1
        if not _covers(cursor, fetched):
            unfetched_times.append(cursor)
        cursor += step
    gaps = _ranges_from_times(unfetched_times, step)
    present = real_count + synthetic_count
    coverage = 0.0 if expected == 0 else round(100.0 * real_count / expected, 2)
    return SeriesReport(
        symbol=symbol,
        mint=mint,
        timeframe=timeframe,
        start=start,
        end=end,
        expected=expected,
        real=real_count,
        synthetic=synthetic_count,
        present=present,
        missing=expected - present,
        coverage_pct=coverage,
        unfetched=sum(gap.missing for gap in gaps),
        gaps=gaps,
        rejected=rejected,
    )


def _covers(timestamp: int, covered: Sequence[tuple[int, int]]) -> bool:
    for lo, hi in covered:
        if lo <= timestamp < hi:
            return True
    return False


def _ranges_from_times(times: list[int], step: int) -> tuple[GapRange, ...]:
    if not times:
        return ()
    ranges: list[GapRange] = []
    run = times[0]
    previous = times[0]
    for timestamp in times[1:]:
        if timestamp == previous + step:
            previous = timestamp
            continue
        ranges.append(GapRange(run, previous + step, (previous + step - run) // step))
        run = timestamp
        previous = timestamp
    ranges.append(GapRange(run, previous + step, (previous + step - run) // step))
    return tuple(ranges)


def _check_window(start: int, end: int, step: int) -> None:
    if step <= 0 or start < 0 or end < start or (end - start) % step != 0:
        raise ValueError("gap window is not aligned to the timeframe")
    if start % step != 0:
        raise ValueError("gap window is not aligned to the timeframe")
