"""Resumable fetch of native OHLCV candles into a TTL cache.

Each successful page is committed before the next one is requested. The
store records the half-open span that page covered, with the same TTL as
the rows. A later run, while the cache is fresh, requests only spans that
were never fetched. After the TTL, those spans are gone and are fetched
again.

1m minutes inside a fetched span with no candle become flagged synthetic
flat bars once a real close exists in that span, at any length. A long quiet
run stays synthetic and is flagged when it is longer than the threshold
(default 120 minutes). It is not a gap. A page is empty only when the
provider returned no candles at all. Older candles on a quiet page are the
prior close, and the quiet range stays synthetic. Minutes before the first
close in the span are a leading gap when history starts inside the window
or a short page does not reach back past the window start. An empty page is
a gap, and a prior close does not cross it. The prior close is seeded from
the fetched span even when that close sits before the current window, and
it does not cross an unfetched hole. Higher timeframes are native
GeckoTerminal aggregates. They are not built from 1m bars. A missing native
bucket after history has started in the merged span is a no-trade count,
not a gap range. Rejected candles stay rejected and are not repaired.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.auth import LOCAL_USER_ID
from app.ohlcv.adapters import open_source
from app.ohlcv.adapters.geckoterminal import PUBLIC_HISTORY_DAYS, PUBLIC_HISTORY_MESSAGE
from app.ohlcv.bars import TIMEFRAME_SECONDS, Bar, BarRejection, validate_bar
from app.ohlcv.errors import SourceError, StoreError
from app.ohlcv.gaps import GapRange, LongNoTrade, SeriesReport, series_report, unfetched_spans
from app.ohlcv.registry import MAJORS, Asset, require_assets
from app.ohlcv.source import FetchPage, OhlcvSource, iter_pages
from app.ohlcv.store import MAX_CACHE_TTL_SECONDS, BarStore, SqliteBarStore

_DEFAULT_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")
_TIMEFRAME_ORDER = _DEFAULT_TIMEFRAMES


def plan_window(now: int, days: int, timeframes: Sequence[str]) -> tuple[int, int]:
    """Half-open UTC window covering at least ``days`` of the coarsest bar.

    ``end`` is ``now`` floored to that bar. ``start`` is ``days`` earlier,
    then floored again, so the grid is aligned. Flooring can only add bars.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    if days > PUBLIC_HISTORY_DAYS:
        raise ValueError(PUBLIC_HISTORY_MESSAGE)
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
    progress: Callable[[str], None] | None = None,
    long_no_trade_minutes: int = 120,
) -> dict[str, object]:
    """Fetch uncached native candles and return the gap report document."""
    ordered = _timeframes(timeframes)
    sections: list[SeriesReport] = []
    for asset in assets:
        for timeframe in ordered:
            rejected = _fill_timeframe(
                source=source,
                store=store,
                asset=asset,
                timeframe=timeframe,
                start=start,
                end=end,
                user_id=user_id,
                progress=progress,
            )
            fetched = store.fetched_ranges(
                user_id=user_id,
                mint=asset.mint,
                timeframe=timeframe,
                start=start,
                end=end,
            )
            empty = store.empty_ranges(
                user_id=user_id,
                mint=asset.mint,
                timeframe=timeframe,
                start=start,
                end=end,
            )
            span_lo, span_hi = _coverage_bounds(fetched, start, end)
            covered_bars = store.load(
                user_id=user_id,
                mint=asset.mint,
                timeframe=timeframe,
                start=span_lo,
                end=span_hi,
            )
            sections.append(
                series_report(
                    symbol=asset.symbol,
                    mint=asset.mint,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                    real={bar.open_time for bar in covered_bars if not bar.synthetic},
                    synthetic={bar.open_time for bar in covered_bars if bar.synthetic},
                    rejected=tuple(rejected),
                    fetched=fetched,
                    empty=empty,
                    long_no_trade_minutes=long_no_trade_minutes,
                )
            )
    return _document(
        user_id=user_id,
        provider=source.name,
        generated_at=generated_at,
        sections=sections,
    )


def _fill_timeframe(
    *,
    source: OhlcvSource,
    store: BarStore,
    asset: Asset,
    timeframe: str,
    start: int,
    end: int,
    user_id: str,
    progress: Callable[[str], None] | None,
) -> list[BarRejection]:
    rejected = store.rejections(
        user_id=user_id,
        mint=asset.mint,
        timeframe=timeframe,
        start=start,
        end=end,
    )
    seen = {(item.open_time, item.reasons) for item in rejected}
    covered = store.fetched_ranges(
        user_id=user_id,
        mint=asset.mint,
        timeframe=timeframe,
        start=start,
        end=end,
    )
    for lo, hi in unfetched_spans(covered, start, end):
        _say(progress, f"ohlcv: {asset.symbol} {timeframe} fetching {_iso(lo)} .. {_iso(hi)}")
        page_number = 0
        for page in iter_pages(source, asset.mint, timeframe, lo, hi):
            good, page_rejected = _split_page(page, asset, timeframe, lo, hi)
            store.commit_page(
                good,
                page_rejected,
                user_id=user_id,
                mint=asset.mint,
                timeframe=timeframe,
                start=page.start,
                end=page.end,
                window_start=start,
                window_end=end,
                provider_empty=page.provider_empty,
                seed=_seed_bar(page, asset, timeframe),
            )
            page_number += 1
            _say(
                progress,
                f"ohlcv: {asset.symbol} {timeframe} committed page {page_number} "
                f"through {_iso(page.end)}",
            )
            for item in page_rejected:
                key = (item.open_time, item.reasons)
                if key not in seen:
                    rejected.append(item)
                    seen.add(key)
    return rejected


