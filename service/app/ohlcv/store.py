"""Local bar cache.

SQLite is the cache that ships. ``BarStore`` is the seam a later licensed
store would fill. No Postgres driver is installed, and nothing here reads
a database URL.

This is a TTL cache, not a long-term store. CoinGecko's API terms do not
allow keeping the response. Rows and fetched-span records older than the
TTL are deleted on open, on every read and write, and on close. The TTL
cannot exceed 24 hours.

Rows carry ``user_id`` so a later session can filter on the auth seam.
This process still serves one user. There is no user list and no sharing.

The file lives under the gitignored ``data/`` directory. Bar rows are
never written into the repository.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from functools import wraps
from pathlib import Path
from typing import Protocol

from app.ohlcv.bars import Bar, BarRejection, validate_bar
from app.ohlcv.errors import IngestExpired, StoreError
from app.ohlcv.gaps import spans_touching
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
    empty INTEGER NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
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

# Pool choice and the ingest document are provider results. They use the
# same TTL and the same ``_purge`` as the bars. Birdeye rows are not kept
# longer than GeckoTerminal rows.
_PROVIDER_TABLES = """
CREATE TABLE IF NOT EXISTS pool_resolutions (
    user_id TEXT NOT NULL,
    mint TEXT NOT NULL,
    address TEXT NOT NULL,
    dex TEXT NOT NULL,
    dex_id TEXT NOT NULL,
    quote_token TEXT NOT NULL,
    quote_symbol TEXT,
    liquidity_usd TEXT NOT NULL,
    resolved_at INTEGER NOT NULL,
    pool_created_at INTEGER,
    provider TEXT NOT NULL,
    stored_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, mint)
);
CREATE TABLE IF NOT EXISTS ingest_results (
    user_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    stored_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, job_id)
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

    def empty_ranges(
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
        provider_empty: bool = False,
        seed: Bar | None = None,
    ) -> None: ...


