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
