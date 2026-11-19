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
class LongNoTrade:
    """A quiet run longer than the report threshold. It is not a gap."""

    start: int
    end: int
    minutes: int


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
    no_trade: int
    long_no_trade_minutes: int
    long_no_trade: tuple[LongNoTrade, ...]
    suspect_outage: bool
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


def merged_spans(covered: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlaps and abutments. An unfetched hole stays a hole.

    The result is not clipped to a window, so a span can start before the
    caller's current request.
    """
    merged: list[list[int]] = []
    for lo, hi in sorted(covered):
        if lo >= hi:
            continue
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]


def spans_touching(
    covered: Sequence[tuple[int, int]], start: int, end: int
) -> list[tuple[int, int]]:
    """Merged spans that overlap ``[start, end)``.

    A returned span may start before ``start`` and end after ``end``.
    Abutting spans are one span. A hole between two fetches keeps them apart,
    so a prior close on the far side of that hole is not in this span.
    """
    if end < start:
        raise ValueError("gap window is not aligned to the timeframe")
    return [(lo, hi) for lo, hi in merged_spans(covered) if lo < end and hi > start]


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
    empty: Sequence[tuple[int, int]] = (),
    long_no_trade_minutes: int = 120,
) -> SeriesReport:
    """Count the grid.

    ``gaps`` are provider-missing points only: never fetched, an empty
    provider page, or the leading range before the first real candle in a
    span the provider did answer. A rejected minute is not a gap. A flagged
    1m synthetic bar is not a gap, however long the quiet run is. A missing
    native bucket at 5m or above, after history has started in that span, is
    a no-trade bucket: it counts in ``no_trade`` and is not a gap range.
    ``unfetched`` counts only points no fetched span covers.
    ``coverage_pct`` is real bars divided by the expected grid, as a percent.
    Synthetic bars are present, so a quiet minute is not missing.
    ``long_no_trade`` lists quiet runs strictly longer than
    ``long_no_trade_minutes``. ``suspect_outage`` is true when that list is
    not empty. Those runs stay in the series.
    """
    if isinstance(long_no_trade_minutes, bool) or not isinstance(long_no_trade_minutes, int):
        raise ValueError("long no-trade threshold must be a whole number of minutes")
    if long_no_trade_minutes < 1:
        raise ValueError("long no-trade threshold must be at least 1 minute")
    step = TIMEFRAME_SECONDS[timeframe]
    _check_window(start, end, step)
    expected = (end - start) // step
    populated = [(lo, hi) for lo, hi in fetched if not _covers_span((lo, hi), empty)]
    first_real = _first_real_by_span(populated, real)
    unfetched_times: list[int] = []
    gap_times: list[int] = []
    no_trade_times: list[int] = []
    synthetic_times: list[int] = []
    rejected_times = {
        item.open_time
        for item in rejected
        if isinstance(item.open_time, int) and not isinstance(item.open_time, bool)
    }
    real_count = 0
    synthetic_count = 0
    cursor = start
    while cursor < end:
        if cursor in real:
            real_count += 1
        elif cursor in synthetic:
            synthetic_count += 1
            synthetic_times.append(cursor)
        if not _covers(cursor, fetched):
            unfetched_times.append(cursor)
        kind = _point_kind(
            cursor,
            timeframe=timeframe,
            real=real,
            synthetic=synthetic,
            rejected_times=rejected_times,
            fetched=fetched,
            empty=empty,
            first_real=first_real,
        )
        if kind == "gap":
            gap_times.append(cursor)
        elif kind == "no_trade":
            no_trade_times.append(cursor)
        cursor += step
    gaps = _ranges_from_times(gap_times, step)
    unfetched = _ranges_from_times(unfetched_times, step)
    quiet_times = synthetic_times if timeframe == "1m" else no_trade_times
    long_runs = _long_runs(quiet_times, step, long_no_trade_minutes)
    no_trade = synthetic_count if timeframe == "1m" else len(no_trade_times)
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
        unfetched=sum(item.missing for item in unfetched),
        no_trade=no_trade,
        long_no_trade_minutes=long_no_trade_minutes,
        long_no_trade=long_runs,
        suspect_outage=bool(long_runs),
        gaps=gaps,
        rejected=rejected,
    )


def _point_kind(
    cursor: int,
    *,
    timeframe: str,
    real: set[int],
    synthetic: set[int],
    rejected_times: set[int],
    fetched: Sequence[tuple[int, int]],
    empty: Sequence[tuple[int, int]],
    first_real: dict[tuple[int, int], int | None],
) -> str:
    """``real``, ``synthetic``, ``rejected``, ``gap``, or ``no_trade``."""
    if cursor in real:
        return "real"
    if cursor in synthetic:
        return "synthetic"
    if cursor in rejected_times:
        return "rejected"
    if not _covers(cursor, fetched) or _covers(cursor, empty):
        return "gap"
    span = _span_containing(cursor, fetched, empty)
    started = None if span is None else first_real.get(span)
    if started is None or cursor < started:
        return "gap"
    if timeframe == "1m":
        # A populated 1m span with a prior close stores a synthetic bar.
        # A minute that still has no bar is the leading range, already
        # handled, or a minute the store did not synthesize. Report it as a
        # gap rather than inventing a bucket.
        return "gap"
    return "no_trade"


def _covers_span(span: tuple[int, int], empty: Sequence[tuple[int, int]]) -> bool:
    lo, hi = span
    for elo, ehi in empty:
        if elo == lo and ehi == hi:
            return True
    return False


def _span_containing(
    timestamp: int,
    fetched: Sequence[tuple[int, int]],
    empty: Sequence[tuple[int, int]],
) -> tuple[int, int] | None:
    for lo, hi in fetched:
        if lo <= timestamp < hi and not _covers_span((lo, hi), empty):
            return (lo, hi)
    return None


def _first_real_by_span(
    populated: Sequence[tuple[int, int]], real: set[int]
) -> dict[tuple[int, int], int | None]:
    found: dict[tuple[int, int], int | None] = {}
    for lo, hi in populated:
        times = [stamp for stamp in real if lo <= stamp < hi]
        found[(lo, hi)] = min(times) if times else None
    return found


def _long_runs(times: list[int], step: int, threshold_minutes: int) -> tuple[LongNoTrade, ...]:
    if not times:
        return ()
    runs: list[LongNoTrade] = []
    run = times[0]
    previous = times[0]
    count = 1

    def close(start_at: int, end_at: int, buckets: int) -> None:
        minutes = buckets * step // 60
        if minutes > threshold_minutes:
            runs.append(LongNoTrade(start_at, end_at, minutes))

    for timestamp in times[1:]:
        if timestamp == previous + step:
            previous = timestamp
            count += 1
            continue
        close(run, previous + step, count)
        run = timestamp
        previous = timestamp
        count = 1
    close(run, previous + step, count)
    return tuple(runs)


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
