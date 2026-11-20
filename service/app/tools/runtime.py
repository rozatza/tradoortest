"""Stores a tool call is allowed to touch.

A test passes isolated stores. The HTTP route passes openers that use the
same lazy init as the rest of the app: a failed open does not stick on
``app.state``, so the next call can try again.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from app.jobs.queue import JobQueue, QueueBusy
from app.ohlcv.errors import StoreUnavailable as OhlcvStoreUnavailable
from app.ohlcv.routes import OhlcvRuntime
from app.strategies.errors import StoreUnavailable as StrategyStoreUnavailable
from app.strategies.store import StrategyStore
from app.tools.errors import ToolError

StrategyOpener = Callable[[], StrategyStore]
JobOpener = Callable[[], JobQueue]
OhlcvOpener = Callable[[], OhlcvRuntime]


class ToolRuntime:
    """Strategy store, job queue, and OHLCV runtime for one call."""

    def __init__(
        self,
        *,
        strategies: StrategyStore | None = None,
        jobs: JobQueue | None = None,
        ohlcv: OhlcvRuntime | None = None,
        open_strategies: StrategyOpener | None = None,
        open_jobs: JobOpener | None = None,
        open_ohlcv: OhlcvOpener | None = None,
    ) -> None:
        self._strategies = strategies
        self._jobs = jobs
        self._ohlcv = ohlcv
        self._open_strategies = open_strategies
        self._open_jobs = open_jobs
        self._open_ohlcv = open_ohlcv

    def strategies(self) -> StrategyStore:
        if self._strategies is not None:
            return self._strategies
        if self._open_strategies is None:
            raise ToolError(
                "service_unavailable",
                "strategy storage is unavailable",
                retryable=True,
            )
        try:
            store = self._open_strategies()
        except StrategyStoreUnavailable:
            raise ToolError(
                "service_unavailable",
                "strategy storage is unavailable",
                retryable=True,
            ) from None
        except (sqlite3.Error, OSError, ValueError):
            raise ToolError(
                "service_unavailable",
                "strategy storage is unavailable",
                retryable=True,
            ) from None
        if not isinstance(store, StrategyStore):
            raise ToolError(
                "service_unavailable",
                "strategy storage is unavailable",
                retryable=True,
            )
        self._strategies = store
        return store

    def jobs(self) -> JobQueue:
        if self._jobs is not None:
            return self._jobs
        if self._open_jobs is None:
            raise ToolError(
                "service_unavailable",
                "the job database is unavailable",
                retryable=True,
            )
        try:
            queue = self._open_jobs()
        except QueueBusy:
            raise ToolError(
                "service_unavailable",
                "the job database is unavailable",
                retryable=True,
            ) from None
        except (sqlite3.Error, OSError, ValueError):
            raise ToolError(
                "service_unavailable",
                "the job database is unavailable",
                retryable=True,
            ) from None
        if not isinstance(queue, JobQueue):
            raise ToolError(
                "service_unavailable",
                "the job database is unavailable",
                retryable=True,
            )
        self._jobs = queue
        return queue

    def ohlcv(self) -> OhlcvRuntime:
        if self._ohlcv is not None:
            return self._ohlcv
        if self._open_ohlcv is None:
            raise ToolError(
                "service_unavailable",
                "the local store is unavailable",
                retryable=True,
            )
        try:
            runtime = self._open_ohlcv()
        except OhlcvStoreUnavailable:
            raise ToolError(
                "service_unavailable",
                "the local store is unavailable",
                retryable=True,
            ) from None
        except (sqlite3.Error, OSError, ValueError):
            raise ToolError(
                "service_unavailable",
                "the local store is unavailable",
                retryable=True,
            ) from None
        if not isinstance(runtime, OhlcvRuntime):
            raise ToolError(
                "service_unavailable",
                "the local store is unavailable",
                retryable=True,
            )
        self._ohlcv = runtime
        return runtime