def _say(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _seed_bar(page: FetchPage, asset: Asset, timeframe: str) -> Bar | None:
    """The latest older candle, when it is a real bar this series can store."""
    if page.provider_empty:
        return None
    seed = page.seed
    if seed is None:
        return None
    if seed.mint != asset.mint or seed.timeframe != timeframe or seed.synthetic:
        return None
    if isinstance(seed.open_time, bool) or not isinstance(seed.open_time, int):
        return None
    if seed.open_time >= page.start:
        return None
    if validate_bar(seed):
        return None
    return seed


def _split_page(
    page: FetchPage,
    asset: Asset,
    timeframe: str,
    request_start: int,
    request_end: int,
) -> tuple[list[Bar], list[BarRejection]]:
    if page.end < page.start or page.start < request_start or page.end > request_end:
        raise SourceError("fetched page is outside the requested span")
    if page.start == page.end:
        return [], []
    rejected = list(page.rejected)
    good: list[Bar] = []
    for bar in page.bars:
        if bar.mint != asset.mint or bar.timeframe != timeframe:
            rejected.append(
                BarRejection(
                    asset.mint,
                    timeframe,
                    bar.open_time,
                    ("candle does not match the requested mint and timeframe",),
                )
            )
            continue
        if bar.open_time < page.start or bar.open_time >= page.end:
            continue
        reasons = validate_bar(bar)
        if reasons:
            rejected.append(BarRejection(bar.mint, bar.timeframe, bar.open_time, tuple(reasons)))
            continue
        good.append(bar)
    return good, rejected


def _timeframes(timeframes: Sequence[str]) -> tuple[str, ...]:
    """Requested timeframes in coarse-grid order. Each one is fetched natively."""
    if not timeframes:
        raise ValueError("at least one timeframe is required")
    unknown = [item for item in timeframes if item not in TIMEFRAME_SECONDS]
    if unknown:
        raise ValueError(f"unsupported timeframe {unknown[0]!r}")
    requested = set(timeframes)
    return tuple(item for item in _TIMEFRAME_ORDER if item in requested)


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
        "attribution": "Data by GeckoTerminal / CoinGecko",
        "forward_filled": False,
        "synthetic_no_trade": True,
        "series": [_section(section) for section in sections],
    }


def _coverage_bounds(fetched: Sequence[tuple[int, int]], start: int, end: int) -> tuple[int, int]:
    """Window plus any fetched span that sticks out of it, so a prior real is visible."""
    lo = start
    hi = end
    for span_lo, span_hi in fetched:
        lo = min(lo, span_lo)
        hi = max(hi, span_hi)
    return lo, hi


def _section(section: SeriesReport) -> dict[str, object]:
    return {
        "symbol": section.symbol,
        "mint": section.mint,
        "timeframe": section.timeframe,
        "start": _iso(section.start),
        "end": _iso(section.end),
        "expected": section.expected,
        "real": section.real,
        "synthetic": section.synthetic,
        "present": section.present,
        "missing": section.missing,
        "coverage_pct": section.coverage_pct,
        "unfetched": section.unfetched,
        "no_trade": section.no_trade,
        "long_no_trade_minutes": section.long_no_trade_minutes,
        "long_no_trade": [_long_no_trade(item) for item in section.long_no_trade],
        "suspect_outage": section.suspect_outage,
        "gaps": [_gap(gap) for gap in section.gaps],
        "rejected": [
            {
                "open_time": None if item.open_time is None else _iso(item.open_time),
                "reasons": list(item.reasons),
            }
            for item in section.rejected
        ],
    }


def _gap(gap: GapRange) -> dict[str, object]:
    return {"start": _iso(gap.start), "end": _iso(gap.end), "missing": gap.missing}


