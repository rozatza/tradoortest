"""SQLite-backed job queue for one process.

Redis would add a second service and a hashed dependency for a runtime that
serves one user. SQLite is already how this process stores bars. The file
keeps a finished result after the worker has exited. A thread pool runs the
jobs. Admission checks, the idempotency key, and the result rows share one
transaction so two requests cannot create two jobs for one key.

One process holds an exclusive lock file next to the database. A second
process must not open it: it would mark this process's live jobs interrupted.
The request body, bars included, is deleted when the job succeeds, fails, or
is marked interrupted. A capped chart window may be stored in ``job_charts``
for a succeeded job. It is not part of the result JSON. The result and that
window follow the GeckoTerminal cache: at most 24 hours, then the job, the
result, the chart window, and any leftover body are deleted.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from app.jobs.chart import take_chart_parts
from app.jobs.errors import job_error
from app.jobs.run import run_backtest_job

Runner = Callable[[Mapping[str, Any]], Mapping[str, Any]]

# CoinGecko API terms do not allow a persistent copy. Same cap as the bar cache.
MAX_RESULT_TTL_SECONDS = 24 * 60 * 60

_CREATE = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    error_json TEXT,
    body_hash TEXT,
    UNIQUE (user_id, request_key)
);
CREATE TABLE IF NOT EXISTS job_bodies (
    job_id TEXT PRIMARY KEY,
    body_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_results (
    job_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_charts (
    job_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    bars_json TEXT NOT NULL,
    indicators_json TEXT NOT NULL
);
"""


class LimitReached(Exception):
    """Admission refused. ``code`` is ``concurrency_limit`` or ``rate_limit``."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class KeyReused(Exception):
    """This ``request_key`` is already stored with a different body."""


class QueueBusy(Exception):
    """Another process holds this job database."""


class JobExpired(Exception):
    """The job was stored, and its retention window has ended.

    The row, the result, and any leftover body are already deleted.
    """


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One stored job. ``error`` is set when ``status`` is ``failed``."""

    job_id: str
    user_id: str
    request_key: str
    status: str
    error: dict[str, Any] | None
    created: bool
    created_at: float | None = None


