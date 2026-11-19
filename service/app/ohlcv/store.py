"""Local bar cache.

SQLite is the cache that ships. ``BarStore`` is the seam a later licensed
store would fill. No Postgres driver is installed, and nothing here reads
a database URL.

This is a TTL cache, not a long-term store. CoinGecko's API terms do not
allow keeping the response. Rows and fetched-span records older than the
TTL are deleted on open, read, and write. The TTL cannot exceed 24 hours.

Rows carry ``user_id`` so a later session can filter on the auth seam.
This process still serves one user. There is no user list and no sharing.

The file lives under the gitignored ``data/`` directory. Bar rows are
never written into the repository.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from app.ohlcv.bars import Bar, BarRejection, validate_bar
from app.ohlcv.errors import StoreError
from app.ohlcv.synthetic import synthetic_minutes

# CoinGecko API terms §6: do not keep a persistent copy. 24h is the cap.
MAX_CACHE_TTL_SECONDS = 24 * 60 * 60

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
    synthetic INTEGER NOT NULL,
    stored_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, mint, timeframe, open_time)
);
CREATE TABLE IF NOT EXISTS fetched (
    user_id TEXT NOT NULL,
    mint TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    stored_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, mint, timeframe, start, end)
);
CREATE TABLE IF NOT EXISTS rejections (
    user_id TEXT NOT NULL,
    mint TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    stored_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, mint, timeframe, open_time)
)
"""

