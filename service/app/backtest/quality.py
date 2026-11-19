"""Quiet runs the metrics and the trade log have to show.

A 1m quiet bar is a flagged synthetic minute. On any other bar size a quiet
bar is a hole after that asset's first real bar. The engine has no fetched
span, so it cannot tell an unfetched gap from a native no-trade bucket, and
a hole before the first real bar is not a long run. A 1m hole is not quiet.
It breaks a synthetic run. Synthetic bars are quiet on every bar size.

A run is long when its minutes are strictly greater than the threshold
(default 120, the same cut as the OHLCV report). Those bars are the
suspect-outage set. The series is a suspect outage when the set is not empty.

Indicators are not masked. A derived series is linted when a finite output
depends on a suspect-outage sample: a rolling window that contains one, or a
seeded run (EMA, RSI, ATR, MACD) that contains one and still produces a
finite value. The stale close stays in the input.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AssetFlags:
    """One asset, aligned to the run grid. True means that slot counts."""

    missing: tuple[bool, ...]
    synthetic: tuple[bool, ...]
    outage: tuple[bool, ...]


@dataclass(frozen=True, slots=True)
class QualityCounts:
    """Asset-slot totals. Minutes are ``bars * bar_seconds / 60``."""

    grid_bars: int
    missing_bars: int
    synthetic_bars: int
    long_no_trade_bars: int
    long_no_trade_minutes: int
    long_no_trade_threshold: int
    missing_bars_in_position: int
    synthetic_bars_in_position: int
    long_no_trade_bars_in_position: int
    long_no_trade_minutes_in_position: int

    @property
    def suspect_outage(self) -> bool:
        return self.long_no_trade_bars > 0


def asset_flags(
    present: Sequence[bool],
    synthetic: Sequence[bool],
    *,
    step: int,
    threshold_minutes: int,
    one_minute: bool,
) -> AssetFlags:
    """Mark missing, synthetic, and long-quiet slots for one asset."""
    missing: list[bool] = []
    synth: list[bool] = []
    quiet: list[int] = []
    seen_real = False
    for index, (is_present, is_synthetic) in enumerate(zip(present, synthetic, strict=True)):
        if not is_present:
            missing.append(True)
            synth.append(False)
            # 1m holes are gaps. Higher timeframes count holes after the
            # first real bar as no-trade buckets.
            if not one_minute and seen_real:
                quiet.append(index)
            continue
        missing.append(False)
        synth.append(is_synthetic)
        if is_synthetic:
            quiet.append(index)
        else:
            seen_real = True
    return AssetFlags(
        missing=tuple(missing),
        synthetic=tuple(synth),
        outage=tuple(_long_mask(quiet, step, threshold_minutes, len(present))),
    )


def outage_lints(
    specs: Sequence[Mapping[str, Any]],
    series: Mapping[str, Mapping[str, Sequence[float]]],
    outages: Mapping[str, Sequence[bool]],
) -> tuple[str, ...]:
    """Lint a finite indicator output that reads a suspect-outage sample.

    The output is left as computed. The lint names the series and the asset
    and says how many suspect-outage bars fed a finite value.
    """
    notes: list[str] = []
    for spec in specs:
        series_id = str(spec["id"])
        for asset_id, columns in series.items():
            output = columns.get(series_id)
            outage = outages.get(asset_id)
            if output is None or outage is None or not any(outage):
                continue
            source_name = "close" if spec["indicator"] in {"atr", "stochastic"} else spec["source"]
            source = columns.get(source_name, ())
            count = _tainted(spec, source, output, outage)
            if count:
                notes.append(
                    f"{series_id} on {asset_id} is computed across "
                    f"{count} suspect_outage bars"
                )
    return tuple(notes)


def _long_mask(indices: Sequence[int], step: int, threshold: int, size: int) -> list[bool]:
    mask = [False] * size
    if not indices:
        return mask
    start = 0

    def close(lo: int, hi: int) -> None:
        count = hi - lo
        minutes = count * step // 60
        if minutes > threshold:
            for cursor in range(lo, hi):
                mask[indices[cursor]] = True

    for cursor in range(1, len(indices)):
        if indices[cursor] != indices[cursor - 1] + 1:
            close(start, cursor)
            start = cursor
    close(start, len(indices))
    return mask


def _tainted(
    spec: Mapping[str, Any],
    source: Sequence[float],
    output: Sequence[float],
    outage: Sequence[bool],
) -> int:
    name = spec["indicator"]
    hit: set[int] = set()
    if name in {"ema", "rsi", "atr", "macd"}:
        start: int | None = None
        for index, value in enumerate(source):
            if _finite(value) and start is None:
                start = index
            elif not _finite(value) and start is not None:
                _seeded(start, index, output, outage, hit)
                start = None
        if start is not None:
            _seeded(start, len(source), output, outage, hit)
        return len(hit)
    period = _lookback(spec)
    for index, value in enumerate(output):
        if not _finite(value):
            continue
        lo = 0 if index + 1 < period else index - period + 1
        for cursor in range(lo, index + 1):
            if cursor < len(outage) and outage[cursor]:
                hit.add(cursor)
    return len(hit)


def _seeded(
    start: int,
    end: int,
    output: Sequence[float],
    outage: Sequence[bool],
    hit: set[int],
) -> None:
    if not any(_finite(output[index]) for index in range(start, min(end, len(output)))):
        return
    for index in range(start, end):
        if index < len(outage) and outage[index]:
            hit.add(index)


def _lookback(spec: Mapping[str, Any]) -> int:
    params = spec["params"]
    name = spec["indicator"]
    if name == "stochastic":
        return int(params["k_period"]) + int(params["d_period"]) - 1
    return int(params["period"])


def _finite(value: object) -> bool:
    return isinstance(value, float) and math.isfinite(value)
