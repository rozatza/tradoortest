"""Resumable backfill of 1m bars, then higher timeframes resampled locally.

Running twice stores the same rows. The second run asks the source only
for minutes that are still missing. A minute the source does not return
stays missing and shows up in the gap report. Rejected bars are listed
there too and are not repaired.

Higher timeframes are rebuilt from the stored 1m bars for the window, so
they do not add HTTP calls. A bucket that is short a minute is omitted.
``replace_window`` drops a previously stored higher-timeframe candle that
the current 1m set no longer supports.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.auth import LOCAL_USER_ID
from app.ohlcv.adapters import open_source
from app.ohlcv.bars import TIMEFRAME_SECONDS, Bar, BarRejection, validate_bar
from app.ohlcv.errors import SourceError, StoreError
from app.ohlcv.gaps import SeriesReport, missing_ranges, series_report
from app.ohlcv.registry import MAJORS, Asset, require_assets
from app.ohlcv.resample import resample
from app.ohlcv.source import OhlcvSource
from app.ohlcv.store import BarStore, SqliteBarStore

_DEFAULT_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")


def plan_window(now: int, days: int, timeframes: Sequence[str]) -> tuple[int, int]:
    """Half-open UTC window covering at least ``days`` of the coarsest bar.

    ``end`` is ``now`` floored to that bar. ``start`` is ``days`` earlier,
    then floored again, so the grid is aligned. Flooring can only add bars.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    if not timeframes:
        raise ValueError("at least one timeframe is required")
    unknown = [item for item in timeframes if item not in TIMEFRAME_SECONDS]
    if unknown:
        raise ValueError(f"unsupported timeframe {unknown[0]!r}")
    step = max(TIMEFRAME_SECONDS[item] for item in timeframes)
    if now < step:
        raise ValueError("now is earlier than one bar")
    end = now - (now % step)
    start = end - days * 86400
    start -= start % step
    if start < 0:
        start = 0
    return start, end


def run_backfill(
    *,
    source: OhlcvSource,
    store: BarStore,
    assets: Sequence[Asset],
    start: int,
    end: int,
    timeframes: Sequence[str],
    user_id: str,
    generated_at: int,
) -> dict[str, object]:
    """Fetch missing 1m bars, resample, and return the gap report document."""
    ordered = _timeframes(timeframes)
    sections: list[SeriesReport] = []
    for asset in assets:
        rejected = _fill_minutes(
            source=source,
            store=store,
            asset=asset,
            start=start,
            end=end,
            user_id=user_id,
        )
        present = store.open_times(
            user_id=user_id,
            mint=asset.mint,
            timeframe="1m",
            start=start,
            end=end,
        )
        sections.append(
            series_report(
                symbol=asset.symbol,
                mint=asset.mint,
                timeframe="1m",
                start=start,
                end=end,
                present=present,
                rejected=tuple(rejected),
            )
        )
        if any(timeframe != "1m" for timeframe in ordered):
            minutes = store.load(
                user_id=user_id,
                mint=asset.mint,
                timeframe="1m",
                start=start,
                end=end,
            )
            for timeframe in ordered:
                if timeframe == "1m":
                    continue
                derived = resample(minutes, timeframe)
                store.replace_window(
                    derived,
                    user_id=user_id,
                    mint=asset.mint,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                )
                stored = store.open_times(
                    user_id=user_id,
                    mint=asset.mint,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                )
                sections.append(
                    series_report(
                        symbol=asset.symbol,
                        mint=asset.mint,
                        timeframe=timeframe,
                        start=start,
                        end=end,
                        present=stored,
                    )
                )
    return _document(
        user_id=user_id,
        provider=source.name,
        generated_at=generated_at,
        sections=sections,
    )