_UPSERT = """
INSERT INTO bars (
    user_id, mint, timeframe, open_time, open, high, low, close, volume,
    synthetic, stored_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(user_id, mint, timeframe, open_time) DO UPDATE SET
    open = excluded.open,
    high = excluded.high,
    low = excluded.low,
    close = excluded.close,
    volume = excluded.volume,
    synthetic = excluded.synthetic,
    stored_at = excluded.stored_at
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
        window_start: int,
        window_end: int,
    ) -> None: ...


class SqliteBarStore:
    """One SQLite file. Upserts are idempotent on the primary key."""

    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: int = MAX_CACHE_TTL_SECONDS,
        now: Callable[[], int] | None = None,
    ) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise ValueError("cache TTL must be a whole number of seconds")
        if ttl_seconds < 1 or ttl_seconds > MAX_CACHE_TTL_SECONDS:
            raise ValueError(
                "cache TTL must be from 1 second to 24 hours; "
                "the CoinGecko API terms do not allow a longer store"
            )
        self.path = path
        self._ttl = ttl_seconds
        self._now = (lambda: int(time.time())) if now is None else now
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._ensure_schema()
        self._purge()

    def close(self) -> None:
        self._conn.close()

    def upsert(self, bars: Sequence[Bar], *, user_id: str) -> int:
        self._purge()
        stored_at = int(self._now())
        rows = [_row(user_id, bar, stored_at) for bar in bars]
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
        """Replace every stored bar of this series inside ``[start, end)``."""
        self._purge()
        _owner(user_id)
        for bar in bars:
            if bar.mint != mint or bar.timeframe != timeframe:
                raise StoreError("replace_window bars must match the requested series")
            if bar.open_time < start or bar.open_time >= end:
                raise StoreError("replace_window bars must fall inside the window")
        stored_at = int(self._now())
        rows = [_row(user_id, bar, stored_at) for bar in bars]
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
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT mint, timeframe, open_time, open, high, low, close, volume, synthetic
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
                synthetic=bool(row[8]),
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
        self._purge()
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
        """Fresh half-open spans that overlap ``[start, end)``, unclipped."""
        self._purge()
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
        self._purge()
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
        window_start: int,
        window_end: int,
    ) -> None:
        """Upsert one page and record its fetched span in a single transaction.

        A failure before this returns leaves the previous pages and their
        fetched spans in place. This page is absent from both. The span is
        stored on its own row with this write's clock, so merging it with an
        older span cannot extend that older span's TTL.

        For 1m, the same transaction rewrites flagged synthetic bars inside
        ``[window_start, window_end)``.
        """
        self._purge()
        _owner(user_id)
        if start < 0 or end <= start:
            raise StoreError("fetched range is invalid")
        if window_end < window_start:
            raise StoreError("fetched window is invalid")
        for bar in bars:
            if bar.mint != mint or bar.timeframe != timeframe:
                raise StoreError("page bar does not match the fetched series")
            if bar.open_time < start or bar.open_time >= end:
                raise StoreError("page bar is outside the fetched range")
            if bar.synthetic:
                raise StoreError("a fetched page cannot contain a synthetic bar")
        stored_at = int(self._now())
        rows = [_row(user_id, bar, stored_at) for bar in bars]
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
                (user_id, mint, timeframe, stamp, json.dumps(list(item.reasons)), stored_at)
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
                        user_id, mint, timeframe, open_time, reasons, stored_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, mint, timeframe, open_time) DO UPDATE SET
                        reasons = excluded.reasons,
                        stored_at = excluded.stored_at
                    """,
                    rejection_rows,
                )
            self._conn.execute(
                """
                INSERT INTO fetched (user_id, mint, timeframe, start, end, stored_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, mint, timeframe, start, end) DO UPDATE SET
                    stored_at = excluded.stored_at
                """,
                (user_id, mint, timeframe, start, end, stored_at),
            )
            if timeframe == "1m":
                self._rewrite_synthetics(user_id, mint, window_start, window_end)

    def _ensure_schema(self) -> None:
        self._conn.executescript(_CREATE)
        bars = {row[1] for row in self._conn.execute("PRAGMA table_info(bars)")}
        fetched = {row[1] for row in self._conn.execute("PRAGMA table_info(fetched)")}
        rejections = {row[1] for row in self._conn.execute("PRAGMA table_info(rejections)")}
        fresh = {"synthetic", "stored_at"} <= bars and "stored_at" in fetched
        fresh = fresh and "stored_at" in rejections
        if fresh:
            self._conn.commit()
            return
        self._conn.executescript(
            """
            DROP TABLE IF EXISTS bars;
            DROP TABLE IF EXISTS fetched;
            DROP TABLE IF EXISTS rejections;
            """
        )
        self._conn.executescript(_CREATE)
        self._conn.commit()

    def _purge(self) -> None:
        """Drop rows and fetched spans that have reached the TTL."""
        cutoff = int(self._now()) - self._ttl
        with self._conn:
            self._conn.execute("DELETE FROM bars WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM fetched WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM rejections WHERE stored_at <= ?", (cutoff,))

    def _rewrite_synthetics(self, user_id: str, mint: str, start: int, end: int) -> None:
        """Replace 1m synthetic bars in the window. Caller holds the transaction."""
        ranges = self._conn.execute(
            """
            SELECT start, end FROM fetched
            WHERE user_id = ? AND mint = ? AND timeframe = '1m'
              AND start < ? AND end > ?
            """,
            (user_id, mint, end, start),
        ).fetchall()
        real_rows = self._conn.execute(
            """
            SELECT open_time, close, stored_at FROM bars
            WHERE user_id = ? AND mint = ? AND timeframe = '1m' AND synthetic = 0
              AND open_time >= ? AND open_time < ?
            """,
            (user_id, mint, start, end),
        ).fetchall()
        rejected_rows = self._conn.execute(
            """
            SELECT open_time FROM rejections
            WHERE user_id = ? AND mint = ? AND timeframe = '1m'
              AND open_time >= ? AND open_time < ?
            """,
            (user_id, mint, start, end),
        ).fetchall()
        self._conn.execute(
            """
            DELETE FROM bars
            WHERE user_id = ? AND mint = ? AND timeframe = '1m' AND synthetic = 1
              AND open_time >= ? AND open_time < ?
            """,
            (user_id, mint, start, end),
        )
        built = synthetic_minutes(
            mint=mint,
            real={int(row[0]): (float(row[1]), int(row[2])) for row in real_rows},
            rejected={int(row[0]) for row in rejected_rows},
            covered=[(int(row[0]), int(row[1])) for row in ranges],
            start=start,
            end=end,
        )
        if built:
            self._conn.executemany(
                _UPSERT,
                [_row(user_id, bar, stored_at) for bar, stored_at in built],
            )


def _owner(user_id: str) -> None:
    if not isinstance(user_id, str) or user_id == "":
        raise StoreError("user_id is required")


def _row(user_id: str, bar: Bar, stored_at: int) -> tuple[object, ...]:
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
        1 if bar.synthetic else 0,
        stored_at,
    )
