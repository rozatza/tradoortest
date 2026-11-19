"""Backfill is resumable, idempotent, and does not fill gaps or bad bars."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.auth import LOCAL_USER_ID
from app.ohlcv.adapters import open_source
from app.ohlcv.backfill import main, plan_window, run_backfill
from app.ohlcv.bars import Bar, BarRejection
from app.ohlcv.errors import SourceError, StoreError
from app.ohlcv.registry import MAJORS, SOL_MINT
from app.ohlcv.source import FetchPage, FetchResult
from app.ohlcv.store import SqliteBarStore

_START = 1_767_225_600  # 2026-01-01T00:00:00Z
_MINT = SOL_MINT


class MemorySource:
    """In-process source. Same ``fetch_bars`` shape as the HTTP adapter."""

    name = "memory"

    def __init__(self, bars: list[Bar], rejected: list[BarRejection] | None = None) -> None:
        self.bars = bars
        self.rejected_bars = list(rejected or [])
        self.calls: list[tuple[str, str, int, int]] = []

    def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult:
        self.calls.append((mint, timeframe, start, end))
        bars = [
            bar
            for bar in self.bars
            if bar.mint == mint and bar.timeframe == timeframe and start <= bar.open_time < end
        ]
        rejected = [
            item
            for item in self.rejected_bars
            if item.mint == mint and (item.open_time is None or start <= item.open_time < end)
        ]
        return FetchResult(bars, rejected)


def _minute(offset: int, *, high: float = 2.0, close: float = 1.5) -> Bar:
    return Bar(_MINT, "1m", _START + offset, 1.0, high, 0.5, close, 1.0)


def test_plan_window_covers_at_least_ninety_days() -> None:
    now = _START + 90 * 86400 + 12345
    start, end = plan_window(now, 90, ("1m", "5m", "15m", "1h", "4h", "1d"))
    assert end - start >= 90 * 86400
    assert start % 86400 == 0
    assert end % 86400 == 0
    assert end <= now


def test_a_later_source_plugs_in_through_fetch_bars() -> None:
    with pytest.raises(SourceError, match="fetch_bars"):
        open_source("birdeye")


def test_registry_is_sol_plus_four_native_majors() -> None:
    assert [asset.symbol for asset in MAJORS] == ["SOL", "JUP", "JTO", "BONK", "WIF"]
    assert len({asset.mint for asset in MAJORS}) == 5


def test_gaps_stay_gaps_and_bad_bars_are_excluded(tmp_path: Path) -> None:
    good = [_minute(0), _minute(180)]
    bad = _minute(60, high=1.0, close=4.0)
    source = MemorySource([*good, bad])
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    try:
        report = run_backfill(
            source=source,
            store=store,
            assets=(MAJORS[0],),
            start=_START,
            end=_START + 300,
            timeframes=("1m",),
            user_id=LOCAL_USER_ID,
            generated_at=_START + 300,
        )
        stored = store.load(
            user_id=LOCAL_USER_ID,
            mint=_MINT,
            timeframe="1m",
            start=_START,
            end=_START + 300,
        )
        assert [bar.open_time for bar in stored] == [_START, _START + 180]
        assert bad not in stored
        series = report["series"]
        assert isinstance(series, list)
        section = series[0]
        assert section == {
            "symbol": "SOL",
            "mint": SOL_MINT,
            "timeframe": "1m",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T00:05:00Z",
            "expected": 5,
            "present": 2,
            "missing": 3,
            "unfetched": 0,
            "no_trade": 2,
            "gaps": [
                {
                    "start": "2026-01-01T00:01:00Z",
                    "end": "2026-01-01T00:03:00Z",
                    "missing": 2,
                },
                {
                    "start": "2026-01-01T00:04:00Z",
                    "end": "2026-01-01T00:05:00Z",
                    "missing": 1,
                },
            ],
            "unfetched_gaps": [],
            "no_trade_gaps": [
                {
                    "start": "2026-01-01T00:02:00Z",
                    "end": "2026-01-01T00:03:00Z",
                    "missing": 1,
                },
                {
                    "start": "2026-01-01T00:04:00Z",
                    "end": "2026-01-01T00:05:00Z",
                    "missing": 1,
                },
            ],
            "rejected": [
                {
                    "open_time": "2026-01-01T00:01:00Z",
                    "reasons": ["high is below max(open, close)"],
                }
            ],
        }
        assert report["forward_filled"] is False
        assert report["user_id"] == LOCAL_USER_ID
    finally:
        store.close()


def test_backfill_is_resumable_and_idempotent(tmp_path: Path) -> None:
    source = MemorySource([_minute(0), _minute(60), _minute(180)])
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    end = _START + 300
    try:
        run_backfill(
            source=source,
            store=store,
            assets=(MAJORS[0],),
            start=_START,
            end=end,
            timeframes=("1m",),
            user_id=LOCAL_USER_ID,
            generated_at=end,
        )
        assert source.calls == [(_MINT, "1m", _START, end)]
        run_backfill(
            source=source,
            store=store,
            assets=(MAJORS[0],),
            start=_START,
            end=end,
            timeframes=("1m",),
            user_id=LOCAL_USER_ID,
            generated_at=end,
        )
        # The holes are inside the span already fetched. They are no-trade.
        assert source.calls == [(_MINT, "1m", _START, end)]
        assert len(
            store.load(
                user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_START, end=end
            )
        ) == 3
        source.bars.extend([_minute(120), _minute(240)])
        report = run_backfill(
            source=source,
            store=store,
            assets=(MAJORS[0],),
            start=_START,
            end=end,
            timeframes=("1m",),
            user_id=LOCAL_USER_ID,
            generated_at=end,
        )
        assert source.calls == [(_MINT, "1m", _START, end)]
        assert len(
            store.load(
                user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_START, end=end
            )
        ) == 3
        section = report["series"][0]
        assert section["unfetched"] == 0
        assert section["no_trade"] == 2
    finally:
        store.close()


def test_resampled_timeframe_does_not_keep_a_stale_bar(tmp_path: Path) -> None:
    # Five-minute bucket at _START, which is midnight and therefore 5m-aligned.
    # Only four minutes exist, so the bucket is a gap. A previously stored
    # 5m candle in that bucket has to disappear.
    minutes = [_minute(offset) for offset in (0, 60, 120, 180)]
    source = MemorySource(minutes)
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    stale = Bar(_MINT, "5m", _START, 1.0, 2.0, 0.5, 1.5, 9.0)
    try:
        store.upsert([stale], user_id=LOCAL_USER_ID)
        report = run_backfill(
            source=source,
            store=store,
            assets=(MAJORS[0],),
            start=_START,
            end=_START + 300,
            timeframes=("5m",),
            user_id=LOCAL_USER_ID,
            generated_at=_START + 300,
        )
        assert store.load(
            user_id=LOCAL_USER_ID,
            mint=_MINT,
            timeframe="5m",
            start=_START,
            end=_START + 300,
        ) == []
        five = [section for section in report["series"] if section["timeframe"] == "5m"]
        assert five[0]["present"] == 0
        assert five[0]["missing"] == 1
        assert source.calls[0][1] == "1m"
    finally:
        store.close()


def test_cli_requests_a_ninety_day_window_without_the_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = _START + 90 * 86400
    source = MemorySource([_minute(0)])
    code = main(
        [
            "--days",
            "90",
            "--symbols",
            "SOL",
            "--timeframes",
            "1m",
            "--data-dir",
            str(tmp_path),
            "--report",
            str(tmp_path / "gap-report.json"),
        ],
        source=source,
        now=now,
    )
    assert code == 0
    assert source.calls
    _mint, timeframe, start, end = source.calls[0]
    assert timeframe == "1m"
    assert end - start >= 90 * 86400
    document = json.loads(capsys.readouterr().out)
    assert document["forward_filled"] is False
    assert document["provider"] == "memory"
    written = json.loads((tmp_path / "gap-report.json").read_text(encoding="utf-8"))
    assert written["series"][0]["symbol"] == "SOL"
    assert "secret" not in json.dumps(written).lower()


def test_ohlcv_data_dir_is_the_only_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OHLCV_DATA_DIR", str(tmp_path))
    now = _START + 86400
    code = main(
        ["--days", "1", "--symbols", "sol", "--timeframes", "1m"],
        source=MemorySource([]),
        now=now,
    )
    assert code == 0
    assert (tmp_path / "ohlcv.sqlite").is_file()
    assert json.loads(capsys.readouterr().out)["provider"] == "memory"


class _PagingSource:
    """Yields fixed pages and can fail before a later page is committed."""

    name = "memory"

    def __init__(self, pages: list[FetchPage], *, fail_on: int | None) -> None:
        self.pages = pages
        self.fail_on = fail_on
        self.calls: list[tuple[str, str, int, int]] = []

    def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult:
        raise AssertionError("backfill should checkpoint through fetch_pages")

    def fetch_pages(self, mint: str, timeframe: str, start: int, end: int):
        self.calls.append((mint, timeframe, start, end))
        yielded = 0
        for page in self.pages:
            if page.end <= start or page.start >= end:
                continue
            if self.fail_on is not None and yielded == self.fail_on:
                raise SourceError("source failed mid-run")
            yielded += 1
            yield page


def test_sparse_window_is_one_call_and_a_rerun_does_not_refetch(tmp_path: Path) -> None:
    end = _START + 1440 * 60
    source = MemorySource([_minute(0), _minute(60)])
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    try:
        report = run_backfill(
            source=source,
            store=store,
            assets=(MAJORS[0],),
            start=_START,
            end=end,
            timeframes=("1m",),
            user_id=LOCAL_USER_ID,
            generated_at=end,
        )
        assert source.calls == [(_MINT, "1m", _START, end)]
        section = report["series"][0]
        assert section["unfetched"] == 0
        assert section["no_trade"] == 1438
        assert section["unfetched_gaps"] == []
        run_backfill(
            source=source,
            store=store,
            assets=(MAJORS[0],),
            start=_START,
            end=end,
            timeframes=("1m",),
            user_id=LOCAL_USER_ID,
            generated_at=end,
        )
        assert source.calls == [(_MINT, "1m", _START, end)]
    finally:
        store.close()


def test_failure_mid_run_keeps_committed_pages_only(tmp_path: Path) -> None:
    end = _START + 300
    mid = _START + 120
    pages = [
        FetchPage([_minute(0), _minute(60)], [], _START, mid),
        FetchPage([_minute(180)], [], mid, end),
    ]
    source = _PagingSource(pages, fail_on=1)
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    try:
        with pytest.raises(SourceError, match="mid-run"):
            run_backfill(
                source=source,
                store=store,
                assets=(MAJORS[0],),
                start=_START,
                end=end,
                timeframes=("1m",),
                user_id=LOCAL_USER_ID,
                generated_at=end,
            )
        stored = store.load(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_START, end=end
        )
        assert [bar.open_time for bar in stored] == [_START, _START + 60]
        assert store.fetched_ranges(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_START, end=end
        ) == [(_START, mid)]
        source.fail_on = None
        run_backfill(
            source=source,
            store=store,
            assets=(MAJORS[0],),
            start=_START,
            end=end,
            timeframes=("1m",),
            user_id=LOCAL_USER_ID,
            generated_at=end,
        )
        assert source.calls == [
            (_MINT, "1m", _START, end),
            (_MINT, "1m", mid, end),
        ]
        stored = store.load(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_START, end=end
        )
        assert [bar.open_time for bar in stored] == [_START, _START + 60, _START + 180]
        assert store.fetched_ranges(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_START, end=end
        ) == [(_START, end)]
    finally:
        store.close()


def test_days_above_the_public_history_limit_fails_before_a_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = MemorySource([])
    code = main(
        ["--days", "181", "--symbols", "SOL", "--timeframes", "1m", "--data-dir", str(tmp_path)],
        source=source,
        now=_START + 200 * 86400,
    )
    assert code == 1
    assert source.calls == []
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "180" in err
    assert err.strip().count("\n") == 0
    with pytest.raises(ValueError, match="180"):
        plan_window(_START + 200 * 86400, 181, ("1m",))


def test_empty_symbols_exit_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["--days", "1", "--symbols", ",", "--timeframes", "1m", "--data-dir", str(tmp_path)],
        source=MemorySource([]),
        now=_START + 86400,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert err.strip() == "at least one symbol is required"
    assert "Traceback" not in err


def test_cli_rejects_a_user_id_flag(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "--days",
                "1",
                "--symbols",
                "SOL",
                "--timeframes",
                "1m",
                "--data-dir",
                str(tmp_path),
                "--user-id",
                "someone-else",
            ],
            source=MemorySource([]),
            now=_START + 86400,
        )
    assert caught.value.code != 0


def test_source_error_is_one_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class Boom:
        name = "boom"

        def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult:
            raise SourceError("GeckoTerminal request failed")

    code = main(
        ["--days", "1", "--symbols", "SOL", "--timeframes", "1m", "--data-dir", str(tmp_path)],
        source=Boom(),
        now=_START + 86400,
    )
    assert code == 1
    assert capsys.readouterr().err.strip() == "GeckoTerminal request failed"


def test_bad_paths_and_sqlite_errors_are_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = _START + 86400
    args = ["--days", "1", "--symbols", "SOL", "--timeframes", "1m"]

    dev_null = main([*args, "--data-dir", "/dev/null"], source=MemorySource([]), now=now)
    dev_err = capsys.readouterr().err
    assert dev_null == 1
    assert "Traceback" not in dev_err
    assert dev_err.strip() != ""
    assert "\n" not in dev_err.strip()

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    unwritable = main([*args, "--data-dir", str(blocked)], source=MemorySource([]), now=now)
    unwritable_err = capsys.readouterr().err
    assert unwritable == 1
    assert "Traceback" not in unwritable_err
    assert "\n" not in unwritable_err.strip()

    report_parent = tmp_path / "report-parent"
    report_parent.write_text("x", encoding="utf-8")
    bad_report = main(
        [
            *args,
            "--data-dir",
            str(tmp_path / "data"),
            "--report",
            str(report_parent / "gap.json"),
        ],
        source=MemorySource([_minute(0)]),
        now=now,
    )
    report_err = capsys.readouterr().err
    assert bad_report == 1
    assert "Traceback" not in report_err
    assert "\n" not in report_err.strip()

    def locked(self: SqliteBarStore, path: Path) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteBarStore, "__init__", locked)
    sqlite_code = main(
        [*args, "--data-dir", str(tmp_path / "db")],
        source=MemorySource([]),
        now=now,
    )
    sqlite_err = capsys.readouterr().err
    assert sqlite_code == 1
    assert sqlite_err.strip() == "database is locked"
    assert "Traceback" not in sqlite_err


def test_unwritable_data_dir_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mkdir = Path.mkdir

    def mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "unwritable":
            raise PermissionError(13, "Permission denied")
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", mkdir)
    code = main(
        [
            "--days",
            "1",
            "--symbols",
            "SOL",
            "--timeframes",
            "1m",
            "--data-dir",
            str(tmp_path / "unwritable"),
        ],
        source=MemorySource([]),
        now=_START + 86400,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert err.strip() == "[Errno 13] Permission denied"
    assert "Traceback" not in err


def test_store_error_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: SqliteBarStore, path: Path) -> None:
        raise StoreError("store failed")

    monkeypatch.setattr(SqliteBarStore, "__init__", refuse)
    code = main(
        ["--days", "1", "--symbols", "SOL", "--timeframes", "1m", "--data-dir", str(tmp_path)],
        source=MemorySource([]),
        now=_START + 86400,
    )
    assert code == 1
    assert capsys.readouterr().err.strip() == "store failed"