def _fill_minutes(
    *,
    source: OhlcvSource,
    store: BarStore,
    asset: Asset,
    start: int,
    end: int,
    user_id: str,
) -> list[BarRejection]:
    present = store.open_times(
        user_id=user_id,
        mint=asset.mint,
        timeframe="1m",
        start=start,
        end=end,
    )
    rejected: list[BarRejection] = []
    for lo, hi in missing_ranges(present, start, end, TIMEFRAME_SECONDS["1m"]):
        fetched = source.fetch_bars(asset.mint, "1m", lo, hi)
        rejected.extend(fetched.rejected)
        good: list[Bar] = []
        for bar in fetched.bars:
            if bar.mint != asset.mint or bar.timeframe != "1m":
                rejected.append(
                    BarRejection(
                        asset.mint,
                        "1m",
                        bar.open_time,
                        ("candle does not match the requested mint and timeframe",),
                    )
                )
                continue
            if bar.open_time < lo or bar.open_time >= hi:
                continue
            reasons = validate_bar(bar)
            if reasons:
                rejected.append(
                    BarRejection(bar.mint, bar.timeframe, bar.open_time, tuple(reasons))
                )
                continue
            good.append(bar)
        store.upsert(good, user_id=user_id)
    return rejected


def _timeframes(timeframes: Sequence[str]) -> tuple[str, ...]:
    """1m is always stored. Higher timeframes are reported after it."""
    if not timeframes:
        raise ValueError("at least one timeframe is required")
    unknown = [item for item in timeframes if item not in TIMEFRAME_SECONDS]
    if unknown:
        raise ValueError(f"unsupported timeframe {unknown[0]!r}")
    return tuple(["1m", *[item for item in timeframes if item != "1m"]])


def _document(
    *,
    user_id: str,
    provider: str,
    generated_at: int,
    sections: Sequence[SeriesReport],
) -> dict[str, object]:
    return {
        "user_id": user_id,
        "provider": provider,
        "generated_at": _iso(generated_at),
        "forward_filled": False,
        "series": [_section(section) for section in sections],
    }


def _section(section: SeriesReport) -> dict[str, object]:
    return {
        "symbol": section.symbol,
        "mint": section.mint,
        "timeframe": section.timeframe,
        "start": _iso(section.start),
        "end": _iso(section.end),
        "expected": section.expected,
        "present": section.present,
        "missing": section.missing,
        "gaps": [
            {"start": _iso(gap.start), "end": _iso(gap.end), "missing": gap.missing}
            for gap in section.gaps
        ],
        "rejected": [
            {
                "open_time": None if item.open_time is None else _iso(item.open_time),
                "reasons": list(item.reasons),
            }
            for item in section.rejected
        ],
    }


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ohlcv.backfill",
        description="Backfill 1m OHLCV for SOL and majors, then resample locally.",
    )
    parser.add_argument("--days", type=int, default=90, help="how many days to cover (default 90)")
    parser.add_argument(
        "--symbols",
        default=",".join(asset.symbol for asset in MAJORS),
        help="comma-separated symbols (default: the five majors)",
    )
    parser.add_argument(
        "--timeframes",
        default=",".join(_DEFAULT_TIMEFRAMES),
        help="comma-separated timeframes (1m is always stored; higher bars are resampled)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="directory for ohlcv.sqlite (default: OHLCV_DATA_DIR or ./data)",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="also write the gap report JSON to this path (keep it under data/)",
    )
    parser.add_argument("--user-id", default=LOCAL_USER_ID, help="owner stored on each row")
    parser.add_argument("--provider", default="geckoterminal", help="OHLCV source name")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    source: OhlcvSource | None = None,
    now: int | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("days must be at least 1")
    try:
        assets = require_assets([part for part in args.symbols.split(",") if part.strip()])
        timeframes = tuple(part.strip() for part in args.timeframes.split(",") if part.strip())
        clock = int(time.time()) if now is None else now
        start, end = plan_window(clock, args.days, timeframes)
        data_dir = Path(args.data_dir or os.environ.get("OHLCV_DATA_DIR") or "data")
        store = SqliteBarStore(data_dir / "ohlcv.sqlite")
        owns_source = source is None
        active = open_source(args.provider) if source is None else source
        try:
            report = run_backfill(
                source=active,
                store=store,
                assets=assets,
                start=start,
                end=end,
                timeframes=timeframes,
                user_id=args.user_id,
                generated_at=clock,
            )
        finally:
            store.close()
            if owns_source:
                close = getattr(active, "close", None)
                if close is not None:
                    close()
    except (SourceError, StoreError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    text = json.dumps(report, indent=2) + "\n"
    if args.report is not None:
        destination = Path(args.report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
