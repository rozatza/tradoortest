"""Backfill is resumable, idempotent, and does not fill gaps or bad bars."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.auth import LOCAL_USER_ID
from app.ohlcv.adapters import open_source
from app.ohlcv.adapters.geckoterminal import GeckoTerminalSource
from app.ohlcv.backfill import main, plan_window, run_backfill
from app.ohlcv.bars import Bar, BarRejection
from app.ohlcv.errors import SourceError, StoreError
from app.ohlcv.registry import MAJORS, SOL_MINT
from app.ohlcv.source import FetchPage, FetchResult
from app.ohlcv.store import SqliteBarStore
from app.ohlcv.synthetic import synthetic_minutes

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


def _real_times(store: SqliteBarStore, end: int) -> list[int]:
    stored = store.load(
        user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_START, end=end
    )
    return [bar.open_time for bar in stored if not bar.synthetic]


def _cli_errors(err: str) -> list[str]:
    return [line for line in err.splitlines() if line and not line.startswith("ohlcv:")]


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
        real = [bar for bar in stored if not bar.synthetic]
        synthetic = [bar for bar in stored if bar.synthetic]
        assert [bar.open_time for bar in real] == [_START, _START + 180]
        assert [bar.open_time for bar in synthetic] == [_START + 120, _START + 240]
        assert all(
            bar.volume == 0.0 and bar.open == bar.high == bar.low == bar.close == 1.5
            for bar in synthetic
        )
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
            "real": 2,
            "synthetic": 2,
            "present": 4,
            "missing": 1,
            "coverage_pct": 40.0,
            "unfetched": 0,
            "no_trade": 2,
            "long_no_trade_minutes": 120,
            "long_no_trade": [],
            "suspect_outage": False,
            "gaps": [],
            "rejected": [
                {
                    "open_time": "2026-01-01T00:01:00Z",
                    "reasons": ["high is below max(open, close)"],
                }
            ],
        }
        assert report["forward_filled"] is False
        assert report["synthetic_no_trade"] is True
        assert report["attribution"] == "Data by GeckoTerminal / CoinGecko"
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
        assert _real_times(store, end) == [_START, _START + 60, _START + 180]
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
        assert _real_times(store, end) == [_START, _START + 60, _START + 180]
        section = report["series"][0]
        assert section["unfetched"] == 0
        assert section["synthetic"] == 2
        assert section["gaps"] == []
    finally:
        store.close()


def test_higher_timeframe_is_fetched_natively(tmp_path: Path) -> None:
    native = Bar(_MINT, "5m", _START, 1.0, 2.0, 0.5, 1.5, 9.0)
    source = MemorySource([native])
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    try:
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
        stored = store.load(
            user_id=LOCAL_USER_ID,
            mint=_MINT,
            timeframe="5m",
            start=_START,
            end=_START + 300,
        )
        assert stored == [native]
        assert stored[0].synthetic is False
        assert source.calls == [(_MINT, "5m", _START, _START + 300)]
        five = report["series"][0]
        assert five["timeframe"] == "5m"
        assert five["real"] == 1
        assert five["synthetic"] == 0
        assert five["gaps"] == []
        assert store.load(
            user_id=LOCAL_USER_ID,
            mint=_MINT,
            timeframe="1m",
            start=_START,
            end=_START + 300,
        ) == []
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
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["forward_filled"] is False
    assert document["provider"] == "memory"
    assert "ohlcv: SOL 1m fetching" in captured.err
    assert "ohlcv: SOL 1m committed page" in captured.err
    written = json.loads((tmp_path / "gap-report.json").read_text(encoding="utf-8"))
    assert written["series"][0]["symbol"] == "SOL"
    assert "secret" not in json.dumps(written).lower()


def test_ohlcv_data_dir_selects_the_cache_directory(
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
        assert section["real"] == 2
        assert section["synthetic"] == 1438
        assert section["missing"] == 0
        assert section["no_trade"] == 1438
        assert section["gaps"] == []
        assert section["suspect_outage"] is True
        assert section["long_no_trade"] == [
            {
                "start": "2026-01-01T00:02:00Z",
                "end": "2026-01-02T00:00:00Z",
                "minutes": 1438,
            }
        ]
        assert section["coverage_pct"] == round(100.0 * 2 / 1440, 2)
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
        assert _real_times(store, end) == [_START, _START + 60, _START + 180]
        assert store.fetched_ranges(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_START, end=end
        ) == [(_START, mid), (mid, end)]
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
    assert caught.value.code == 1


def test_dns_failure_through_the_cli_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns lookup failed")

    source = GeckoTerminalSource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        min_interval=0,
        sleep=lambda _seconds: None,
    )
    code = main(
        ["--days", "1", "--symbols", "SOL", "--timeframes", "1m", "--data-dir", str(tmp_path)],
        source=source,
        now=_START + 86400,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert _cli_errors(err) == ["GeckoTerminal request failed"]
    assert "dns lookup failed" not in err


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
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert _cli_errors(err) == ["GeckoTerminal request failed"]


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
    report_source = MemorySource([_minute(0)])
    bad_report = main(
        [
            *args,
            "--data-dir",
            str(tmp_path / "data"),
            "--report",
            str(report_parent / "gap.json"),
        ],
        source=report_source,
        now=now,
    )
    assert report_source.calls == []
    report_err = capsys.readouterr().err
    assert bad_report == 1
    assert "Traceback" not in report_err
    assert len(_cli_errors(report_err)) == 1

    def locked(self: SqliteBarStore, path: Path, **kwargs: object) -> None:
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
    def refuse(self: SqliteBarStore, path: Path, **kwargs: object) -> None:
        raise StoreError("store failed")

    monkeypatch.setattr(SqliteBarStore, "__init__", refuse)
    code = main(
        ["--days", "1", "--symbols", "SOL", "--timeframes", "1m", "--data-dir", str(tmp_path)],
        source=MemorySource([]),
        now=_START + 86400,
    )
    assert code == 1
    assert capsys.readouterr().err.strip() == "store failed"


def test_cache_ttl_above_24_hours_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = MemorySource([])
    code = main(
        [
            "--days",
            "1",
            "--symbols",
            "SOL",
            "--timeframes",
            "1m",
            "--data-dir",
            str(tmp_path),
            "--cache-ttl-hours",
            "48",
        ],
        source=source,
        now=_START + 86400,
    )
    assert code == 1
    assert source.calls == []
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert _cli_errors(err) == [
        "cache TTL must be a finite number of hours, greater than 0 and at most 24 hours; "
        "the CoinGecko API terms do not allow a longer store"
    ]
    monkeypatch.setenv("OHLCV_CACHE_TTL_HOURS", "25")
    again = main(
        ["--days", "1", "--symbols", "SOL", "--timeframes", "1m", "--data-dir", str(tmp_path)],
        source=source,
        now=_START + 86400,
    )
    assert again == 1
    assert "24 hours" in capsys.readouterr().err


def test_expired_cache_is_refetched(tmp_path: Path) -> None:
    clock = {"now": _START + 400}
    source = MemorySource([_minute(0)])
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite", ttl_seconds=60, now=lambda: clock["now"])
    end = _START + 120
    try:
        run_backfill(
            source=source,
            store=store,
            assets=(MAJORS[0],),
            start=_START,
            end=end,
            timeframes=("1m",),
            user_id=LOCAL_USER_ID,
            generated_at=clock["now"],
        )
        assert source.calls == [(_MINT, "1m", _START, end)]
        clock["now"] += 59
        run_backfill(
            source=source,
            store=store,
            assets=(MAJORS[0],),
            start=_START,
            end=end,
            timeframes=("1m",),
            user_id=LOCAL_USER_ID,
            generated_at=clock["now"],
        )
        assert source.calls == [(_MINT, "1m", _START, end)]
        clock["now"] += 1
        run_backfill(
            source=source,
            store=store,
            assets=(MAJORS[0],),
            start=_START,
            end=end,
            timeframes=("1m",),
            user_id=LOCAL_USER_ID,
            generated_at=clock["now"],
        )
        assert source.calls == [(_MINT, "1m", _START, end), (_MINT, "1m", _START, end)]
    finally:
        store.close()


def test_prior_close_does_not_cross_an_unfetched_hole(tmp_path: Path) -> None:
    end = _START + 360
    later = Bar(_MINT, "1m", _START + 240, 2.0, 4.0, 1.0, 3.0, 1.0)

    class HoleSource:
        name = "memory"

        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult:
            raise AssertionError("pages")

        def fetch_pages(self, mint: str, timeframe: str, start: int, end: int):
            self.calls.append((start, end))
            if start == _START:
                yield FetchPage([_minute(0)], [], _START, _START + 120)
                return
            yield FetchPage([later], [], _START + 240, end)

    source = HoleSource()
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
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
        by_time = {bar.open_time: bar for bar in stored}
        assert _START + 60 in by_time and by_time[_START + 60].synthetic
        assert _START + 120 not in by_time
        assert _START + 180 not in by_time
        assert by_time[_START + 300].synthetic
        assert by_time[_START + 300].close == 3.0
        assert by_time[_START + 300].volume == 0.0
    finally:
        store.close()


def test_synthetic_run_keeps_a_prior_close_at_any_length() -> None:
    real_time = _START
    seeded = synthetic_minutes(
        mint=_MINT,
        real={real_time: (2.5, 10)},
        rejected=set(),
        covered=[(real_time, real_time + 180)],
        start=real_time + 60,
        end=real_time + 180,
    )
    assert [bar.open_time for bar, _stored in seeded] == [real_time + 60, real_time + 120]
    assert all(bar.synthetic and bar.close == 2.5 and bar.volume == 0.0 for bar, _stored in seeded)
    long = synthetic_minutes(
        mint=_MINT,
        real={real_time: (1.0, 1)},
        rejected=set(),
        covered=[(real_time, real_time + 17 * 60)],
        start=real_time,
        end=real_time + 17 * 60,
    )
    assert len(long) == 16
    assert all(bar.synthetic and bar.close == 1.0 for bar, _stored in long)


def test_a_long_quiet_run_stays_synthetic_and_is_not_a_gap(tmp_path: Path) -> None:
    end = _START + 21 * 60
    later = _minute(20 * 60, high=4.0, close=3.0)
    source = MemorySource([_minute(0), later])
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
        stored = store.load(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_START, end=end
        )
        synthetic = [bar for bar in stored if bar.synthetic]
        assert len(synthetic) == 19
        assert all(bar.close == 1.5 and bar.volume == 0.0 for bar in synthetic)
        section = report["series"][0]
        assert section["synthetic"] == 19
        assert section["no_trade"] == 19
        assert section["unfetched"] == 0
        assert section["gaps"] == []
        assert section["long_no_trade"] == []
        assert section["suspect_outage"] is False
    finally:
        store.close()


def test_shifted_windows_store_the_same_series_in_either_order(tmp_path: Path) -> None:
    later = Bar(_MINT, "1m", _START + 300, 3.0, 5.0, 2.0, 4.0, 1.0)
    windows = [(_START, _START + 300), (_START + 120, _START + 480)]

    def series(name: str, order: list[tuple[int, int]]) -> list[tuple[int, float, bool]]:
        source = MemorySource([_minute(0), later])
        store = SqliteBarStore(tmp_path / name / "ohlcv.sqlite")
        try:
            for start, end in order:
                run_backfill(
                    source=source,
                    store=store,
                    assets=(MAJORS[0],),
                    start=start,
                    end=end,
                    timeframes=("1m",),
                    user_id=LOCAL_USER_ID,
                    generated_at=end,
                )
            stored = store.load(
                user_id=LOCAL_USER_ID,
                mint=_MINT,
                timeframe="1m",
                start=_START,
                end=_START + 480,
            )
            return [(bar.open_time, bar.close, bar.synthetic) for bar in stored]
        finally:
            store.close()

    forward = series("forward", windows)
    backward = series("backward", list(reversed(windows)))
    assert forward == backward
    by_time = {open_time: (close, synthetic) for open_time, close, synthetic in forward}
    assert by_time[_START + 120] == (1.5, True)
    assert by_time[_START + 240] == (1.5, True)
    assert by_time[_START + 300] == (4.0, False)
    assert by_time[_START + 360] == (4.0, True)


def test_short_page_before_the_window_start_is_a_leading_gap(tmp_path: Path) -> None:
    end = _START + 600
    candles = [
        [_START + 540, 1.0, 2.0, 0.5, 1.5, 1.0],
        [_START + 480, 1.0, 2.0, 0.5, 1.5, 1.0],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "include_empty_intervals" not in request.url.params
        before = int(request.url.params["before_timestamp"])
        limit = int(request.url.params["limit"])
        page = sorted((row for row in candles if row[0] <= before), reverse=True)[:limit]
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": page}}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = GeckoTerminalSource(client, page_limit=2, min_interval=0, sleep=lambda _seconds: None)
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
        section = report["series"][0]
        assert section["real"] == 2
        assert section["synthetic"] == 0
        assert section["unfetched"] == 0
        assert section["gaps"] == [
            {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:08:00Z", "missing": 8}
        ]
    finally:
        store.close()
        client.close()


def test_history_that_starts_mid_window_is_a_leading_gap(tmp_path: Path) -> None:
    # One short page. The adapter records coverage from the window start,
    # and the minutes before the first candle are a gap.
    end = _START + 600
    candle = [_START + 300, 1.0, 2.0, 0.5, 1.5, 1.0]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": [candle]}}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = GeckoTerminalSource(client, min_interval=0, sleep=lambda _seconds: None)
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
        section = report["series"][0]
        assert section["real"] == 1
        assert section["unfetched"] == 0
        assert section["gaps"][0] == {
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T00:05:00Z",
            "missing": 5,
        }
        stored = store.load(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_START, end=end
        )
        assert stored[0].open_time == _START + 300
        assert stored[0].synthetic is False
        assert any(bar.synthetic for bar in stored)
    finally:
        store.close()
        client.close()


def test_ctrl_c_exits_130_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Interrupt:
        name = "memory"

        def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult:
            raise KeyboardInterrupt

    code = main(
        ["--days", "1", "--symbols", "SOL", "--timeframes", "1m", "--data-dir", str(tmp_path)],
        source=Interrupt(),
        now=_START + 86400,
    )
    assert code == 130
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_nan_and_inf_ttl_are_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = MemorySource([_minute(0)])
    code = main(
        [
            "--days",
            "1",
            "--symbols",
            "SOL",
            "--timeframes",
            "1m",
            "--data-dir",
            str(tmp_path),
            "--cache-ttl-hours",
            "nan",
        ],
        source=source,
        now=_START + 86400,
    )
    assert code == 1
    assert source.calls == []
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert err.strip().count("\n") == 0
    assert "finite" in err
    monkeypatch.setenv("OHLCV_CACHE_TTL_HOURS", "inf")
    again = main(
        ["--days", "1", "--symbols", "SOL", "--timeframes", "1m", "--data-dir", str(tmp_path)],
        source=source,
        now=_START + 86400,
    )
    assert again == 1
    assert source.calls == []
    assert "finite" in capsys.readouterr().err
    assert "Traceback" not in capsys.readouterr().err


def test_quiet_series_has_no_gaps_and_flags_the_long_run(tmp_path: Path) -> None:
    # Sparse on purpose: short quiet runs stay synthetic, one run over 120
    # minutes is flagged, and none of it is a gap. Not a live series.
    end = _START + 200 * 60
    source = MemorySource([_minute(0), _minute(40 * 60), _minute(191 * 60, high=4.0, close=3.0)])
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
        section = report["series"][0]
        assert section["gaps"] == []
        assert section["unfetched"] == 0
        assert section["real"] == 3
        assert section["synthetic"] == 197
        assert section["no_trade"] == 197
        assert section["present"] == 200
        assert section["missing"] == 0
        assert section["suspect_outage"] is True
        assert section["long_no_trade"] == [
            {
                "start": "2026-01-01T00:41:00Z",
                "end": "2026-01-01T03:11:00Z",
                "minutes": 150,
            }
        ]
        stored = store.load(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_START, end=end
        )
        assert sum(1 for bar in stored if bar.synthetic) == 197
    finally:
        store.close()


def test_a_three_hour_provider_outage_is_a_gap(tmp_path: Path) -> None:
    quiet_end = _START + 30 * 60
    outage_end = quiet_end + 3 * 3600
    end = outage_end + 60
    later = _minute(outage_end - _START, high=4.0, close=3.0)
    pages = [
        FetchPage([_minute(0)], [], _START, quiet_end),
        FetchPage([], [], quiet_end, outage_end),
        FetchPage([later], [], outage_end, end),
    ]
    source = _PagingSource(pages, fail_on=None)
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
        stored = store.load(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_START, end=end
        )
        assert not any(quiet_end <= bar.open_time < outage_end for bar in stored)
        assert any(bar.synthetic and bar.open_time < quiet_end for bar in stored)
        section = report["series"][0]
        assert section["unfetched"] == 0
        assert section["gaps"] == [
            {
                "start": "2026-01-01T00:30:00Z",
                "end": "2026-01-01T03:30:00Z",
                "missing": 180,
            }
        ]
    finally:
        store.close()


def test_missing_native_buckets_are_a_count_not_gap_ranges(tmp_path: Path) -> None:
    end = _START + 31 * 300
    native = Bar(_MINT, "5m", _START, 1.0, 2.0, 0.5, 1.5, 9.0)
    source = MemorySource([native])
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    try:
        report = run_backfill(
            source=source,
            store=store,
            assets=(MAJORS[0],),
            start=_START,
            end=end,
            timeframes=("5m",),
            user_id=LOCAL_USER_ID,
            generated_at=end,
        )
        section = report["series"][0]
        assert section["real"] == 1
        assert section["expected"] == 31
        assert section["no_trade"] == 30
        assert section["missing"] == 30
        assert section["gaps"] == []
        assert section["coverage_pct"] == round(100.0 / 31, 2)
        assert section["long_no_trade"] == [
            {"start": "2026-01-01T00:05:00Z", "end": "2026-01-01T02:35:00Z", "minutes": 150}
        ]
    finally:
        store.close()


def test_read_only_report_dir_is_rejected_before_fetch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    report_dir.chmod(0o555)
    source = MemorySource([_minute(0)])
    try:
        code = main(
            [
                "--days",
                "1",
                "--symbols",
                "SOL",
                "--timeframes",
                "1m",
                "--data-dir",
                str(tmp_path / "data"),
                "--report",
                str(report_dir / "gap-report.json"),
            ],
            source=source,
            now=_START + 86400,
        )
        assert code == 1
        assert source.calls == []
        err = capsys.readouterr().err
        assert "not writable" in err
        assert "Traceback" not in err
    finally:
        report_dir.chmod(0o755)
