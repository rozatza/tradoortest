"""Thread-safe SQLite store for strategy versions.

One connection, one ``RLock``, and ``BEGIN IMMEDIATE`` for every write.
The next version is ``MAX(version) + 1`` inside that transaction, and
``(strategy_id, version)`` is the primary key.

Rows hold the owner, the DSL text, the canonical IR JSON, and ``ir_hash``.
There is no bar table and no column for prices.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.strategies.compile import CompiledStrategy
from app.strategies.diff import dsl_unified_diff, ir_structural_diff
from app.strategies.errors import StoreUnavailable, StrategyNotFound, VersionNotFound

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    ir_strategy_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    CHECK (length(strategy_id) = 32),
    CHECK (length(user_id) >= 1)
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    dsl TEXT NOT NULL,
    ir_json TEXT NOT NULL,
    ir_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (strategy_id, version),
    FOREIGN KEY (strategy_id) REFERENCES strategies (strategy_id),
    CHECK (version >= 1),
    CHECK (length(user_id) >= 1),
    CHECK (length(ir_hash) = 64)
);

CREATE INDEX IF NOT EXISTS strategies_by_owner
ON strategies (user_id, created_at, strategy_id);
"""


@dataclass(frozen=True, slots=True)
class StrategyRecord:
    """Metadata for one saved strategy. The DSL and IR live on a version."""

    strategy_id: str
    user_id: str
    name: str
    ir_strategy_id: str
    created_at: str
    updated_at: str
    archived_at: str | None
    latest_version: int
    ir_hash: str


@dataclass(frozen=True, slots=True)
class VersionRecord:
    """One immutable save."""

    strategy_id: str
    user_id: str
    version: int
    dsl: str
    ir: dict[str, Any]
    ir_hash: str
    created_at: str


@dataclass(frozen=True, slots=True)
class StrategyPage:
    """One page of strategy metadata, oldest first."""

    items: list[StrategyRecord]
    limit: int
    offset: int
    total: int


@dataclass(frozen=True, slots=True)
class VersionPage:
    """One page of version summaries, lowest version first."""

    strategy_id: str
    items: list[tuple[int, str, str]]
    limit: int
    offset: int
    total: int


