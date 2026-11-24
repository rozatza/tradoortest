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
from collections.abc import Callable, Mapping, Sequence
from functools import wraps
from pathlib import Path
from typing import Protocol

from app.ohlcv.bars import Bar, BarRejection, validate_bar
from app.ohlcv.errors import IngestExpired, RegistrationRefused, StoreError
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

# Live-feed rows. Payloads, decisions, the bar log, conflicts, and errors
# are provider data or a copy of a close, so they use the same purge as the
# bars. ``live_cursors.state_json`` is kept blank: the scheduler rebuilds
# runner memory from bars and does not persist a price on the cursor.
# ``last_open_time`` and the registration stay so a restart does not decide
# a bar again and does not drop the strategy IR.
_LIVE_TABLES = """
CREATE TABLE IF NOT EXISTS live_payloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    mint TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    body TEXT NOT NULL,
    stored_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS live_registrations (
    user_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    document_json TEXT NOT NULL,
    bar_size TEXT NOT NULL,
    registered_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, strategy_id, version)
);
CREATE TABLE IF NOT EXISTS live_cursors (
    user_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    origin INTEGER NOT NULL,
    last_open_time INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    state_stored_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, strategy_id, version)
);
CREATE TABLE IF NOT EXISTS live_decisions (
    user_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    open_time INTEGER NOT NULL,
    decision_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    stored_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, strategy_id, version, open_time)
);
CREATE TABLE IF NOT EXISTS live_bar_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    open_time INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    stored_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS live_bar_log_once
ON live_bar_log (user_id, strategy_id, version, open_time, kind);
CREATE TABLE IF NOT EXISTS live_bar_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    mint TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    incoming_close TEXT NOT NULL,
    stored_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS live_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    open_time INTEGER NOT NULL,
    exc_type TEXT NOT NULL,
    message TEXT NOT NULL,
    stored_at INTEGER NOT NULL
);
"""