def _serialized(method):
    """One store, many request threads. The connection is not safe to share."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


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
        # Request threads share this connection. ``_serialized`` holds the lock
        # for each call, and busy_timeout covers a writer in another process.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self._conn.execute("PRAGMA secure_delete=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema()
        self._purge()

    @_serialized
    def close(self) -> None:
        """Purge expired rows, then close the file.

        A process that exits through ``close`` does not leave expired bars
        for the next open. A kill that skips ``close`` is purged on the
        next open.
        """
        try:
            self._purge()
        finally:
            self._conn.close()

    @_serialized
    def upsert(self, bars: Sequence[Bar], *, user_id: str) -> int:
        self._purge()
        stored_at = int(self._now())
        rows = [_row(user_id, bar, stored_at) for bar in bars]
        with self._conn:
            self._conn.executemany(_UPSERT, rows)
        return len(rows)

    @_serialized
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

    @_serialized
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

    @_serialized
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

    @_serialized
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

    @_serialized
    def empty_ranges(
        self,
        *,
        user_id: str,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
    ) -> list[tuple[int, int]]:
        """Fetched spans whose provider returned no candles at all.

        A quiet page that returned older candles is not in this list. The
        range is not fetched again inside the TTL. It is a gap, not a
        no-trade run, and a prior close does not cross it.
        """
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT start, end FROM fetched
            WHERE user_id = ? AND mint = ? AND timeframe = ? AND empty = 1
              AND start < ? AND end > ?
            ORDER BY start
            """,
            (user_id, mint, timeframe, end, start),
        )
        return [(row[0], row[1]) for row in cursor]

    @_serialized
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

    @_serialized
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
        provider_empty: bool = False,
        seed: Bar | None = None,
        provider: str = "geckoterminal",
    ) -> None:
        """Upsert one page and record its fetched span in a single transaction.

        A failure before this returns leaves the previous pages and their
        fetched spans in place. This page is absent from both. The span is
        stored on its own row with this write's clock, so merging it with an
        older span cannot extend that older span's TTL.

        ``provider_empty`` is set only when the provider returned no candles
        at all. Zero bars inside the requested range is not enough: ``seed``
        is the latest older candle, the span starts at that candle, and the
        quiet minutes are synthetic. An empty page stays a gap, and a prior
        close does not cross it.

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
        span_start = start
        if seed is not None:
            if provider_empty:
                raise StoreError("an empty provider page cannot carry a prior close")
            if seed.mint != mint or seed.timeframe != timeframe or seed.synthetic:
                raise StoreError("prior close does not match the fetched series")
            if isinstance(seed.open_time, bool) or not isinstance(seed.open_time, int):
                raise StoreError("prior close must sit before the fetched page")
            if seed.open_time >= start:
                raise StoreError("prior close must sit before the fetched page")
            span_start = seed.open_time
        rejection_rows: list[tuple[object, ...]] = []
        for item in rejections:
            stamp = item.open_time
            if isinstance(stamp, bool) or not isinstance(stamp, int):
                continue
            # The span starts at the seed, so a bad candle between the seed
            # and this page is part of the span. Leaving it out makes the
            # synthetic rewrite copy the seed into that minute.
            if stamp < span_start or stamp >= end or stamp in stored_times:
                continue
            if seed is not None and stamp == seed.open_time:
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
            if seed is not None:
                self._conn.execute(_UPSERT, _row(user_id, seed, stored_at))
                self._conn.execute(
                    """
                    DELETE FROM rejections
                    WHERE user_id = ? AND mint = ? AND timeframe = ? AND open_time = ?
                    """,
                    (user_id, mint, timeframe, seed.open_time),
                )
            empty_page = 1 if provider_empty else 0
            self._conn.execute(
                """
                INSERT INTO fetched (
                    user_id, mint, timeframe, start, end, stored_at, empty, provider
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, mint, timeframe, start, end) DO UPDATE SET
                    stored_at = excluded.stored_at,
                    empty = excluded.empty,
                    provider = excluded.provider
                """,
                (user_id, mint, timeframe, span_start, end, stored_at, empty_page, provider),
            )
            if timeframe == "1m":
                self._rewrite_synthetics(user_id, mint, window_start, window_end)

    @_serialized
    def save_pool(
        self,
        *,
        user_id: str,
        mint: str,
        address: str,
        dex: str,
        dex_id: str,
        quote_token: str,
        quote_symbol: str | None,
        liquidity_usd: str,
        resolved_at: int,
        pool_created_at: int | None,
        provider: str,
    ) -> None:
        """Remember the pool chosen for ``mint``. The row expires with the bars."""
        self._purge()
        _owner(user_id)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO pool_resolutions (
                    user_id, mint, address, dex, dex_id, quote_token, quote_symbol,
                    liquidity_usd, resolved_at, pool_created_at, provider, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, mint) DO UPDATE SET
                    address = excluded.address,
                    dex = excluded.dex,
                    dex_id = excluded.dex_id,
                    quote_token = excluded.quote_token,
                    quote_symbol = excluded.quote_symbol,
                    liquidity_usd = excluded.liquidity_usd,
                    resolved_at = excluded.resolved_at,
                    pool_created_at = excluded.pool_created_at,
                    provider = excluded.provider,
                    stored_at = excluded.stored_at
                """,
                (
                    user_id,
                    mint,
                    address,
                    dex,
                    dex_id,
                    quote_token,
                    quote_symbol,
                    liquidity_usd,
                    resolved_at,
                    pool_created_at,
                    provider,
                    int(self._now()),
                ),
            )

    @_serialized
    def load_pool(self, *, user_id: str, mint: str) -> dict[str, object] | None:
        """The fresh pool row, or ``None`` after the TTL has deleted it."""
        self._purge()
        _owner(user_id)
        row = self._conn.execute(
            """
            SELECT address, dex, dex_id, quote_token, quote_symbol, liquidity_usd,
                   resolved_at, pool_created_at, provider
            FROM pool_resolutions
            WHERE user_id = ? AND mint = ?
            """,
            (user_id, mint),
        ).fetchone()
        if row is None:
            return None
        return {
            "address": row[0],
            "dex": row[1],
            "dex_id": row[2],
            "quote_token": row[3],
            "quote_symbol": row[4],
            "liquidity_usd": row[5],
            "resolved_at": row[6],
            "pool_created_at": row[7],
            "provider": row[8],
        }

    @_serialized
    def series_providers(
        self,
        *,
        user_id: str,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
    ) -> list[str]:
        """Providers recorded on fetched spans that overlap ``[start, end)``.

        One name when the window is a single provider. More than one when
        GeckoTerminal and Birdeye each covered part of it. The rows expire
        with the bars.
        """
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT DISTINCT provider FROM fetched
            WHERE user_id = ? AND mint = ? AND timeframe = ?
              AND start < ? AND end > ? AND provider != ''
            ORDER BY provider
            """,
            (user_id, mint, timeframe, end, start),
        )
        return [row[0] for row in cursor if isinstance(row[0], str)]

    @_serialized
    def save_ingest_result(self, *, user_id: str, job_id: str, payload: dict[str, object]) -> None:
        """Store the job result. It is deleted by the same purge as the bars."""
        self._purge()
        _owner(user_id)
        if not isinstance(job_id, str) or job_id == "":
            raise StoreError("job_id is required")
        text = json.dumps(payload)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO ingest_results (user_id, job_id, payload, stored_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, job_id) DO UPDATE SET
                    payload = excluded.payload,
                    stored_at = excluded.stored_at
                """,
                (user_id, job_id, text, int(self._now())),
            )

    @_serialized
    def read_ingest_result(self, *, user_id: str, job_id: str) -> dict[str, object] | None:
        """Return the result, or raise ``IngestExpired`` when this read deletes it.

        A missing id is ``None``. A row that has reached the TTL is deleted
        here, together with the other expired provider rows, and the caller
        reports that as gone.
        """
        _owner(user_id)
        row = self._conn.execute(
            "SELECT payload, stored_at FROM ingest_results WHERE user_id = ? AND job_id = ?",
            (user_id, job_id),
        ).fetchone()
        if row is None:
            self._purge()
            return None
        expired = int(row[1]) <= int(self._now()) - self._ttl
        self._purge()
        if expired:
            raise IngestExpired("the ingest result has expired")
        loaded = self._conn.execute(
            "SELECT payload FROM ingest_results WHERE user_id = ? AND job_id = ?",
            (user_id, job_id),
        ).fetchone()
        if loaded is None:
            return None
        payload = json.loads(loaded[0])
        if not isinstance(payload, dict):
            raise StoreError("stored ingest result is not an object")
        return payload

    @_serialized
    def _ensure_schema(self) -> None:
        self._conn.executescript(_CREATE)
        bars = {row[1] for row in self._conn.execute("PRAGMA table_info(bars)")}
        fetched = {row[1] for row in self._conn.execute("PRAGMA table_info(fetched)")}
        rejections = {row[1] for row in self._conn.execute("PRAGMA table_info(rejections)")}
        fresh = {"synthetic", "stored_at"} <= bars and {"stored_at", "empty", "provider"} <= fetched
        fresh = fresh and "stored_at" in rejections
        if not fresh:
            self._conn.executescript(
                """
                DROP TABLE IF EXISTS bars;
                DROP TABLE IF EXISTS fetched;
                DROP TABLE IF EXISTS rejections;
                """
            )
            self._conn.executescript(_CREATE)
        pool_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(pool_resolutions)")}
        if pool_cols and "dex_id" not in pool_cols:
            self._conn.execute("DROP TABLE pool_resolutions")
        self._conn.executescript(_PROVIDER_TABLES)
        self._conn.commit()

    @_serialized
    def _purge(self) -> None:
        """Drop rows and fetched spans that have reached the TTL."""
        cutoff = int(self._now()) - self._ttl
        with self._conn:
            self._conn.execute("DELETE FROM bars WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM fetched WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM rejections WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM pool_resolutions WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM ingest_results WHERE stored_at <= ?", (cutoff,))

    def _rewrite_synthetics(self, user_id: str, mint: str, start: int, end: int) -> None:
        """Rebuild 1m synthetic bars on every fetched span that touches the window.

        The rebuild covers the whole span, not only ``[start, end)``. A later
        window therefore still sees a real close stored before ``start``, and
        a long no-trade run is classified the same way no matter which window
        was requested last. An unfetched hole splits the span, so that close
        does not cross it. Caller holds the transaction.
        """
        ranges = self._conn.execute(
            """
            SELECT start, end, empty FROM fetched
            WHERE user_id = ? AND mint = ? AND timeframe = '1m'
            """,
            (user_id, mint),
        ).fetchall()
        populated = [(int(row[0]), int(row[1])) for row in ranges if not row[2]]
        holes = [(int(row[0]), int(row[1])) for row in ranges if row[2]]
        for lo, hi in holes:
            self._conn.execute(
                """
                DELETE FROM bars
                WHERE user_id = ? AND mint = ? AND timeframe = '1m' AND synthetic = 1
                  AND open_time >= ? AND open_time < ?
                """,
                (user_id, mint, lo, hi),
            )
        touching = spans_touching(populated, start, end)
        for lo, hi in touching:
            real_rows = self._conn.execute(
                """
                SELECT open_time, close, stored_at FROM bars
                WHERE user_id = ? AND mint = ? AND timeframe = '1m' AND synthetic = 0
                  AND open_time >= ? AND open_time < ?
                """,
                (user_id, mint, lo, hi),
            ).fetchall()
            rejected_rows = self._conn.execute(
                """
                SELECT open_time FROM rejections
                WHERE user_id = ? AND mint = ? AND timeframe = '1m'
                  AND open_time >= ? AND open_time < ?
                """,
                (user_id, mint, lo, hi),
            ).fetchall()
            self._conn.execute(
                """
                DELETE FROM bars
                WHERE user_id = ? AND mint = ? AND timeframe = '1m' AND synthetic = 1
                  AND open_time >= ? AND open_time < ?
                """,
                (user_id, mint, lo, hi),
            )
            built = synthetic_minutes(
                mint=mint,
                real={int(row[0]): (float(row[1]), int(row[2])) for row in real_rows},
                rejected={int(row[0]) for row in rejected_rows},
                covered=[(lo, hi)],
                start=lo,
                end=hi,
            )
            built = [
                item
                for item in built
                if not any(hole_lo <= item[0].open_time < hole_hi for hole_lo, hole_hi in holes)
            ]
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