def utc_now() -> str:
    """UTC timestamp with a ``Z`` suffix and no fractional seconds."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class StrategyStore:
    """SQLite file for one process.

    ``busy_timeout_ms`` is applied with ``PRAGMA busy_timeout``. Foreign
    keys and secure delete are turned on for this connection. A failure
    while opening closes the connection and leaves the caller's
    ``app.state`` untouched; ``open_store`` only assigns the store after
    this constructor returns.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise ValueError("busy_timeout_ms must be a whole number of milliseconds")
        if busy_timeout_ms < 0 or busy_timeout_ms > 60_000:
            raise ValueError("busy_timeout_ms must be from 0 to 60000")
        self.path = Path(path)
        self._clock = clock or utc_now
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        try:
            self._conn = sqlite3.connect(
                str(self.path),
                timeout=busy_timeout_ms / 1000,
                check_same_thread=False,
                isolation_level=None,
                uri=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA secure_delete=ON")
            self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self._conn.executescript(_SCHEMA)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Close the connection. Later calls raise ``StoreUnavailable``."""
        conn = self._conn
        self._conn = None
        if conn is not None:
            conn.close()

    def create(
        self, user_id: str, compiled: CompiledStrategy
    ) -> tuple[StrategyRecord, VersionRecord]:
        """Insert a strategy and immutable version 1."""
        strategy_id = uuid.uuid4().hex
        with self._immediate():
            now = self._clock()
            self._conn_or_raise().execute(
                """
                INSERT INTO strategies (
                    strategy_id, user_id, name, ir_strategy_id,
                    created_at, updated_at, archived_at
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    strategy_id,
                    user_id,
                    compiled.name,
                    compiled.ir_strategy_id,
                    now,
                    now,
                ),
            )
            version = self._insert_version(strategy_id, user_id, 1, compiled, now)
            record = _strategy(strategy_id, user_id, compiled, now, now, None, 1)
        return record, version

    def add_version(
        self,
        user_id: str,
        strategy_id: str,
        compiled: CompiledStrategy,
    ) -> tuple[StrategyRecord, VersionRecord]:
        """Append the next version. Version 1 is never updated."""
        with self._immediate():
            row = self._locked_strategy(user_id, strategy_id, active_only=True)
            if row is None:
                raise StrategyNotFound()
            current = self._conn_or_raise().execute(
                """
                SELECT COALESCE(MAX(version), 0) FROM strategy_versions
                WHERE strategy_id = ? AND user_id = ?
                """,
                (strategy_id, user_id),
            ).fetchone()[0]
            number = int(current) + 1
            now = self._clock()
            version = self._insert_version(strategy_id, user_id, number, compiled, now)
            self._conn_or_raise().execute(
                """
                UPDATE strategies
                SET name = ?, ir_strategy_id = ?, updated_at = ?
                WHERE strategy_id = ? AND user_id = ?
                """,
                (compiled.name, compiled.ir_strategy_id, now, strategy_id, user_id),
            )
            record = _strategy(
                strategy_id,
                user_id,
                compiled,
                str(row["created_at"]),
                now,
                None,
                number,
            )
        return record, version

    def archive(self, user_id: str, strategy_id: str) -> StrategyRecord:
        """Soft-archive an active strategy. The version rows stay."""
        with self._immediate():
            row = self._locked_strategy(user_id, strategy_id, active_only=True)
            if row is None:
                raise StrategyNotFound()
            latest = self._latest_version(strategy_id, user_id)
            if latest is None:
                raise StrategyNotFound()
            now = self._clock()
            self._conn_or_raise().execute(
                """
                UPDATE strategies
                SET archived_at = ?, updated_at = ?
                WHERE strategy_id = ? AND user_id = ? AND archived_at IS NULL
                """,
                (now, now, strategy_id, user_id),
            )
            record = StrategyRecord(
                strategy_id=strategy_id,
                user_id=user_id,
                name=str(row["name"]),
                ir_strategy_id=str(row["ir_strategy_id"]),
                created_at=str(row["created_at"]),
                updated_at=now,
                archived_at=now,
                latest_version=latest.version,
                ir_hash=latest.ir_hash,
            )
        return record

    def get(self, user_id: str, strategy_id: str) -> tuple[StrategyRecord, VersionRecord]:
        """Return an active strategy and its latest version."""
        with self._lock:
            row = self._locked_strategy(user_id, strategy_id, active_only=True)
            if row is None:
                raise StrategyNotFound()
            latest = self._latest_version(strategy_id, user_id)
            if latest is None:
                raise StrategyNotFound()
            record = _record_from_row(row, latest.version, latest.ir_hash)
        return record, latest

    def list_for(
        self,
        user_id: str,
        limit: int,
        offset: int,
        include_archived: bool,
    ) -> StrategyPage:
        """List this owner's strategies, oldest ``created_at`` then id."""
        with self._lock:
            where = "user_id = ?"
            params: list[object] = [user_id]
            if not include_archived:
                where += " AND archived_at IS NULL"
            conn = self._conn_or_raise()
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM strategies WHERE {where}", params).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT strategy_id FROM strategies
                WHERE {where}
                ORDER BY created_at ASC, strategy_id ASC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            items: list[StrategyRecord] = []
            for row in rows:
                loaded = self._locked_strategy(user_id, str(row["strategy_id"]), active_only=False)
                if loaded is None:
                    continue
                latest = self._latest_version(str(loaded["strategy_id"]), user_id)
                if latest is None:
                    continue
                items.append(_record_from_row(loaded, latest.version, latest.ir_hash))
        return StrategyPage(items=items, limit=limit, offset=offset, total=total)

    def list_versions(
        self,
        user_id: str,
        strategy_id: str,
        limit: int,
        offset: int,
    ) -> VersionPage:
        """List versions of an active strategy, version number ascending."""
        with self._lock:
            if self._locked_strategy(user_id, strategy_id, active_only=True) is None:
                raise StrategyNotFound()
            conn = self._conn_or_raise()
            total = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM strategy_versions
                    WHERE strategy_id = ? AND user_id = ?
                    """,
                    (strategy_id, user_id),
                ).fetchone()[0]
            )
            rows = conn.execute(
                """
                SELECT version, ir_hash, created_at FROM strategy_versions
                WHERE strategy_id = ? AND user_id = ?
                ORDER BY version ASC
                LIMIT ? OFFSET ?
                """,
                (strategy_id, user_id, limit, offset),
            ).fetchall()
        items = [(int(row["version"]), str(row["ir_hash"]), str(row["created_at"])) for row in rows]
        return VersionPage(
            strategy_id=strategy_id,
            items=items,
            limit=limit,
            offset=offset,
            total=total,
        )

    def get_version(self, user_id: str, strategy_id: str, version: int) -> VersionRecord:
        """Return one version of an active strategy."""
        with self._lock:
            if self._locked_strategy(user_id, strategy_id, active_only=True) is None:
                raise StrategyNotFound()
            found = self._version(strategy_id, user_id, version)
        if found is None:
            raise VersionNotFound()
        return found

    def diff(
        self,
        user_id: str,
        strategy_id: str,
        from_version: int,
        to_version: int,
    ) -> dict[str, Any]:
        """Diff two versions of an active strategy."""
        with self._lock:
            if self._locked_strategy(user_id, strategy_id, active_only=True) is None:
                raise StrategyNotFound()
            before = self._version(strategy_id, user_id, from_version)
            after = self._version(strategy_id, user_id, to_version)
        if before is None or after is None:
            raise VersionNotFound()
        return {
            "strategy_id": strategy_id,
            "from_version": before.version,
            "to_version": after.version,
            "from_ir_hash": before.ir_hash,
            "to_ir_hash": after.ir_hash,
            "dsl_diff": dsl_unified_diff(before.dsl, after.dsl, before.version, after.version),
            "ir_diff": ir_structural_diff(before.ir, after.ir),
        }

    @contextmanager
    def _immediate(self) -> Iterator[None]:
        """Hold the store lock and a reserved SQLite transaction."""
        with self._lock:
            conn = self._conn_or_raise()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error:
                self._rollback()
                raise StoreUnavailable from None
            try:
                yield
                conn.execute("COMMIT")
            except (StrategyNotFound, VersionNotFound):
                self._rollback()
                raise
            except StoreUnavailable:
                self._rollback()
                raise
            except sqlite3.Error:
                self._rollback()
                raise StoreUnavailable from None
            except Exception:
                self._rollback()
                raise

    def _rollback(self) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            return

    def _conn_or_raise(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is None:
            raise StoreUnavailable()
        return conn

    def _insert_version(
        self,
        strategy_id: str,
        user_id: str,
        number: int,
        compiled: CompiledStrategy,
        created_at: str,
    ) -> VersionRecord:
        self._conn_or_raise().execute(
            """
            INSERT INTO strategy_versions (
                strategy_id, version, user_id, dsl, ir_json, ir_hash, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                number,
                user_id,
                compiled.dsl,
                compiled.ir_json,
                compiled.ir_hash,
                created_at,
            ),
        )
        return VersionRecord(
            strategy_id=strategy_id,
            user_id=user_id,
            version=number,
            dsl=compiled.dsl,
            ir=compiled.ir,
            ir_hash=compiled.ir_hash,
            created_at=created_at,
        )

    def _locked_strategy(
        self,
        user_id: str,
        strategy_id: str,
        *,
        active_only: bool,
    ) -> sqlite3.Row | None:
        sql = """
            SELECT strategy_id, user_id, name, ir_strategy_id,
                   created_at, updated_at, archived_at
            FROM strategies
            WHERE strategy_id = ? AND user_id = ?
        """
        if active_only:
            sql += " AND archived_at IS NULL"
        try:
            return self._conn_or_raise().execute(sql, (strategy_id, user_id)).fetchone()
        except sqlite3.Error:
            raise StoreUnavailable from None

    def _latest_version(self, strategy_id: str, user_id: str) -> VersionRecord | None:
        try:
            row = self._conn_or_raise().execute(
                """
                SELECT version FROM strategy_versions
                WHERE strategy_id = ? AND user_id = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (strategy_id, user_id),
            ).fetchone()
        except sqlite3.Error:
            raise StoreUnavailable from None
        if row is None:
            return None
        return self._version(strategy_id, user_id, int(row["version"]))

    def _version(self, strategy_id: str, user_id: str, number: int) -> VersionRecord | None:
        try:
            row = self._conn_or_raise().execute(
                """
                SELECT strategy_id, user_id, version, dsl, ir_json, ir_hash, created_at
                FROM strategy_versions
                WHERE strategy_id = ? AND user_id = ? AND version = ?
                """,
                (strategy_id, user_id, number),
            ).fetchone()
        except sqlite3.Error:
            raise StoreUnavailable from None
        if row is None:
            return None
        try:
            ir = json.loads(str(row["ir_json"]))
        except json.JSONDecodeError:
            raise StoreUnavailable from None
        if not isinstance(ir, dict):
            raise StoreUnavailable()
        return VersionRecord(
            strategy_id=str(row["strategy_id"]),
            user_id=str(row["user_id"]),
            version=int(row["version"]),
            dsl=str(row["dsl"]),
            ir=ir,
            ir_hash=str(row["ir_hash"]),
            created_at=str(row["created_at"]),
        )


def _strategy(
    strategy_id: str,
    user_id: str,
    compiled: CompiledStrategy,
    created_at: str,
    updated_at: str,
    archived_at: str | None,
    latest_version: int,
) -> StrategyRecord:
    return StrategyRecord(
        strategy_id=strategy_id,
        user_id=user_id,
        name=compiled.name,
        ir_strategy_id=compiled.ir_strategy_id,
        created_at=created_at,
        updated_at=updated_at,
        archived_at=archived_at,
        latest_version=latest_version,
        ir_hash=compiled.ir_hash,
    )


def _record_from_row(row: sqlite3.Row, latest_version: int, ir_hash: str) -> StrategyRecord:
    archived = row["archived_at"]
    return StrategyRecord(
        strategy_id=str(row["strategy_id"]),
        user_id=str(row["user_id"]),
        name=str(row["name"]),
        ir_strategy_id=str(row["ir_strategy_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        archived_at=None if archived is None else str(archived),
        latest_version=latest_version,
        ir_hash=ir_hash,
    )
