"""Local bar store.

SQLite is the store that ships. ``BarStore`` is the seam a Postgres
implementation would fill. No Postgres driver is installed, and nothing
here reads a database URL.

Rows carry ``user_id`` so a later session can filter on the auth seam.
This process still serves one user. There is no user list and no sharing.

The file lives under the gitignored ``data/`` directory. Bar rows are
never written into the repository.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from app.ohlcv.bars import Bar, validate_bar
from app.ohlcv.errors import StoreError

_CREATE = """
CREATE TABLE IF NOT EXISTS bars (
    user_id TEXT NOT NULL,
    mint TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (user_id, mint, timeframe, open_time)
)
"""

_UPSERT = """
INSERT INTO bars (
    user_id, mint, timeframe, open_time, open, high, low, close, volume
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(user_id, mint, timeframe, open_time) DO UPDATE SET
    open = excluded.open,
    high = excluded.high,
    low = excluded.low,
    close = excluded.close,
    volume = excluded.volume
"""


class BarStore(Protocol):
    """What the backfill needs from a store. Postgres would implement this."""

    def upsert(self, bars: Sequence[Bar], *, user_id: str) -> int: ...

    def replace_window(
        self,
        bars: Sequence[Bar],
        *,
        user_id: str,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
    ) -> None: ...

    def load(
        self,
        *,
        user_id: str,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
    ) -> list[Bar]: ...

    def open_times(
        self,
        *,
        user_id: str,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
    ) -> set[int]: ...


class SqliteBarStore:
    """One SQLite file. Upserts are idempotent on the primary key."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute(_CREATE)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def upsert(self, bars: Sequence[Bar], *, user_id: str) -> int:
        rows = [_row(user_id, bar) for bar in bars]
        with self._conn:
            self._conn.executemany(_UPSERT, rows)
        return len(rows)

    def replace_window(
        self,
        bars: Sequence[Bar],
        *,
        user_id: str,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
    ) -> None:
        """Replace every stored bar of this series inside ``[start, end)``.

        Used for resampled timeframes so a bucket that is no longer complete
        does not keep an old candle. The delete is the whole window, then
        the new bars are inserted. It does not fill minutes the list omits.
        """
        _owner(user_id)
        for bar in bars:
            if bar.mint != mint or bar.timeframe != timeframe:
                raise StoreError("replace_window bars must match the requested series")
            if bar.open_time < start or bar.open_time >= end:
                raise StoreError("replace_window bars must fall inside the window")
        rows = [_row(user_id, bar) for bar in bars]
        with self._conn:
            self._conn.execute(
                """
                DELETE FROM bars
                WHERE user_id = ? AND mint = ? AND timeframe = ?
                  AND open_time >= ? AND open_time < ?
                """,
                (user_id, mint, timeframe, start, end),
            )
            self._conn.executemany(_UPSERT, rows)

    def load(
        self,
        *,
        user_id: str,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
    ) -> list[Bar]:
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT mint, timeframe, open_time, open, high, low, close, volume
            FROM bars
            WHERE user_id = ? AND mint = ? AND timeframe = ?
              AND open_time >= ? AND open_time < ?
            ORDER BY open_time
            """,
            (user_id, mint, timeframe, start, end),
        )
        return [
            Bar(
                mint=row[0],
                timeframe=row[1],
                open_time=row[2],
                open=row[3],
                high=row[4],
                low=row[5],
                close=row[6],
                volume=row[7],
            )
            for row in cursor
        ]

    def open_times(
        self,
        *,
        user_id: str,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
    ) -> set[int]:
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT open_time FROM bars
            WHERE user_id = ? AND mint = ? AND timeframe = ?
              AND open_time >= ? AND open_time < ?
            """,
            (user_id, mint, timeframe, start, end),
        )
        return {row[0] for row in cursor}


def _owner(user_id: str) -> None:
    if not isinstance(user_id, str) or user_id == "":
        raise StoreError("user_id is required")


def _row(user_id: str, bar: Bar) -> tuple[object, ...]:
    _owner(user_id)
    reasons = validate_bar(bar)
    if reasons:
        raise StoreError("refusing to store a bar that failed validation: " + "; ".join(reasons))
    return (
        user_id,
        bar.mint,
        bar.timeframe,
        bar.open_time,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
    )