def _long_no_trade(item: LongNoTrade) -> dict[str, object]:
    return {"start": _iso(item.start), "end": _iso(item.end), "minutes": item.minutes}


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Parser(argparse.ArgumentParser):
    """Usage errors exit 1, the same code as every other CLI failure."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m app.ohlcv.backfill",
        description="Fetch native OHLCV for SOL and majors into a 24-hour cache.",
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
        help="comma-separated native timeframes (1m, 5m, 15m, 1h, 4h, 1d)",
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
    parser.add_argument(
        "--cache-ttl-hours",
        type=float,
        default=None,
        help="how long cached bars are kept, in hours (maximum 24)",
    )
    parser.add_argument("--provider", default="geckoterminal", help="OHLCV source name")
    parser.add_argument(
        "--long-no-trade-minutes",
        type=int,
        default=120,
        help="flag a quiet run longer than this many minutes (default 120); it stays synthetic",
    )
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
    symbols = [part.strip() for part in args.symbols.split(",") if part.strip()]
    if not symbols:
        print("at least one symbol is required", file=sys.stderr)
        return 1
    if args.days > PUBLIC_HISTORY_DAYS:
        print(PUBLIC_HISTORY_MESSAGE, file=sys.stderr)
        return 1
    try:
        ttl_seconds = _ttl_seconds(args.cache_ttl_hours)
        long_no_trade_minutes = _long_threshold(args.long_no_trade_minutes)
    except ValueError as exc:
        print(_one_line(exc), file=sys.stderr)
        return 1
    store: SqliteBarStore | None = None
    active: OhlcvSource | None = None
    owns_source = source is None
    try:
        if args.report is not None:
            _prepare_report_path(Path(args.report))
        assets = require_assets(symbols)
        timeframes = tuple(part.strip() for part in args.timeframes.split(",") if part.strip())
        clock = int(time.time()) if now is None else now
        start, end = plan_window(clock, args.days, timeframes)
        data_dir = Path(args.data_dir or os.environ.get("OHLCV_DATA_DIR") or "data")
        try:
            store = SqliteBarStore(data_dir / "ohlcv.sqlite", ttl_seconds=ttl_seconds)
        except OSError as exc:
            print(_data_dir_line(data_dir, exc), file=sys.stderr)
            return 1
        active = open_source(args.provider) if source is None else source
        report = run_backfill(
            source=active,
            store=store,
            assets=assets,
            start=start,
            end=end,
            timeframes=timeframes,
            user_id=LOCAL_USER_ID,
            generated_at=clock,
            progress=_progress,
            long_no_trade_minutes=long_no_trade_minutes,
        )
        text = json.dumps(report, indent=2) + "\n"
        if args.report is not None:
            Path(args.report).write_text(text, encoding="utf-8")
        sys.stdout.write(text)
    except KeyboardInterrupt:
        return 130
    except (SourceError, StoreError, ValueError, OSError, sqlite3.Error) as exc:
        print(_one_line(exc), file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()
        if owns_source and active is not None:
            close = getattr(active, "close", None)
            if close is not None:
                close()
    return 0


_TTL_ERROR = (
    "cache TTL must be a finite number of hours, greater than 0 and at most 24 hours; "
    "the CoinGecko API terms do not allow a longer store"
)


def _ttl_seconds(hours: float | None) -> int:
    """Resolve the cache TTL. Above 24 hours is an error, not a silent clamp.

    ``nan`` and ``inf`` are rejected with the same one-line message. They are
    not clamped and they are not turned into a zero TTL.
    """
    if hours is None:
        raw = os.environ.get("OHLCV_CACHE_TTL_HOURS")
        if raw is None or raw.strip() == "":
            return MAX_CACHE_TTL_SECONDS
        try:
            hours = float(raw)
        except ValueError as exc:
            raise ValueError(_TTL_ERROR) from exc
    if isinstance(hours, bool) or not isinstance(hours, (int, float)):
        raise ValueError(_TTL_ERROR)
    if not math.isfinite(hours) or hours <= 0 or hours > 24:
        raise ValueError(_TTL_ERROR)
    seconds = int(hours * 3600)
    if seconds < 1:
        seconds = 1
    if seconds > MAX_CACHE_TTL_SECONDS:
        raise ValueError(_TTL_ERROR)
    return seconds


def _long_threshold(minutes: int) -> int:
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1:
        raise ValueError("long no-trade threshold must be a whole number of minutes, at least 1")
    return minutes


def _writable(path: Path) -> bool:
    """True when ``path`` has a write bit and this process may write it.

    A mode with no write bit at all is read-only even for root, which is
    the case a ``chmod`` test uses. ``os.access`` still applies for a
    directory this user does not own.
    """
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    if mode & 0o222 == 0:
        return False
    return os.access(path, os.W_OK)


def _prepare_report_path(path: Path) -> None:
    """Fail before any fetch when the report cannot be written."""
    if path.exists() and path.is_dir():
        raise ValueError(f"report path {path} is a directory")
    parent = path.parent
    if parent.exists() and not parent.is_dir():
        raise ValueError(f"report directory {parent} is not a directory")
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    if not _writable(parent):
        raise ValueError(f"report directory {parent} is not writable")
    if path.exists() and not _writable(path):
        raise ValueError(f"report path {path} is not writable")


def _progress(message: str) -> None:
    print(message, file=sys.stderr)


def _data_dir_line(path: Path, exc: OSError) -> str:
    """One clean line for a data directory that cannot hold the cache."""
    if isinstance(exc, PermissionError):
        return f"data directory {path} is not writable"
    if isinstance(exc, (FileExistsError, NotADirectoryError)):
        return f"data directory {path} is not a directory"
    text = str(exc).strip().splitlines()
    return text[0] if text else f"data directory {path} is not usable"


def _one_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return text[0] if text else exc.__class__.__name__


if __name__ == "__main__":
    raise SystemExit(main())
