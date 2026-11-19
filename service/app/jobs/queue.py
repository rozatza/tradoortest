"""SQLite-backed job queue for one process.

Redis would add a second service and a hashed dependency for a runtime that
serves one user. SQLite is already how this process stores bars. The file
keeps a finished result after the worker has exited. A thread pool runs the
jobs. Admission checks, the idempotency key, and the result rows share one
transaction so two requests cannot create two jobs for one key.

One process holds an exclusive lock file next to the database. A second
process must not open it: it would mark this process's live jobs interrupted.
The request body, bars included, is deleted when the job succeeds, fails, or
is marked interrupted. The result row keeps fill prices.
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

from app.jobs.errors import job_error
from app.jobs.run import run_backtest_job

Runner = Callable[[Mapping[str, Any]], Mapping[str, Any]]

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


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One stored job. ``error`` is set when ``status`` is ``failed``."""

    job_id: str
    user_id: str
    request_key: str
    status: str
    error: dict[str, Any] | None
    created: bool


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
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        if per_minute < 1:
            raise ValueError("per_minute must be at least 1")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be at least 0")
        self.max_concurrent = max_concurrent
        self.per_minute = per_minute
        self._runner = runner or run_backtest_job
        self._clock = clock or _unix_time
        self._error_from = error_from or job_error
        self._lock = threading.Lock()
        self._lock_file = _lock_file(path)
        try:
            self._conn = sqlite3.connect(
                str(path), check_same_thread=False, isolation_level=None
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self._conn.execute("PRAGMA secure_delete=ON")
            self._conn.executescript(_CREATE)
            self._ensure_body_hash()
            self._fail_interrupted()
        except Exception:
            self._conn_close_quiet()
            self._release_lock()
            raise
        self._executor = _Pool(max_concurrent)

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
        if created:
            self._executor.submit(self._execute, job_id)
        return self.get(user_id, job_id, created=created)

    def get(self, user_id: str, job_id: str, *, created: bool = False) -> JobRecord:
        """Return the job owned by ``user_id``, or raise ``KeyError``."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT job_id, user_id, request_key, status, error_json
                FROM jobs WHERE job_id = ? AND user_id = ?
                """,
                (job_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        error = json.loads(row[4]) if row[4] else None
        return JobRecord(row[0], row[1], row[2], row[3], error, created)

    def result_json(self, user_id: str, job_id: str) -> str | None:
        """The stored result text, or ``None`` when the job has not finished."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT result_json FROM job_results
                WHERE job_id = ? AND user_id = ?
                """,
                (job_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return str(row[0])

    def _execute(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status = 'running' WHERE job_id = ? AND status = 'queued'",
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
        if row is None or owner is None:
            return
        try:
            result = self._runner(json.loads(row[0]))
            text = json.dumps(result, allow_nan=False, separators=(",", ":"))
        except Exception as exc:
            self._mark_failed(job_id, json.dumps(self._error_from(exc), allow_nan=False))
            return
        self._store_result(job_id, str(owner[0]), text)

    def _store_result(self, job_id: str, user_id: str, text: str) -> None:
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
                        self._conn.execute(
                            """
                            INSERT INTO job_results (job_id, user_id, result_json)
                            VALUES (?, ?, ?)
                            """,
                            (job_id, user_id, text),
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
                    except Exception:
                        self._rollback()
                        raise
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
                        self._conn.execute(
                            """
                            UPDATE jobs SET status = 'failed', error_json = ?
                            WHERE job_id = ? AND status = 'running'
                            """,
                            (error, job_id),
                        )
                        self._delete_body(job_id)
                        self._conn.execute("COMMIT")
                    except Exception:
                        self._rollback()
                        raise
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
