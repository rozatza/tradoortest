"""Data-quality flags for one any-mint series.

Flags describe the series that was stored. They do not add bars. A short
history is a lint, not a back-fill. A sparse series keeps its gaps. A
zero-volume bar stays in the series. A young pool is reported with its age.
"""

from __future__ import annotations

from app.ohlcv.bars import Bar
from app.ohlcv.pools import ChosenPool

# Below half the grid, and only once the grid is long enough that one
# missing bucket is not the whole story.
SPARSE_MIN_EXPECTED = 4
SPARSE_MAX_REAL_RATIO = 0.5

SHORT_WINDOW_LINT = "history is shorter than the requested window; nothing was back-filled"
SHORT_WARMUP_LINT = "history is shorter than the indicator warmup; nothing was back-filled"
SPARSE_LINT = "series is sparse; missing bars were not filled"
ZERO_VOLUME_LINT = "series contains zero-volume bars"
YOUNG_POOL_LINT = "pool is younger than the requested window"


def annotate_series(
    section: dict[str, object],
    real_bars: list[Bar],
    *,
    pool: ChosenPool | None,
    provider: str,
    warmup_bars: int,
    window_start: int,
    window_end: int,
) -> dict[str, object]:
    """Return ``section`` plus provider, pool, flags, and lints."""
    flags: list[str] = []
    lints: list[str] = []
    if not real_bars or real_bars[0].open_time > window_start:
        flags.append("short_history")
        lints.append(SHORT_WINDOW_LINT)
    if warmup_bars > 0 and len(real_bars) < warmup_bars:
        if "short_history" not in flags:
            flags.append("short_history")
        lints.append(SHORT_WARMUP_LINT)
    expected = section.get("expected")
    real_count = len(real_bars)
    if (
        isinstance(expected, int)
        and not isinstance(expected, bool)
        and expected >= SPARSE_MIN_EXPECTED
        and real_count / expected < SPARSE_MAX_REAL_RATIO
    ):
        flags.append("sparse")
        lints.append(SPARSE_LINT)
    zero_volume = sum(1 for bar in real_bars if bar.volume == 0)
    if zero_volume:
        flags.append("zero_volume")
        lints.append(ZERO_VOLUME_LINT)
    if window_end < window_start:
        raise ValueError("quality window is invalid")
    age: int | None = None
    if pool is not None and pool.pool_created_at is not None:
        if pool.pool_created_at <= pool.resolved_at:
            age = pool.resolved_at - pool.pool_created_at
        younger_than_window = pool.pool_created_at > window_start
        if younger_than_window or (age is not None and age < window_end - window_start):
            flags.append("young_pool")
            lints.append(YOUNG_POOL_LINT)
    annotated = dict(section)
    annotated["provider"] = provider
    annotated["pool"] = None if pool is None else pool.public()
    annotated["flags"] = flags
    annotated["lints"] = lints
    annotated["zero_volume_bars"] = zero_volume
    annotated["pool_age_seconds"] = age
    return annotated
