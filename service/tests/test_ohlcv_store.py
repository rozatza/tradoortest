"""SQLite bar store: idempotent writes, user_id seam, no silent repair."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.auth import LOCAL_USER_ID
from app.ohlcv.bars import Bar
from app.ohlcv.errors import StoreError
from app.ohlcv.store import SqliteBarStore

_MINT = "Mint111111111111111111111111111111111111111"
_T = 1_700_000_040


def _bar(close: float = 11.0, open_time: int = _T) -> Bar:
    return Bar(_MINT, "1m", open_time, 10.0, max(12.0, close), 8.0, close, 3.0)


def test_upsert_is_idempotent_and_isolated_by_user(tmp_path: Path) -> None:
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    try:
        assert store.upsert([_bar()], user_id=LOCAL_USER_ID) == 1
        assert store.upsert([_bar(close=15.0)], user_id=LOCAL_USER_ID) == 1
        stored = store.load(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_T, end=_T + 60
        )
        assert len(stored) == 1
        assert stored[0].close == 15.0
        assert (
            store.load(user_id="someone-else", mint=_MINT, timeframe="1m", start=_T, end=_T + 60)
            == []
        )
    finally:
        store.close()


def test_store_refuses_a_bar_that_fails_validation(tmp_path: Path) -> None:
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    try:
        bad = Bar(_MINT, "1m", _T, 10.0, 9.0, 8.0, 11.0, 1.0)
        with pytest.raises(StoreError, match="high is below"):
            store.upsert([bad], user_id=LOCAL_USER_ID)
        assert store.open_times(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_T, end=_T + 60
        ) == set()
    finally:
        store.close()


def test_replace_window_drops_a_bucket_that_is_no_longer_present(tmp_path: Path) -> None:
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    try:
        store.upsert([_bar()], user_id=LOCAL_USER_ID)
        store.replace_window(
            [],
            user_id=LOCAL_USER_ID,
            mint=_MINT,
            timeframe="1m",
            start=_T,
            end=_T + 60,
        )
        assert store.load(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_T, end=_T + 120
        ) == []
    finally:
        store.close()


def test_bar_directory_is_gitignored() -> None:
    root = Path(__file__).resolve().parents[2]
    ignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert "\ndata/\n" in f"\n{ignore}\n"


def test_ttl_above_24_hours_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="24 hours"):
        SqliteBarStore(tmp_path / "ohlcv.sqlite", ttl_seconds=24 * 3600 + 1)
    with pytest.raises(ValueError, match="whole number"):
        SqliteBarStore(tmp_path / "ohlcv.sqlite", ttl_seconds=True)  # type: ignore[arg-type]


def test_rows_and_fetched_ranges_expire_and_reopen_purges(tmp_path: Path) -> None:
    clock = {"now": 1_800_000_000}
    path = tmp_path / "ohlcv.sqlite"
    store = SqliteBarStore(path, ttl_seconds=3600, now=lambda: clock["now"])
    try:
        store.commit_page(
            [_bar()],
            [],
            user_id=LOCAL_USER_ID,
            mint=_MINT,
            timeframe="1m",
            start=_T,
            end=_T + 60,
            window_start=_T,
            window_end=_T + 60,
        )
        clock["now"] += 3599
        assert len(
            store.load(user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_T, end=_T + 60)
        ) == 1
        assert store.fetched_ranges(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_T, end=_T + 60
        ) == [(_T, _T + 60)]
        clock["now"] += 1
        assert (
            store.load(user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_T, end=_T + 60)
            == []
        )
        assert (
            store.fetched_ranges(
                user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_T, end=_T + 60
            )
            == []
        )
    finally:
        store.close()
    reopened = SqliteBarStore(path, ttl_seconds=3600, now=lambda: clock["now"])
    try:
        assert (
            reopened.load(user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_T, end=_T + 60)
            == []
        )
    finally:
        reopened.close()


def test_a_write_purges_rows_that_have_reached_the_ttl(tmp_path: Path) -> None:
    clock = {"now": 1_800_000_000}
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite", ttl_seconds=60, now=lambda: clock["now"])
    try:
        store.upsert([_bar()], user_id=LOCAL_USER_ID)
        clock["now"] += 60
        store.upsert([_bar(open_time=_T + 60)], user_id=LOCAL_USER_ID)
        stored = store.load(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_T, end=_T + 120
        )
        assert [bar.open_time for bar in stored] == [_T + 60]
    finally:
        store.close()


def test_an_older_page_expires_without_extending_its_ttl(tmp_path: Path) -> None:
    clock = {"now": 1_800_000_000}
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite", ttl_seconds=100, now=lambda: clock["now"])
    try:
        store.commit_page(
            [_bar()],
            [],
            user_id=LOCAL_USER_ID,
            mint=_MINT,
            timeframe="1m",
            start=_T,
            end=_T + 60,
            window_start=_T,
            window_end=_T + 120,
        )
        clock["now"] += 50
        store.commit_page(
            [_bar(open_time=_T + 60)],
            [],
            user_id=LOCAL_USER_ID,
            mint=_MINT,
            timeframe="1m",
            start=_T + 60,
            end=_T + 120,
            window_start=_T,
            window_end=_T + 120,
        )
        clock["now"] += 50
        assert store.fetched_ranges(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_T, end=_T + 120
        ) == [(_T + 60, _T + 120)]
        stored = store.load(
            user_id=LOCAL_USER_ID, mint=_MINT, timeframe="1m", start=_T, end=_T + 120
        )
        assert [bar.open_time for bar in stored] == [_T + 60]
    finally:
        store.close()


def test_a_fetched_page_cannot_contain_a_synthetic_bar(tmp_path: Path) -> None:
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    synthetic = Bar(_MINT, "1m", _T, 11.0, 11.0, 11.0, 11.0, 0.0, True)
    try:
        with pytest.raises(StoreError, match="synthetic"):
            store.commit_page(
                [synthetic],
                [],
                user_id=LOCAL_USER_ID,
                mint=_MINT,
                timeframe="1m",
                start=_T,
                end=_T + 60,
                window_start=_T,
                window_end=_T + 60,
            )
    finally:
        store.close()
