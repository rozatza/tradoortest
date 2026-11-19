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

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from app.ohlcv.bars import Bar, BarRejection, validate_bar
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
);
CREATE TABLE IF NOT EXISTS fetched (
    user_id TEXT NOT NULL,
    mint TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    PRIMARY KEY (user_id, mint, timeframe, start)
);
CREATE TABLE IF NOT EXISTS rejections (
    user_id TEXT NOT NULL,
    mint TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    reasons TEXT NOT NULL,
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

    def fetched_ranges(
        self,
        *,
        user_id: str,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
    ) -> list[tuple[int, int]]: ...

    def rejections(
        self,
        *,
        user_id: str,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
    ) -> list[BarRejection]: ...

    def commit_page(
        self,
        bars: Sequence[Bar],
        rejections: Sequence[BarRejection],
        *,
        user_id: str,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
    ) -> None: ...


class SqliteBarStore:
    """One SQLite file. Upserts are idempotent on the primary key."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_CREATE)
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

    def fetched_ranges(
        self,
        *,
        user_id: str,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
    ) -> list[tuple[int, int]]:
        """Stored half-open spans that overlap ``[start, end)``, unclipped."""
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT start, end FROM fetched
            WHERE user_id = ? AND mint = ? AND timeframe = ?
              AND start < ? AND end > ?
            ORDER BY start
            """,
            (user_id, mint, timeframe, end, start),
        )
        return [(row[0], row[1]) for row in cursor]

    def rejections(
        self,
        *,
        user_id: str,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
    ) -> list[BarRejection]:
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT mint, timeframe, open_time, reasons FROM rejections
            WHERE user_id = ? AND mint = ? AND timeframe = ?
              AND open_time >= ? AND open_time < ?
            ORDER BY open_time
            """,
            (user_id, mint, timeframe, start, end),
        )
        found: list[BarRejection] = []
        for row in cursor:
            reasons = json.loads(row[3])
            if not isinstance(reasons, list):
                raise StoreError("stored rejection reasons are not a list")
            found.append(
                BarRejection(row[0], row[1], row[2], tuple(str(reason) for reason in reasons))
            )
        return found

    def commit_page(
        self,
        bars: Sequence[Bar],
        rejections: Sequence[BarRejection],
        *,
        user_id: str,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
    ) -> None:
        """Upsert one page and record its fetched span in a single transaction.

        A failure before this returns leaves the previous pages and their
        fetched spans in place. This page is absent from both.
        """
        _owner(user_id)
        if start < 0 or end <= start:
            raise StoreError("fetched range is invalid")
        for bar in bars:
            if bar.mint != mint or bar.timeframe != timeframe:
                raise StoreError("page bar does not match the fetched series")
            if bar.open_time < start or bar.open_time >= end:
                raise StoreError("page bar is outside the fetched range")
        rows = [_row(user_id, bar) for bar in bars]
        stored_times = {bar.open_time for bar in bars}
        rejection_rows: list[tuple[object, ...]] = []
        for item in rejections:
            stamp = item.open_time
            if isinstance(stamp, bool) or not isinstance(stamp, int):
                continue
            if stamp < start or stamp >= end or stamp in stored_times:
                continue
            if item.mint != mint or item.timeframe != timeframe:
                raise StoreError("page rejection does not match the fetched series")
            rejection_rows.append(
                (user_id, mint, timeframe, stamp, json.dumps(list(item.reasons)))
            )
        with self._conn:
            if rows:
                self._conn.executemany(_UPSERT, rows)
                self._conn.executemany(
                    """
                    DELETE FROM rejections
                    WHERE user_id = ? AND mint = ? AND timeframe = ? AND open_time = ?
                    """,
                    [(user_id, mint, timeframe, bar.open_time) for bar in bars],
                )
            if rejection_rows:
                self._conn.executemany(
                    """
                    INSERT INTO rejections (
                        user_id, mint, timeframe, open_time, reasons
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, mint, timeframe, open_time) DO UPDATE SET
                        reasons = excluded.reasons
                    """,
                    rejection_rows,
                )
            _merge_fetched(self._conn, user_id, mint, timeframe, start, end)


def _merge_fetched(
    conn: sqlite3.Connection,
    user_id: str,
    mint: str,
    timeframe: str,
    start: int,
    end: int,
) -> None:
    """Fold ``[start, end)`` into overlapping or abutting fetched spans."""
    rows = conn.execute(
        """
        SELECT start, end FROM fetched
        WHERE user_id = ? AND mint = ? AND timeframe = ?
          AND start <= ? AND end >= ?
        """,
        (user_id, mint, timeframe, end, start),
    ).fetchall()
    lo, hi = start, end
    for row in rows:
        lo = min(lo, int(row[0]))
        hi = max(hi, int(row[1]))
    conn.execute(
        """
        DELETE FROM fetched
        WHERE user_id = ? AND mint = ? AND timeframe = ?
          AND start <= ? AND end >= ?
        """,
        (user_id, mint, timeframe, hi, lo),
    )
    conn.execute(
        """
        INSERT INTO fetched (user_id, mint, timeframe, start, end)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, mint, timeframe, lo, hi),
    )


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