# Paper rows. Fills, orders, and the event log carry ``stored_at`` and are
# deleted by ``_purge``. The wallet, the position, and the cursor do not:
# a held long outlives the 24-hour fill log, and the cursor is an open time,
# not a price. See ``docs/paper.md``.
_PAPER_TABLES = """
CREATE TABLE IF NOT EXISTS paper_wallets (
    user_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    cash TEXT NOT NULL,
    initial_capital TEXT NOT NULL,
    PRIMARY KEY (user_id, strategy_id, version)
);
CREATE TABLE IF NOT EXISTS paper_positions (
    user_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    qty TEXT,
    open_cost TEXT,
    entry TEXT,
    basis TEXT,
    realized TEXT NOT NULL,
    trade_realized TEXT,
    take_profit TEXT,
    stop_loss TEXT,
    anchor TEXT,
    attached_at INTEGER,
    PRIMARY KEY (user_id, strategy_id, version, asset_id)
);
CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    open_time INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    placed_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    price TEXT,
    qty TEXT,
    notional TEXT,
    commission TEXT,
    leg TEXT NOT NULL,
    reason TEXT NOT NULL,
    stored_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    open_time INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    price TEXT NOT NULL,
    notional TEXT NOT NULL,
    commission TEXT NOT NULL,
    leg TEXT NOT NULL,
    limit_below_low INTEGER NOT NULL,
    stored_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    open_time INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    stored_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_cursors (
    user_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    last_open_time INTEGER NOT NULL,
    PRIMARY KEY (user_id, strategy_id, version)
);
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
    def upsert_live(
        self, bars: Sequence[Bar], *, user_id: str, provider: str
    ) -> list[Bar]:
        """Store closed bars. A decided bar is left as it was.

        Returns the bars that were not written because a registered strategy
        has already decided that open time and the incoming candle differs.
        The conflict row is purged with the rest of the cache.
        """
        self._purge()
        _owner(user_id)
        stored_at = int(self._now())
        kept: list[Bar] = []
        conflicts: list[Bar] = []
        with self._conn:
            for bar in bars:
                if self._decided_open(user_id, bar.timeframe, bar.open_time):
                    existing = self._conn.execute(
                        """
                        SELECT open, high, low, close, volume, synthetic FROM bars
                        WHERE user_id = ? AND mint = ? AND timeframe = ? AND open_time = ?
                        """,
                        (user_id, bar.mint, bar.timeframe, bar.open_time),
                    ).fetchone()
                    if existing is None or not _same_candle(existing, bar):
                        conflicts.append(bar)
                        continue
                kept.append(bar)
            if kept:
                self._conn.executemany(
                    _UPSERT, [_row(user_id, bar, stored_at) for bar in kept]
                )
            for bar in conflicts:
                self._conn.execute(
                    """
                    INSERT INTO live_bar_conflicts (
                        user_id, provider, mint, timeframe, open_time,
                        incoming_close, stored_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        provider,
                        bar.mint,
                        bar.timeframe,
                        bar.open_time,
                        _close_text(bar.close),
                        stored_at,
                    ),
                )
        return conflicts

    def _decided_open(self, user_id: str, timeframe: str, open_time: int) -> bool:
        """True when any registered version has decided this open time.

        The caller holds the store lock. This does not purge.
        """
        row = self._conn.execute(
            """
            SELECT 1 FROM live_decisions AS decisions
            JOIN live_registrations AS registrations
              ON registrations.user_id = decisions.user_id
             AND registrations.strategy_id = decisions.strategy_id
             AND registrations.version = decisions.version
            WHERE decisions.user_id = ? AND registrations.bar_size = ?
              AND decisions.open_time = ?
            LIMIT 1
            """,
            (user_id, timeframe, open_time),
        ).fetchone()
        return row is not None

    @_serialized
    def replace_live_cursor(
        self,
        *,
        user_id: str,
        strategy_id: str,
        version: int,
        origin: int,
        last_open_time: int,
        state_json: str,
    ) -> None:
        """Set the cursor after memory was purged.

        ``last_open_time`` never moves backward. A blank ``state_json`` is
        stored with ``state_stored_at`` 0 so the next purge keeps it blank
        until a new decision writes memory.
        """
        self._purge()
        _owner(user_id)
        blank = state_json.strip() in {"", "null", "[]"}
        stored_at = 0 if blank else int(self._now())
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO live_cursors (
                    user_id, strategy_id, version, origin, last_open_time,
                    state_json, state_stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, strategy_id, version) DO UPDATE SET
                    origin = excluded.origin,
                    last_open_time = CASE
                        WHEN excluded.last_open_time > live_cursors.last_open_time
                        THEN excluded.last_open_time
                        ELSE live_cursors.last_open_time
                    END,
                    state_json = excluded.state_json,
                    state_stored_at = excluded.state_stored_at
                """,
                (
                    user_id,
                    strategy_id,
                    version,
                    origin,
                    last_open_time,
                    state_json,
                    stored_at,
                ),
            )

    @_serialized
    def live_bar_conflicts(self, *, user_id: str) -> list[tuple[int, str, str, str, str]]:
        """``(open_time, provider, mint, timeframe, incoming_close)``."""
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT open_time, provider, mint, timeframe, incoming_close
            FROM live_bar_conflicts
            WHERE user_id = ?
            ORDER BY id
            """,
            (user_id,),
        )
        return [(int(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4])) for row in cursor]

    @_serialized
    def log_live_error(
        self,
        *,
        user_id: str,
        strategy_id: str,
        version: int,
        open_time: int,
        exc_type: str,
        message: str,
    ) -> None:
        """Record one scheduler failure. The message is already shortened."""
        self._purge()
        _owner(user_id)
        stored_at = int(self._now())
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO live_errors (
                    user_id, strategy_id, version, open_time, exc_type, message, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, strategy_id, version, open_time, exc_type, message, stored_at),
            )

    @_serialized
    def live_errors(
        self, *, user_id: str, strategy_id: str, version: int
    ) -> list[tuple[int, str, str]]:
        """``(open_time, exc_type, message)`` in insert order."""
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT open_time, exc_type, message FROM live_errors
            WHERE user_id = ? AND strategy_id = ? AND version = ?
            ORDER BY id
            """,
            (user_id, strategy_id, version),
        )
        return [(int(row[0]), str(row[1]), str(row[2])) for row in cursor]

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
        """Remember the pool chosen for ``mint``. The row expires with the bars.

        Saving the same ``resolved_at`` again does not move ``stored_at``, so
        a repeat request cannot keep the pool past 24 hours from resolution.
        """
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
                    stored_at = CASE
                        WHEN excluded.resolved_at <> pool_resolutions.resolved_at
                        THEN excluded.stored_at
                        ELSE pool_resolutions.stored_at
                    END
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
    def put_live_payloads(
        self, user_id: str, rows: Sequence[tuple[str, str, str, str]]
    ) -> None:
        """Store raw response bodies. ``rows`` is provider, mint, timeframe, body."""
        self._purge()
        _owner(user_id)
        if not rows:
            return
        stored_at = int(self._now())
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO live_payloads (
                    user_id, provider, mint, timeframe, body, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (user_id, provider, mint, timeframe, body, stored_at)
                    for provider, mint, timeframe, body in rows
                ],
            )

    @_serialized
    def live_payload_bodies(self, *, user_id: str) -> list[str]:
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            "SELECT body FROM live_payloads WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        return [str(row[0]) for row in cursor]

    @_serialized
    def save_live_registration(
        self,
        *,
        user_id: str,
        strategy_id: str,
        version: int,
        document_json: str,
        bar_size: str,
        limit: int | None = None,
    ) -> tuple[int, bool]:
        """Insert a version. The same document is a no-op. A different one is refused.

        Returns ``(registered_at, created)``. ``limit`` is the running-paper
        cap for this user. ``None`` does not cap, which is what the
        scheduler helper uses. A new row at the cap raises
        ``RegistrationRefused``. The same document does not consume a slot.
        """
        self._purge()
        _owner(user_id)
        with self._conn:
            row = self._conn.execute(
                """
                SELECT document_json, registered_at FROM live_registrations
                WHERE user_id = ? AND strategy_id = ? AND version = ?
                """,
                (user_id, strategy_id, version),
            ).fetchone()
            if row is not None:
                if str(row[0]) == document_json:
                    return int(row[1]), False
                raise StoreError("strategy version is already registered")
            if limit is not None:
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM live_registrations WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if int(count[0]) >= limit:
                    raise RegistrationRefused("limit")
            registered_at = int(self._now())
            self._conn.execute(
                """
                INSERT INTO live_registrations (
                    user_id, strategy_id, version, document_json, bar_size, registered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, strategy_id, version, document_json, bar_size, registered_at),
            )
        return registered_at, True

    @_serialized
    def live_registrations(self, *, user_id: str) -> list[tuple[str, int, str, str]]:
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT strategy_id, version, document_json, bar_size
            FROM live_registrations
            WHERE user_id = ?
            ORDER BY strategy_id, version
            """,
            (user_id,),
        )
        return [(str(row[0]), int(row[1]), str(row[2]), str(row[3])) for row in cursor]

    @_serialized
    def live_registered_at(
        self, *, user_id: str, strategy_id: str, version: int
    ) -> int | None:
        """Unix seconds when this version was first registered. None if it is not."""
        self._purge()
        _owner(user_id)
        row = self._conn.execute(
            """
            SELECT registered_at FROM live_registrations
            WHERE user_id = ? AND strategy_id = ? AND version = ?
            """,
            (user_id, strategy_id, version),
        ).fetchone()
        if row is None:
            return None
        return int(row[0])

    @_serialized
    def live_cursor(
        self, *, user_id: str, strategy_id: str, version: int
    ) -> tuple[int, int, str] | None:
        """``(origin, last_open_time, state_json)`` or None when nothing is committed."""
        self._purge()
        _owner(user_id)
        row = self._conn.execute(
            """
            SELECT origin, last_open_time, state_json FROM live_cursors
            WHERE user_id = ? AND strategy_id = ? AND version = ?
            """,
            (user_id, strategy_id, version),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), int(row[1]), str(row[2])

    @_serialized
    def commit_live_step(
        self,
        *,
        user_id: str,
        strategy_id: str,
        version: int,
        origin: int,
        open_time: int,
        state_json: str,
        decision_json: str,
        log_kind: str | None = None,
        log_detail: str = "",
    ) -> bool:
        """Record one decision and move the cursor. False when that bar was already recorded."""
        self._purge()
        _owner(user_id)
        stored_at = int(self._now())
        with self._conn:
            existing = self._conn.execute(
                """
                SELECT state_json FROM live_decisions
                WHERE user_id = ? AND strategy_id = ? AND version = ? AND open_time = ?
                """,
                (user_id, strategy_id, version, open_time),
            ).fetchone()
            if existing is not None:
                self._advance_live_cursor(
                    user_id,
                    strategy_id,
                    version,
                    origin,
                    open_time,
                    "",
                    stored_at,
                )
                if log_kind:
                    self._insert_live_log(
                        user_id,
                        strategy_id,
                        version,
                        open_time,
                        log_kind,
                        log_detail,
                        stored_at,
                    )
                return False
            self._conn.execute(
                """
                INSERT INTO live_decisions (
                    user_id, strategy_id, version, open_time,
                    decision_json, state_json, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    strategy_id,
                    version,
                    open_time,
                    decision_json,
                    state_json,
                    stored_at,
                ),
            )
            self._advance_live_cursor(
                user_id, strategy_id, version, origin, open_time, state_json, stored_at
            )
            if log_kind:
                self._insert_live_log(
                    user_id, strategy_id, version, open_time, log_kind, log_detail, stored_at
                )
        return True

    @_serialized
    def commit_live_skip(
        self,
        *,
        user_id: str,
        strategy_id: str,
        version: int,
        origin: int,
        open_time: int,
        state_json: str,
        log_kind: str,
        log_detail: str,
    ) -> None:
        """Move the cursor past a slot that was not decided, and log why."""
        self._purge()
        _owner(user_id)
        stored_at = int(self._now())
        with self._conn:
            self._advance_live_cursor(
                user_id, strategy_id, version, origin, open_time, state_json, stored_at
            )
            self._insert_live_log(
                user_id, strategy_id, version, open_time, log_kind, log_detail, stored_at
            )

    @_serialized
    def has_live_decision(
        self, *, user_id: str, strategy_id: str, version: int, open_time: int
    ) -> bool:
        self._purge()
        _owner(user_id)
        row = self._conn.execute(
            """
            SELECT 1 FROM live_decisions
            WHERE user_id = ? AND strategy_id = ? AND version = ? AND open_time = ?
            """,
            (user_id, strategy_id, version, open_time),
        ).fetchone()
        return row is not None

    @_serialized
    def live_decision_times(
        self, *, user_id: str, strategy_id: str, version: int
    ) -> set[int]:
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT open_time FROM live_decisions
            WHERE user_id = ? AND strategy_id = ? AND version = ?
            """,
            (user_id, strategy_id, version),
        )
        return {int(row[0]) for row in cursor}

    @_serialized
    def live_decisions(
        self, *, user_id: str, strategy_id: str, version: int
    ) -> list[tuple[int, str]]:
        """``(open_time, decision_json)`` in open-time order."""
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT open_time, decision_json FROM live_decisions
            WHERE user_id = ? AND strategy_id = ? AND version = ?
            ORDER BY open_time
            """,
            (user_id, strategy_id, version),
        )
        return [(int(row[0]), str(row[1])) for row in cursor]

    @_serialized
    def log_live_bar(
        self,
        *,
        user_id: str,
        strategy_id: str,
        version: int,
        open_time: int,
        kind: str,
        detail: str,
    ) -> None:
        self._purge()
        _owner(user_id)
        stored_at = int(self._now())
        with self._conn:
            self._insert_live_log(
                user_id, strategy_id, version, open_time, kind, detail, stored_at
            )

    @_serialized
    def live_bar_logged(
        self,
        *,
        user_id: str,
        strategy_id: str,
        version: int,
        open_time: int,
        kind: str,
    ) -> bool:
        self._purge()
        _owner(user_id)
        row = self._conn.execute(
            """
            SELECT 1 FROM live_bar_log
            WHERE user_id = ? AND strategy_id = ? AND version = ?
              AND open_time = ? AND kind = ?
            """,
            (user_id, strategy_id, version, open_time, kind),
        ).fetchone()
        return row is not None

    @_serialized
    def live_bar_log(
        self, *, user_id: str, strategy_id: str, version: int
    ) -> list[tuple[int, str, str]]:
        """``(open_time, kind, detail)`` in insert order."""
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT open_time, kind, detail FROM live_bar_log
            WHERE user_id = ? AND strategy_id = ? AND version = ?
            ORDER BY id
            """,
            (user_id, strategy_id, version),
        )
        return [(int(row[0]), str(row[1]), str(row[2])) for row in cursor]

    def _advance_live_cursor(
        self,
        user_id: str,
        strategy_id: str,
        version: int,
        origin: int,
        open_time: int,
        state_json: str,
        state_stored_at: int,
    ) -> None:
        """Move the cursor forward. The caller holds the transaction.

        ``origin`` stays at the first insert. A later call does not move it
        backward or replace it. ``state_stored_at`` is the clock for the
        memory purge.
        """
        self._conn.execute(
            """
            INSERT INTO live_cursors (
                user_id, strategy_id, version, origin, last_open_time,
                state_json, state_stored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, strategy_id, version) DO UPDATE SET
                last_open_time = CASE
                    WHEN excluded.last_open_time > live_cursors.last_open_time
                    THEN excluded.last_open_time
                    ELSE live_cursors.last_open_time
                END,
                state_json = CASE
                    WHEN excluded.last_open_time > live_cursors.last_open_time
                    THEN excluded.state_json
                    ELSE live_cursors.state_json
                END,
                state_stored_at = CASE
                    WHEN excluded.last_open_time > live_cursors.last_open_time
                    THEN excluded.state_stored_at
                    ELSE live_cursors.state_stored_at
                END
            """,
            (user_id, strategy_id, version, origin, open_time, state_json, state_stored_at),
        )

    def _insert_live_log(
        self,
        user_id: str,
        strategy_id: str,
        version: int,
        open_time: int,
        kind: str,
        detail: str,
        stored_at: int,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO live_bar_log (
                user_id, strategy_id, version, open_time, kind, detail, stored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, strategy_id, version, open_time, kind, detail, stored_at),
        )

    @_serialized
    def paper_wallet(
        self, *, user_id: str, strategy_id: str, version: int
    ) -> tuple[str, str] | None:
        """``(cash, initial_capital)`` decimal strings, or None before the first tick."""
        self._purge()
        _owner(user_id)
        row = self._conn.execute(
            """
            SELECT cash, initial_capital FROM paper_wallets
            WHERE user_id = ? AND strategy_id = ? AND version = ?
            """,
            (user_id, strategy_id, version),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1])

    @_serialized
    def paper_positions(
        self, *, user_id: str, strategy_id: str, version: int
    ) -> list[tuple[object, ...]]:
        """Position rows, open and flat. Nulls stay null. Not rebuilt from fills."""
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT asset_id, qty, open_cost, entry, basis, realized, trade_realized,
                   take_profit, stop_loss, anchor, attached_at
            FROM paper_positions
            WHERE user_id = ? AND strategy_id = ? AND version = ?
            ORDER BY asset_id
            """,
            (user_id, strategy_id, version),
        )
        rows: list[tuple[object, ...]] = []
        for row in cursor:
            attached = None if row[10] is None else int(row[10])
            rows.append(
                (
                    str(row[0]),
                    None if row[1] is None else str(row[1]),
                    None if row[2] is None else str(row[2]),
                    None if row[3] is None else str(row[3]),
                    None if row[4] is None else str(row[4]),
                    str(row[5]),
                    None if row[6] is None else str(row[6]),
                    None if row[7] is None else str(row[7]),
                    None if row[8] is None else str(row[8]),
                    None if row[9] is None else str(row[9]),
                    attached,
                )
            )
        return rows

    @_serialized
    def paper_cursor(
        self, *, user_id: str, strategy_id: str, version: int
    ) -> int | None:
        """Last paper open time. This is not a price and it is not purged."""
        self._purge()
        _owner(user_id)
        row = self._conn.execute(
            """
            SELECT last_open_time FROM paper_cursors
            WHERE user_id = ? AND strategy_id = ? AND version = ?
            """,
            (user_id, strategy_id, version),
        ).fetchone()
        if row is None:
            return None
        return int(row[0])

    @_serialized
    def paper_fills(
        self, *, user_id: str, strategy_id: str, version: int
    ) -> list[tuple[object, ...]]:
        """Fills still inside the TTL, in insert order."""
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT open_time, asset_id, side, qty, price, notional, commission,
                   leg, limit_below_low
            FROM paper_fills
            WHERE user_id = ? AND strategy_id = ? AND version = ?
            ORDER BY id
            """,
            (user_id, strategy_id, version),
        )
        return [
            (
                int(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
                str(row[7]),
                bool(row[8]),
            )
            for row in cursor
        ]

    @_serialized
    def paper_orders(
        self, *, user_id: str, strategy_id: str, version: int
    ) -> list[tuple[object, ...]]:
        """Orders still inside the TTL, in insert order."""
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT open_time, asset_id, side, status, placed_at, expires_at,
                   price, qty, notional, commission, leg, reason
            FROM paper_orders
            WHERE user_id = ? AND strategy_id = ? AND version = ?
            ORDER BY id
            """,
            (user_id, strategy_id, version),
        )
        return [tuple(None if value is None else value for value in row) for row in cursor]

    @_serialized
    def paper_events(
        self, *, user_id: str, strategy_id: str, version: int
    ) -> list[tuple[int, str, str]]:
        """``(open_time, kind, detail)`` still inside the TTL, in insert order."""
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT open_time, kind, detail FROM paper_events
            WHERE user_id = ? AND strategy_id = ? AND version = ?
            ORDER BY id
            """,
            (user_id, strategy_id, version),
        )
        return [(int(row[0]), str(row[1]), str(row[2])) for row in cursor]

    @_serialized
    def paper_open_orders(
        self, *, user_id: str, strategy_id: str, version: int
    ) -> list[tuple[int, int, str, int]]:
        """``(id, open_time, asset_id, expires_at)`` for status ``open``."""
        self._purge()
        _owner(user_id)
        cursor = self._conn.execute(
            """
            SELECT id, open_time, asset_id, expires_at FROM paper_orders
            WHERE user_id = ? AND strategy_id = ? AND version = ? AND status = 'open'
            ORDER BY id
            """,
            (user_id, strategy_id, version),
        )
        return [(int(row[0]), int(row[1]), str(row[2]), int(row[3])) for row in cursor]

    @_serialized
    def undeploy_paper(
        self,
        *,
        user_id: str,
        strategy_id: str,
        version: int,
    ) -> tuple[int, str] | None:
        """Stop evaluation for one registered version.

        Deletes the registration, sets open orders to ``canceled``, and
        appends ``ORDER_CANCELED`` plus ``STRATEGY_UNDEPLOYED``. The wallet,
        the position rows, the paper cursor, and the fills are not written.
        No fill row is inserted. ``None`` means the version is not registered.
        The pair is ``(orders_canceled, position_state)``. ``position_state``
        is ``open_unmanaged`` when any qty is still set, otherwise ``flat``.
        """
        self._purge()
        _owner(user_id)
        stored_at = int(self._now())
        with self._conn:
            found = self._conn.execute(
                """
                SELECT 1 FROM live_registrations
                WHERE user_id = ? AND strategy_id = ? AND version = ?
                """,
                (user_id, strategy_id, version),
            ).fetchone()
            if found is None:
                return None
            open_orders = self._conn.execute(
                """
                SELECT id, open_time, asset_id FROM paper_orders
                WHERE user_id = ? AND strategy_id = ? AND version = ? AND status = 'open'
                ORDER BY id
                """,
                (user_id, strategy_id, version),
            ).fetchall()
            open_qty = self._conn.execute(
                """
                SELECT 1 FROM paper_positions
                WHERE user_id = ? AND strategy_id = ? AND version = ?
                  AND qty IS NOT NULL
                LIMIT 1
                """,
                (user_id, strategy_id, version),
            ).fetchone()
            position_state = "open_unmanaged" if open_qty is not None else "flat"
            cursor = self._conn.execute(
                """
                SELECT last_open_time FROM paper_cursors
                WHERE user_id = ? AND strategy_id = ? AND version = ?
                """,
                (user_id, strategy_id, version),
            ).fetchone()
            marked_at = 0 if cursor is None else int(cursor[0])
            for order_id, open_time, asset_id in open_orders:
                self._conn.execute(
                    """
                    UPDATE paper_orders SET status = 'canceled'
                    WHERE id = ? AND user_id = ? AND status = 'open'
                    """,
                    (int(order_id), user_id),
                )
                self._conn.execute(
                    """
                    INSERT INTO paper_events (
                        user_id, strategy_id, version, open_time, kind, detail, stored_at
                    ) VALUES (?, ?, ?, ?, 'ORDER_CANCELED', ?, ?)
                    """,
                    (
                        user_id,
                        strategy_id,
                        version,
                        int(open_time),
                        _paper_detail({"asset_id": str(asset_id), "reason": "undeploy"}),
                        stored_at,
                    ),
                )
            self._conn.execute(
                """
                INSERT INTO paper_events (
                    user_id, strategy_id, version, open_time, kind, detail, stored_at
                ) VALUES (?, ?, ?, ?, 'STRATEGY_UNDEPLOYED', ?, ?)
                """,
                (
                    user_id,
                    strategy_id,
                    version,
                    marked_at,
                    _paper_detail(
                        {
                            "orders_canceled": len(open_orders),
                            "position_state": position_state,
                        }
                    ),
                    stored_at,
                ),
            )
            self._conn.execute(
                """
                DELETE FROM live_registrations
                WHERE user_id = ? AND strategy_id = ? AND version = ?
                """,
                (user_id, strategy_id, version),
            )
        return len(open_orders), position_state

    @_serialized
    def insert_paper_order(
        self,
        *,
        user_id: str,
        strategy_id: str,
        version: int,
        open_time: int,
        asset_id: str,
        side: str,
        status: str,
        placed_at: int,
        expires_at: int,
        leg: str,
        reason: str,
    ) -> None:
        """Insert one order row. The paper tick is what fills or expires it."""
        self._purge()
        _owner(user_id)
        stored_at = int(self._now())
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO paper_orders (
                    user_id, strategy_id, version, open_time, asset_id, side, status,
                    placed_at, expires_at, price, qty, notional, commission, leg,
                    reason, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?)
                """,
                (
                    user_id,
                    strategy_id,
                    version,
                    open_time,
                    asset_id,
                    side,
                    status,
                    placed_at,
                    expires_at,
                    leg,
                    reason,
                    stored_at,
                ),
            )

    @_serialized
    def commit_paper_step(
        self,
        *,
        user_id: str,
        strategy_id: str,
        version: int,
        cash: str,
        initial_capital: str,
        positions: Sequence[Mapping[str, object]],
        orders: Sequence[Mapping[str, object]],
        fills: Sequence[Mapping[str, object]],
        events: Sequence[Mapping[str, object]],
        cursor_open_time: int,
        expire_order_ids: Sequence[int] = (),
    ) -> None:
        """Write one bar's book, log, and cursor in one transaction.

        The wallet and position rows have no ``stored_at``. Fills, orders, and
        events do, so ``_purge`` can drop them without touching the book.
        """
        self._purge()
        _owner(user_id)
        stored_at = int(self._now())
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO paper_wallets (
                    user_id, strategy_id, version, cash, initial_capital
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, strategy_id, version) DO UPDATE SET
                    cash = excluded.cash
                """,
                (user_id, strategy_id, version, cash, initial_capital),
            )
            # A flat row must not land after the open row for the same asset.
            # The snapshot already emits one row; this keeps a later writer
            # from nulling a long that was re-opened on the same bar.
            kept: dict[str, Mapping[str, object]] = {}
            for position in positions:
                asset_id = str(position["asset_id"])
                current = kept.get(asset_id)
                flat_after_open = (
                    current is not None
                    and current["qty"] is not None
                    and position["qty"] is None
                )
                if flat_after_open:
                    continue
                kept[asset_id] = position
            for position in kept.values():
                self._conn.execute(
                    """
                    INSERT INTO paper_positions (
                        user_id, strategy_id, version, asset_id, qty, open_cost, entry,
                        basis, realized, trade_realized, take_profit, stop_loss,
                        anchor, attached_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, strategy_id, version, asset_id) DO UPDATE SET
                        qty = excluded.qty,
                        open_cost = excluded.open_cost,
                        entry = excluded.entry,
                        basis = excluded.basis,
                        realized = excluded.realized,
                        trade_realized = excluded.trade_realized,
                        take_profit = excluded.take_profit,
                        stop_loss = excluded.stop_loss,
                        anchor = excluded.anchor,
                        attached_at = excluded.attached_at
                    """,
                    (
                        user_id,
                        strategy_id,
                        version,
                        position["asset_id"],
                        position["qty"],
                        position["open_cost"],
                        position["entry"],
                        position["basis"],
                        position["realized"],
                        position["trade_realized"],
                        position["take_profit"],
                        position["stop_loss"],
                        position["anchor"],
                        position["attached_at"],
                    ),
                )
            for order_id in expire_order_ids:
                self._conn.execute(
                    """
                    UPDATE paper_orders SET status = 'expired'
                    WHERE id = ? AND user_id = ? AND strategy_id = ? AND version = ?
                      AND status = 'open'
                    """,
                    (order_id, user_id, strategy_id, version),
                )
            for order in orders:
                self._conn.execute(
                    """
                    INSERT INTO paper_orders (
                        user_id, strategy_id, version, open_time, asset_id, side, status,
                        placed_at, expires_at, price, qty, notional, commission, leg,
                        reason, stored_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        strategy_id,
                        version,
                        order["open_time"],
                        order["asset_id"],
                        order["side"],
                        order["status"],
                        order["placed_at"],
                        order["expires_at"],
                        order["price"],
                        order["qty"],
                        order["notional"],
                        order["commission"],
                        order["leg"],
                        order["reason"],
                        stored_at,
                    ),
                )
            for fill in fills:
                self._conn.execute(
                    """
                    INSERT INTO paper_fills (
                        user_id, strategy_id, version, open_time, asset_id, side,
                        qty, price, notional, commission, leg, limit_below_low, stored_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        strategy_id,
                        version,
                        fill["open_time"],
                        fill["asset_id"],
                        fill["side"],
                        fill["qty"],
                        fill["price"],
                        fill["notional"],
                        fill["commission"],
                        fill["leg"],
                        1 if fill["limit_below_low"] else 0,
                        stored_at,
                    ),
                )
            for event in events:
                self._conn.execute(
                    """
                    INSERT INTO paper_events (
                        user_id, strategy_id, version, open_time, kind, detail, stored_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        strategy_id,
                        version,
                        event["open_time"],
                        event["kind"],
                        event["detail"],
                        stored_at,
                    ),
                )
            self._conn.execute(
                """
                INSERT INTO paper_cursors (
                    user_id, strategy_id, version, last_open_time
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, strategy_id, version) DO UPDATE SET
                    last_open_time = excluded.last_open_time
                """,
                (user_id, strategy_id, version, cursor_open_time),
            )

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
        self._conn.executescript(_LIVE_TABLES)
        self._conn.executescript(_PAPER_TABLES)
        reg_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(live_registrations)")}
        if reg_cols and "registered_at" not in reg_cols:
            self._conn.execute(
                "ALTER TABLE live_registrations ADD COLUMN registered_at INTEGER NOT NULL DEFAULT 0"
            )
        cursor_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(live_cursors)")}
        if cursor_cols and "state_stored_at" not in cursor_cols:
            self._conn.execute(
                "ALTER TABLE live_cursors ADD COLUMN state_stored_at INTEGER NOT NULL DEFAULT 0"
            )
        self._conn.commit()

    @_serialized
    def _purge(self) -> None:
        """Drop rows and fetched spans that have reached the TTL."""
        cutoff = int(self._now()) - self._ttl
        with self._conn:
            self._conn.execute("DELETE FROM bars WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM fetched WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM rejections WHERE stored_at <= ?", (cutoff,))
            self._conn.execute(
                "DELETE FROM pool_resolutions WHERE stored_at <= ? OR resolved_at <= ?",
                (cutoff, cutoff),
            )
            self._conn.execute("DELETE FROM ingest_results WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM live_payloads WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM live_decisions WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM live_bar_log WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM live_bar_conflicts WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM live_errors WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM paper_orders WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM paper_fills WHERE stored_at <= ?", (cutoff,))
            self._conn.execute("DELETE FROM paper_events WHERE stored_at <= ?", (cutoff,))
            self._conn.execute(
                "UPDATE live_cursors SET state_json = '' WHERE state_stored_at <= ?",
                (cutoff,),
            )

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


def _paper_detail(body: Mapping[str, object]) -> str:
    """Stable event JSON. No price fields belong in an undeploy event."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


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


def _close_text(value: float) -> str:
    """Short decimal text. Trailing zeros are not a second copy of the price."""
    text = format(value, "f").rstrip("0").rstrip(".")
    return text or "0"


def _same_candle(row: tuple[object, ...], bar: Bar) -> bool:
    """True when the stored candle and ``bar`` have the same OHLC."""
    return (
        row[0] == bar.open
        and row[1] == bar.high
        and row[2] == bar.low
        and row[3] == bar.close
        and row[4] == bar.volume
        and bool(row[5]) == bar.synthetic
    )
