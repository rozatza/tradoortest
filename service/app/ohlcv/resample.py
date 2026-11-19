"""Resample 1m bars into a higher timeframe.

Buckets are aligned to the unix epoch in UTC. A 5m bar starts when
``open_time % 300 == 0``, an hour when ``% 3600 == 0``, a day at UTC
midnight (``% 86400 == 0``).

A higher-timeframe bar is emitted only when every constituent 1m bar is
present. A missing minute leaves a gap. Input order does not matter.
Duplicate minutes suppress that bucket. Nothing is forward-filled.

Aggregation, in time order:

- open is the first minute's open
- high is the max high
- low is the min low
- close is the last minute's close
- volume is the sum of the minute volumes
"""

from __future__ import annotations

from collections.abc import Sequence

from app.ohlcv.bars import TIMEFRAME_SECONDS, Bar


def resample(bars: Sequence[Bar], timeframe: str) -> list[Bar]:
    """Return deterministic ``timeframe`` bars built only from complete buckets."""
    step = TIMEFRAME_SECONDS.get(timeframe)
    if step is None:
        raise ValueError(f"timeframe {timeframe!r} is not supported")
    if any(bar.timeframe != "1m" for bar in bars):
        raise ValueError("resample reads 1m bars")
    if not bars:
        return []
    mint = bars[0].mint
    if any(bar.mint != mint for bar in bars):
        raise ValueError("resample reads one mint at a time")

    ordered = sorted(bars, key=lambda bar: bar.open_time)
    if timeframe == "1m":
        return ordered

    buckets: dict[int, list[Bar]] = {}
    for bar in ordered:
        if bar.open_time % 60 != 0:
            raise ValueError("resample reads 1m bars aligned to the minute")
        bucket = bar.open_time - (bar.open_time % step)
        buckets.setdefault(bucket, []).append(bar)

    need = step // 60
    out: list[Bar] = []
    for bucket in sorted(buckets):
        group = buckets[bucket]
        times = [bar.open_time for bar in group]
        if len(times) != len(set(times)):
            continue
        expected = list(range(bucket, bucket + step, 60))
        if times != expected or len(group) != need:
            continue
        out.append(
            Bar(
                mint=mint,
                timeframe=timeframe,
                open_time=bucket,
                open=group[0].open,
                high=max(bar.high for bar in group),
                low=min(bar.low for bar in group),
                close=group[-1].close,
                volume=sum(bar.volume for bar in group),
            )
        )
    return out
