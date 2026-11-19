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
    present: int
    missing: int
    unfetched: int
    no_trade: int
    gaps: tuple[GapRange, ...]
    unfetched_gaps: tuple[GapRange, ...]
    no_trade_gaps: tuple[GapRange, ...]
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


def unfetched_spans(
    covered: Sequence[tuple[int, int]], start: int, end: int
) -> list[tuple[int, int]]:
    """Half-open holes inside ``[start, end)`` that no fetched range covers.

    ``covered`` ranges may overlap or abut. They are clipped to the window
    and merged. A minute inside a covered range is not a hole, even when no
    candle was stored for it.
    """
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
    holes: list[tuple[int, int]] = []
    cursor = start
    for lo, hi in merged:
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
    present: set[int],
    rejected: tuple[BarRejection, ...] = (),
    fetched: Sequence[tuple[int, int]] | None = None,
) -> SeriesReport:
    """Count the grid. ``present`` is the set of stored open times.

    ``fetched`` is the set of half-open spans the provider has already
    answered for this series. Pass it for 1m. A minute inside one of those
    spans with no candle and no rejection is ``no_trade``. A minute outside
    them is ``unfetched``. Omit ``fetched`` for a resampled timeframe: its
    ``gaps`` are incomplete buckets, and the unfetched / no-trade split is
    left at zero.
    """
    step = TIMEFRAME_SECONDS[timeframe]
    _check_window(start, end, step)
    gaps = gap_ranges(present, start, end, step)
    expected = (end - start) // step
    missing = sum(gap.missing for gap in gaps)
    if fetched is None:
        unfetched_gaps: tuple[GapRange, ...] = ()
        no_trade_gaps: tuple[GapRange, ...] = ()
        unfetched = 0
        no_trade = 0
    else:
        rejected_times = _rejected_times(rejected, start, end, step)
        unfetched_times: list[int] = []
        no_trade_times: list[int] = []
        cursor = start
        while cursor < end:
            if cursor not in present and cursor not in rejected_times:
                if _covers(cursor, fetched):
                    no_trade_times.append(cursor)
                else:
                    unfetched_times.append(cursor)
            cursor += step
        unfetched_gaps = _ranges_from_times(unfetched_times, step)
        no_trade_gaps = _ranges_from_times(no_trade_times, step)
        unfetched = sum(gap.missing for gap in unfetched_gaps)
        no_trade = sum(gap.missing for gap in no_trade_gaps)
    return SeriesReport(
        symbol=symbol,
        mint=mint,
        timeframe=timeframe,
        start=start,
        end=end,
        expected=expected,
        present=expected - missing,
        missing=missing,
        unfetched=unfetched,
        no_trade=no_trade,
        gaps=gaps,
        unfetched_gaps=unfetched_gaps,
        no_trade_gaps=no_trade_gaps,
        rejected=rejected,
    )


def _rejected_times(
    rejected: Sequence[BarRejection], start: int, end: int, step: int
) -> set[int]:
    times: set[int] = set()
    for item in rejected:
        stamp = item.open_time
        if isinstance(stamp, bool) or not isinstance(stamp, int):
            continue
        if start <= stamp < end and stamp % step == 0:
            times.add(stamp)
    return times


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