class JobQueue:
    """Admit jobs, run them on a thread pool, and read the stored result.

    ``max_concurrent`` counts queued and running jobs. ``per_minute`` counts
    jobs created in the last 60 seconds. A repeated ``request_key`` for the
    same user returns the original job and does not consume either limit.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_concurrent: int = 2,
        per_minute: int = 30,
        runner: Runner | None = None,
        clock: Callable[[], float] | None = None,
        error_from: Callable[[BaseException], dict[str, Any]] | None = None,
        busy_timeout_ms: int = 5000,
        result_ttl_seconds: int = MAX_RESULT_TTL_SECONDS,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        if per_minute < 1:
            raise ValueError("per_minute must be at least 1")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be at least 0")
        self._ttl = _check_result_ttl(result_ttl_seconds)
        self.max_concurrent = max_concurrent
        self.per_minute = per_minute
        self._runner = runner or run_backtest_job
        self._clock = clock or _unix_time
        self._error_from = error_from or job_error
        self._lock = threading.Lock()
        self._lock_file = _lock_file(path)
        try:
            self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self._conn.execute("PRAGMA secure_delete=ON")
            self._conn.executescript(_CREATE)
            self._ensure_body_hash()
            self._fail_interrupted()
            self._purge()
        except Exception:
            self._conn_close_quiet()
            self._release_lock()
            raise
        self._executor = _Pool(max_concurrent)

    @property
    def result_ttl_seconds(self) -> int:
        """How long a finished result stays stored. Never above 24 hours."""
        return self._ttl

    def close(self) -> None:
        """Wait for running jobs, close the database, and release the lock."""
        self._executor.shutdown()
        with self._lock:
            self._conn.close()
        self._release_lock()

    def start(self, *, user_id: str, request_key: str, body: Mapping[str, Any]) -> JobRecord:
        """Insert a job or return the one already stored for ``request_key``."""
        if not user_id:
            raise ValueError("user_id is required")
        payload = json.dumps(body, allow_nan=False, separators=(",", ":"))
        digest = _body_hash(payload)
        created = False
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                _removed, changed = self._purge_unlocked()
                existing = self._find(user_id, request_key)
                if existing is None:
                    active = self._conn.execute(
                        """
                        SELECT COUNT(*) FROM jobs
                        WHERE user_id = ? AND status IN ('queued', 'running')
                        """,
                        (user_id,),
                    ).fetchone()[0]
                    if active >= self.max_concurrent:
                        raise LimitReached(
                            "concurrency_limit",
                            f"at most {self.max_concurrent} backtests can run at once",
                        )
                    recent = self._conn.execute(
                        """
                        SELECT COUNT(*) FROM jobs
                        WHERE user_id = ? AND created_at > ?
                        """,
                        (user_id, self._clock() - 60),
                    ).fetchone()[0]
                    if recent >= self.per_minute:
                        raise LimitReached(
                            "rate_limit",
                            f"at most {self.per_minute} backtests can start per minute",
                        )
                    job_id = uuid.uuid4().hex
                    self._conn.execute(
                        """
                        INSERT INTO jobs (
                            job_id, user_id, request_key, status, created_at,
                            error_json, body_hash
                        )
                        VALUES (?, ?, ?, 'queued', ?, NULL, ?)
                        """,
                        (job_id, user_id, request_key, self._clock(), digest),
                    )
                    self._conn.execute(
                        "INSERT INTO job_bodies (job_id, body_json) VALUES (?, ?)",
                        (job_id, payload),
                    )
                    created = True
                else:
                    job_id, stored = existing
                    if stored != digest:
                        raise KeyReused(request_key)
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback()
                raise
            if changed:
                self._checkpoint()
        if created:
            self._executor.submit(self._execute, job_id)
        return self.get(user_id, job_id, created=created)

    def get(self, user_id: str, job_id: str, *, created: bool = False) -> JobRecord:
        """Return the job owned by ``user_id``.

        An id that is still stored past its TTL raises ``JobExpired`` and the
        row is deleted. An id that is not stored raises ``KeyError``.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                removed, changed = self._purge_unlocked()
                row = self._conn.execute(
                    """
                    SELECT job_id, user_id, request_key, status, error_json, created_at
                    FROM jobs WHERE job_id = ? AND user_id = ?
                    """,
                    (job_id, user_id),
                ).fetchone()
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback()
                raise
            if changed:
                self._checkpoint()
        if (job_id, user_id) in removed:
            raise JobExpired(job_id)
        if row is None:
            raise KeyError(job_id)
        error = json.loads(row[4]) if row[4] else None
        return JobRecord(row[0], row[1], row[2], row[3], error, created, float(row[5]))

    def result_json(self, user_id: str, job_id: str) -> str | None:
        """The stored result text, or ``None`` when the job has not finished.

        A job past its TTL raises ``JobExpired`` and is deleted.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                removed, changed = self._purge_unlocked()
                row = self._conn.execute(
                    """
                    SELECT result_json FROM job_results
                    WHERE job_id = ? AND user_id = ?
                    """,
                    (job_id, user_id),
                ).fetchone()
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback()
                raise
            if changed:
                self._checkpoint()
        if (job_id, user_id) in removed:
            raise JobExpired(job_id)
        if row is None:
            return None
        return str(row[0])

    def bars_json(self, user_id: str, job_id: str) -> str | None:
        """The capped bar window, or ``None`` when this job has no chart row.

        Does not read ``result_json``. A job past its TTL raises ``JobExpired``.
        """
        return self._chart_column(user_id, job_id, "bars_json")

    def indicators_json(self, user_id: str, job_id: str) -> str | None:
        """The capped indicator window. Does not read ``result_json``."""
        return self._chart_column(user_id, job_id, "indicators_json")

    def trades_json(self, user_id: str, job_id: str) -> tuple[str | None, str | None]:
        """``(json_type, json_text)`` of ``$.trades`` only.

        SQLite extracts the trade log. The equity curve is not loaded into
        Python. A job past its TTL raises ``JobExpired``. ``(None, None)``
        means the result row is missing. A missing ``trades`` member has
        json type ``None`` and text ``None`` while the row exists: the
        caller tells those apart with the row check below, so a missing
        member is ``("missing", None)``.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                removed, changed = self._purge_unlocked()
                row = self._conn.execute(
                    """
                    SELECT
                        CASE
                            WHEN result_json IS NULL THEN NULL
                            ELSE COALESCE(json_type(result_json, '$.trades'), 'missing')
                        END,
                        json_extract(result_json, '$.trades')
                    FROM job_results
                    WHERE job_id = ? AND user_id = ?
                    """,
                    (job_id, user_id),
                ).fetchone()
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback()
                raise
            if changed:
                self._checkpoint()
        if (job_id, user_id) in removed:
            raise JobExpired(job_id)
        if row is None:
            return None, None
        kind = None if row[0] is None else str(row[0])
        text = None if row[1] is None else str(row[1])
        return kind, text

    def _chart_column(self, user_id: str, job_id: str, column: str) -> str | None:
        if column not in {"bars_json", "indicators_json"}:
            raise ValueError(column)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                removed, changed = self._purge_unlocked()
                row = self._conn.execute(
                    f"""
                    SELECT {column} FROM job_charts
                    WHERE job_id = ? AND user_id = ?
                    """,
                    (job_id, user_id),
                ).fetchone()
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback()
                raise
            if changed:
                self._checkpoint()
        if (job_id, user_id) in removed:
            raise JobExpired(job_id)
        if row is None:
            return None
        return str(row[0])

    def _execute(self, job_id: str) -> None:
        body: str | None = None
        user_id: str | None = None
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                _removed, changed = self._purge_unlocked()
                if self._job_status(job_id) != "queued":
                    self._conn.execute("COMMIT")
                else:
                    self._conn.execute(
                        """
                        UPDATE jobs SET status = 'running'
                        WHERE job_id = ? AND status = 'queued'
                        """,
                        (job_id,),
                    )
                    row = self._conn.execute(
                        "SELECT body_json FROM job_bodies WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    owner = self._conn.execute(
                        "SELECT user_id FROM jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    self._conn.execute("COMMIT")
                    if row is not None and owner is not None:
                        body = str(row[0])
                        user_id = str(owner[0])
            except Exception:
                self._rollback()
                raise
            if changed:
                self._checkpoint()
        if body is None or user_id is None:
            return
        take_chart_parts()
        try:
            result = self._runner(json.loads(body))
            text = json.dumps(result, allow_nan=False, separators=(",", ":"))
            chart = take_chart_parts()
        except Exception as exc:
            take_chart_parts()
            self._mark_failed(job_id, json.dumps(self._error_from(exc), allow_nan=False))
            return
        self._store_result(job_id, user_id, text, chart)

    def _store_result(
        self,
        job_id: str,
        user_id: str,
        text: str,
        chart: tuple[str, str] | None = None,
    ) -> None:
        """Write the result and drop the request body. Retry a held write lock.

        A failure here used to leave the job ``running``, so it kept its
        concurrency slot. After the retries the job is marked failed instead.
        """
        last: BaseException | None = None
        for _attempt in range(3):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        _removed, changed = self._purge_unlocked()
                        # The job may already have been purged by another
                        # start or read. ``removed`` only lists rows this
                        # transaction just deleted, so re-read the row.
                        if self._job_status(job_id) != "running":
                            self._conn.execute("COMMIT")
                        else:
                            self._conn.execute(
                                """
                                INSERT INTO job_results (job_id, user_id, result_json)
                                VALUES (?, ?, ?)
                                """,
                                (job_id, user_id, text),
                            )
                            if chart is not None:
                                self._conn.execute(
                                    """
                                    INSERT INTO job_charts (
                                        job_id, user_id, bars_json, indicators_json
                                    )
                                    VALUES (?, ?, ?, ?)
                                    """,
                                    (job_id, user_id, chart[0], chart[1]),
                                )
                            self._conn.execute(
                                """
                                UPDATE jobs SET status = 'succeeded', error_json = NULL
                                WHERE job_id = ? AND status = 'running'
                                """,
                                (job_id,),
                            )
                            self._delete_body(job_id)
                            self._conn.execute("COMMIT")
                            changed = True
                    except Exception:
                        self._rollback()
                        raise
                    if changed:
                        self._checkpoint()
                return
            except sqlite3.OperationalError as exc:
                last = exc
                continue
            except Exception as exc:
                self._mark_failed(job_id, json.dumps(self._error_from(exc), allow_nan=False))
                return
        message = last if last is not None else RuntimeError("result was not stored")
        self._mark_failed(job_id, json.dumps(self._error_from(message), allow_nan=False))

    def _mark_failed(self, job_id: str, error: str) -> None:
        """Mark ``job_id`` failed and delete its body. Retry a held write lock."""
        for _attempt in range(3):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        _removed, changed = self._purge_unlocked()
                        if self._job_status(job_id) != "running":
                            self._conn.execute("COMMIT")
                        else:
                            self._conn.execute(
                                """
                                UPDATE jobs SET status = 'failed', error_json = ?
                                WHERE job_id = ? AND status = 'running'
                                """,
                                (error, job_id),
                            )
                            self._delete_body(job_id)
                            self._conn.execute("COMMIT")
                            changed = True
                    except Exception:
                        self._rollback()
                        raise
                    if changed:
                        self._checkpoint()
                return
            except sqlite3.OperationalError:
                continue

    def _find(self, user_id: str, request_key: str) -> tuple[str, str | None] | None:
        row = self._conn.execute(
            """
            SELECT job_id, body_hash FROM jobs
            WHERE user_id = ? AND request_key = ?
            """,
            (user_id, request_key),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), None if row[1] is None else str(row[1])

    def _delete_body(self, job_id: str) -> None:
        self._conn.execute("DELETE FROM job_bodies WHERE job_id = ?", (job_id,))

    def _job_status(self, job_id: str) -> str | None:
        """Status of ``job_id``, or ``None`` when the row is already gone."""
        row = self._conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0])

    def _purge(self) -> None:
        """Delete jobs whose retention window has ended. Startup calls this."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            _removed, changed = self._purge_unlocked()
            self._conn.execute("COMMIT")
        except Exception:
            self._rollback()
            raise
        if changed:
            self._checkpoint()

    def _purge_unlocked(self) -> tuple[set[tuple[str, str]], bool]:
        """Delete expired jobs and orphan rows. Caller holds the transaction.

        ``created_at + ttl <= now`` matches the bar cache. The job row, its
        result, and any leftover body go together. Each item is
        ``(job_id, user_id)`` so another user's read stays ``not_found``.
        A result or body whose job row is already gone is deleted too: a
        late worker must not leave fill prices behind. The bool is true when
        any row was deleted, so the caller can checkpoint.
        """
        before = self._changes()
        cutoff = self._clock() - self._ttl
        rows = self._conn.execute(
            "SELECT job_id, user_id FROM jobs WHERE created_at <= ?",
            (cutoff,),
        ).fetchall()
        self._conn.execute(
            """
            DELETE FROM job_results WHERE job_id IN (
                SELECT job_id FROM jobs WHERE created_at <= ?
            )
            """,
            (cutoff,),
        )
        self._conn.execute(
            """
            DELETE FROM job_bodies WHERE job_id IN (
                SELECT job_id FROM jobs WHERE created_at <= ?
            )
            """,
            (cutoff,),
        )
        self._conn.execute(
            """
            DELETE FROM job_charts WHERE job_id IN (
                SELECT job_id FROM jobs WHERE created_at <= ?
            )
            """,
            (cutoff,),
        )
        self._conn.execute("DELETE FROM jobs WHERE created_at <= ?", (cutoff,))
        self._conn.execute("DELETE FROM job_results WHERE job_id NOT IN (SELECT job_id FROM jobs)")
        self._conn.execute("DELETE FROM job_bodies WHERE job_id NOT IN (SELECT job_id FROM jobs)")
        self._conn.execute("DELETE FROM job_charts WHERE job_id NOT IN (SELECT job_id FROM jobs)")
        removed = {(str(row[0]), str(row[1])) for row in rows}
        return removed, self._changes() != before

    def _changes(self) -> int:
        row = self._conn.execute("SELECT total_changes()").fetchone()
        return 0 if row is None else int(row[0])

    def _fail_interrupted(self) -> None:
        """A previous process died. Those rows must not hold a concurrency slot.

        Their request bodies go with them. The bars are not a cache.
        """
        error = json.dumps(
            {
                "code": "interrupted",
                "message": "the backtest did not finish before the process stopped",
                "path": None,
            }
        )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """
                DELETE FROM job_bodies WHERE job_id IN (
                    SELECT job_id FROM jobs WHERE status IN ('queued', 'running')
                )
                """
            )
            self._conn.execute(
                """
                UPDATE jobs SET status = 'failed', error_json = ?
                WHERE status IN ('queued', 'running')
                """,
                (error,),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._rollback()
            raise
        self._checkpoint()

    def _ensure_body_hash(self) -> None:
        """``CREATE TABLE IF NOT EXISTS`` does not add a column to an old file."""
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        if "body_hash" not in columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN body_hash TEXT")

    def _checkpoint(self) -> None:
        """Drop WAL frames so a deleted body is not left in the sidecar."""
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _conn_close_quiet(self) -> None:
        conn = getattr(self, "_conn", None)
        if conn is None:
            return
        try:
            conn.close()
        except sqlite3.Error:
            pass

    def _release_lock(self) -> None:
        handle = getattr(self, "_lock_file", None)
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._lock_file = None


class _Pool:
    """A small thread pool. ``shutdown`` waits for work already submitted."""

    def __init__(self, workers: int) -> None:
        from concurrent.futures import ThreadPoolExecutor

        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="backtest-job")

    def submit(self, fn: Callable[..., None], *args: object) -> None:
        self._pool.submit(fn, *args)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)


def _unix_time() -> float:
    import time

    return time.time()


def _body_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_result_ttl(seconds: int) -> int:
    """Reject a retention window longer than the GeckoTerminal cache.

    A longer window is an error, not a clamp. A licensed provider that allows
    storage is ROZ-92.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise ValueError("result TTL must be a whole number of seconds")
    if seconds < 1 or seconds > MAX_RESULT_TTL_SECONDS:
        raise ValueError(
            "result TTL must be from 1 second to 24 hours; "
            "the CoinGecko API terms do not allow a longer store"
        )
    return seconds


def _lock_file(path: str | Path) -> IO[str]:
    """Exclusive lock beside the database. Released when the file is closed.

    A crashed process drops the lock, so the next process can open the file
    and mark that process's jobs interrupted. A live second process cannot.
    """
    target = Path(path)
    handle = target.with_name(target.name + ".lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise QueueBusy("another process is using this job database") from exc
    return handle
