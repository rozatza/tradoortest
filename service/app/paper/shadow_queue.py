"""Shadow quotes off the scheduler thread.

A tick only enqueues. Each quote runs on ``shadow-quotes`` with a
per-call timeout. The queue has a backlog limit. A slow or retrying
venue cannot hold the tick. A timed-out call may still finish in the
background; it does not write the book, and the number of those calls
is capped.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from app.ohlcv.store import SqliteBarStore
from app.paper.shadow import observe_shadow
from app.venue.errors import VenueExecuteDisabled, VenueTransportError

DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_BACKLOG = 4


class ShadowJob:
    def __init__(
        self,
        *,
        store: SqliteBarStore,
        user_id: str,
        strategy_id: str,
        version: int,
        document: Mapping[str, Any],
        now: int,
        venue: object | None,
        chain_rpc: object | None,
    ) -> None:
        self.store = store
        self.user_id = user_id
        self.strategy_id = strategy_id
        self.version = version
        self.document = document
        self.now = now
        self.venue = venue
        self.chain_rpc = chain_rpc
        self.done = threading.Event()
        self.marker = False


def _timeout(raw: float | None) -> float:
    if raw is not None:
        return raw
    text = os.environ.get("SHADOW_QUOTE_TIMEOUT_SECONDS", "").strip()
    if text == "":
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(text)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    if value < 0.05 or value > 30:
        return DEFAULT_TIMEOUT_SECONDS
    return value


def _backlog(raw: int | None) -> int:
    if raw is not None:
        return raw
    text = os.environ.get("SHADOW_QUOTE_BACKLOG", "").strip()
    if text == "":
        return DEFAULT_BACKLOG
    if not text.isdigit():
        return DEFAULT_BACKLOG
    value = int(text)
    if value < 1 or value > 32:
        return DEFAULT_BACKLOG
    return value


class ShadowQueue:
    """One worker. ``submit`` does not wait for the venue."""

    def __init__(self, *, timeout: float | None = None, backlog: int | None = None) -> None:
        self._timeout = _timeout(timeout)
        self._backlog = _backlog(backlog)
        self._queue: queue.Queue[ShadowJob] = queue.Queue(maxsize=self._backlog)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._abandoned = 0

    def submit(self, job: ShadowJob) -> bool:
        self._start()
        with self._lock:
            if self._abandoned >= self._backlog:
                return False
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            return False
        return True

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until work queued so far has finished. Does not call the venue."""
        if self._thread is None:
            return True
        marker = ShadowJob(
            store=None,  # type: ignore[arg-type]
            user_id="",
            strategy_id="",
            version=0,
            document={},
            now=0,
            venue=None,
            chain_rpc=None,
        )
        marker.marker = True
        try:
            self._queue.put(marker, timeout=timeout)
        except queue.Full:
            return False
        return marker.done.wait(timeout)

    def _start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._loop, name="shadow-quotes", daemon=True
            )
            self._thread.start()

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if not job.marker:
                    self._run(job)
            finally:
                job.done.set()

    def _run(self, job: ShadowJob) -> None:
        inner, owns = _inner_venue(job.venue)
        wrapped = _DeadlineVenue(inner, self._timeout, self)
        try:
            observe_shadow(
                job.store,
                user_id=job.user_id,
                strategy_id=job.strategy_id,
                version=job.version,
                document=job.document,
                now=job.now,
                venue=wrapped,
                chain_rpc=job.chain_rpc,
            )
        finally:
            if owns:
                inner.close()  # type: ignore[attr-defined]


def _inner_venue(venue: object | None) -> tuple[object, bool]:
    if venue is not None:
        return venue, False
    from app.venue.jupiter import JupiterVenue

    return JupiterVenue(), True


class _DeadlineVenue:
    """Runs each venue call on a side thread and stops waiting at the timeout."""

    def __init__(self, inner: object, timeout: float, owner: ShadowQueue) -> None:
        self._inner = inner
        self._timeout = timeout
        self._owner = owner

    def quote(self, request: object, *, now: int) -> object:
        return self._within(lambda: self._inner.quote(request, now=now))  # type: ignore[attr-defined]

    def build(self, quote: object, **kwargs: object) -> object:
        return self._within(lambda: self._inner.build(quote, **kwargs))  # type: ignore[attr-defined]

    def execute(self, transaction: object = None) -> None:
        del transaction
        raise VenueExecuteDisabled("execute_disabled")

    def _within(self, fn: Callable[[], object]) -> object:
        box: dict[str, object] = {}
        state = {"done": False, "abandoned": False}

        def run() -> None:
            try:
                box["value"] = fn()
            except Exception as exc:
                box["error"] = exc
            finally:
                with self._owner._lock:
                    if state["abandoned"]:
                        self._owner._abandoned -= 1
                    state["done"] = True

        worker = threading.Thread(target=run, name="shadow-quotes", daemon=True)
        worker.start()
        deadline = time.monotonic() + self._timeout
        while True:
            with self._owner._lock:
                if state["done"]:
                    break
                if time.monotonic() >= deadline:
                    state["abandoned"] = True
                    self._owner._abandoned += 1
                    break
            time.sleep(0.01)
        if state["abandoned"] and not state["done"]:
            raise VenueTransportError("quote_timeout")
        if "error" in box:
            raise box["error"]  # type: ignore[misc]
        return box.get("value")


def record_backlog(
    store: SqliteBarStore,
    *,
    user_id: str,
    strategy_id: str,
    version: int,
    now: int,
) -> None:
    """Mark unshadowed fills dropped. Does not call the venue."""
    pending = store.unshadowed_paper_fills(
        user_id=user_id, strategy_id=strategy_id, version=version
    )
    for fill in pending:
        fill_id, open_time, asset_id, side, qty, notional = fill
        store.insert_paper_shadow(
            {
                "user_id": user_id,
                "strategy_id": strategy_id,
                "version": version,
                "fill_id": fill_id,
                "open_time": open_time,
                "asset_id": asset_id,
                "side": side,
                "input_mint": None,
                "output_mint": None,
                "paper_out": str(qty if side == "buy" else notional),
                "quoted_out": None,
                "price_impact": None,
                "route_label": None,
                "router": None,
                "network_fee_lamports": None,
                "priority_fee_lamports": None,
                "rent_lamports": None,
                "latency_ms": None,
                "slippage_bps": None,
                "quote_status": "error",
                "build_status": "not_requested",
                "error_code": "backlog",
                "quoted_at": now,
            }
        )
