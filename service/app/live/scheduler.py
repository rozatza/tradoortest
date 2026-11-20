"""Paper scheduler. One background thread, one decision per closed bar.

The thread is not the request event loop. ``GET /health`` does not wait for
a tick. Each tick asks the feed for new bars, then calls ``step`` once for
every closed real bar the cursor has not already handled. The cursor is
``(strategy id, version, last open time)`` in the bar cache. A new scheduler
on the same file continues after that open time, so a restart does not
repeat a bar and does not skip one that was committed.

Missed slots are logged once the following bar is already closed. Until
then the tick waits, so a bar that is only a poll late is still decided
once. A real bar that shows up after the cursor has moved past its open
time is logged late and is not decided. Synthetic bars are not passed to
the runner.

The book view passed to ``step`` is empty. This module records the decision
and the runner state. It does not fill, attach an OCO, or send an order.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.auth import LOCAL_USER_ID
from app.ir.canonical import canonical_json
from app.ir.validate import format_issues, validate_ir
from app.live.feed import LiveFeed, last_closed_open
from app.live.series import build_window
from app.ohlcv.bars import TIMEFRAME_SECONDS, Bar
from app.ohlcv.errors import SourceError
from app.ohlcv.registry import BY_SYMBOL
from app.ohlcv.store import MAX_CACHE_TTL_SECONDS, SqliteBarStore
from app.runner import (
    BarDecision,
    BookView,
    OcoDrop,
    OcoTouch,
    RunnerState,
    config_from_document,
    initial_state,
    step,
)
from app.runner.core import MemoryState

logger = logging.getLogger("app.live")

# Finest native bar. Used when nothing is registered, and as the default
# pause between ticks. A registered strategy waits its own bar size when
# the caller does not pass an interval.
POLL_SECONDS = 60.0

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class PaperStrategy:
    """One registered version. ``assets`` is ``(asset id, mint)`` in IR order."""

    strategy_id: str
    version: int
    document: dict[str, Any]
    bar_size: str
    warmup_bars: int
    assets: tuple[tuple[str, str], ...]


def register_paper_strategy(
    scheduler: PaperScheduler,
    strategy_id: str,
    version: int,
    document: Mapping[str, Any],
) -> None:
    """Register one strategy version. There is no HTTP deploy route."""
    scheduler.register(strategy_id, version, document)


def live_feed_enabled(raw: str | None = None) -> bool:
    """True when the scheduler should run. Unset, blank, and ``0`` are off."""
    if raw is None:
        raw = os.environ.get("LIVE_FEED_ENABLED", "")
    return raw.strip().lower() in _TRUTHY


class PaperScheduler:
    """Run ``tick`` on a thread until ``stop``."""

    def __init__(
        self,
        store: SqliteBarStore,
        feed: LiveFeed,
        *,
        now: Callable[[], int] | None = None,
        user_id: str = LOCAL_USER_ID,
        interval: float | None = None,
        on_tick: Callable[[], None] | None = None,
        owns_store: bool = False,
        owns_feed: bool = False,
        join_timeout: float = 5.0,
    ) -> None:
        self._store = store
        self._feed = feed
        self._now = (lambda: int(time.time())) if now is None else now
        self._user_id = user_id
        self._interval = interval
        self._on_tick = on_tick
        self._owns_store = owns_store
        self._owns_feed = owns_feed
        self._join_timeout = join_timeout
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

    def register(self, strategy_id: str, version: int, document: Mapping[str, Any]) -> None:
        """Persist one version. The same document again is a no-op."""
        strategy = _strategy(strategy_id, version, document)
        text = canonical_json(strategy.document)
        with self._lock:
            self._store.save_live_registration(
                user_id=self._user_id,
                strategy_id=strategy.strategy_id,
                version=strategy.version,
                document_json=text,
                bar_size=strategy.bar_size,
            )

    def tick(self) -> None:
        """Fetch, then decide each new closed bar. Safe to call from a test."""
        with self._lock:
            now = int(self._now())
            for strategy in self._load():
                try:
                    self._tick_one(strategy, now)
                except Exception:
                    logger.warning(
                        "live scheduler skipped strategy_id=%s version=%s",
                        strategy.strategy_id,
                        strategy.version,
                    )

    def start(self) -> None:
        """Start the worker thread. A second call while it is alive does nothing."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="live-bar-scheduler", daemon=False
        )
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to leave and wait for it. A stuck tick is not killed."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(self._join_timeout)
        if thread is not None and thread.is_alive():
            return
        self._thread = None
        if self._closed:
            return
        self._closed = True
        if self._owns_feed:
            self._feed.close()
        if self._owns_store:
            self._store.close()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._on_tick is not None:
                    self._on_tick()
                else:
                    self.tick()
            except Exception:
                logger.warning("live scheduler tick failed")
            if self._stop.wait(self._pause()):
                return

    def _pause(self) -> float:
        if self._interval is not None:
            return self._interval
        sizes = [
            TIMEFRAME_SECONDS[strategy.bar_size]
            for strategy in self._load_unlocked()
        ]
        if not sizes:
            return POLL_SECONDS
        return float(min(sizes))

    def _load(self) -> list[PaperStrategy]:
        return self._load_unlocked()

    def _load_unlocked(self) -> list[PaperStrategy]:
        rows = self._store.live_registrations(user_id=self._user_id)
        loaded: list[PaperStrategy] = []
        for strategy_id, version, document_json, _bar_size in rows:
            document = json.loads(document_json)
            loaded.append(_strategy(strategy_id, version, document))
        return loaded

    def _tick_one(self, strategy: PaperStrategy, now: int) -> None:
        step_seconds = TIMEFRAME_SECONDS[strategy.bar_size]
        closed_through = last_closed_open(now, step_seconds)
        if closed_through is None:
            return
        cursor = self._store.live_cursor(
            user_id=self._user_id,
            strategy_id=strategy.strategy_id,
            version=strategy.version,
        )
        self._refresh(strategy, cursor, closed_through, step_seconds)
        bars = self._load_bars(strategy, closed_through + step_seconds)
        asset_ids = tuple(asset_id for asset_id, _mint in strategy.assets)
        if cursor is None:
            origin = _first_real(bars, step_seconds, now)
            if origin is None:
                return
            next_time = origin
            state = initial_state(asset_ids)
            last_handled: int | None = None
        else:
            origin, last_handled, state_json = cursor
            next_time = last_handled + step_seconds
            state = _load_state(state_json, asset_ids)
        decided: set[int] = set()
        while next_time <= closed_through:
            kind = _classify(bars, strategy.assets, next_time, step_seconds, now)
            if kind == "in_progress":
                break
            if kind == "missing":
                if now >= next_time + 2 * step_seconds:
                    self._advance_skip(strategy, origin, next_time, state, "missed", "missing")
                    last_handled = next_time
                    next_time += step_seconds
                    continue
                break
            if kind != "real":
                self._advance_skip(
                    strategy, origin, next_time, state, "skipped", "synthetic_bar"
                )
                last_handled = next_time
                next_time += step_seconds
                continue
            if self._store.has_live_decision(
                user_id=self._user_id,
                strategy_id=strategy.strategy_id,
                version=strategy.version,
                open_time=next_time,
            ):
                self._note(strategy, next_time, "duplicate", "already evaluated")
                saved = self._store.live_cursor(
                    user_id=self._user_id,
                    strategy_id=strategy.strategy_id,
                    version=strategy.version,
                )
                if saved is not None:
                    state = _load_state(saved[2], asset_ids)
                last_handled = next_time
                next_time += step_seconds
                continue
            late = now >= next_time + 2 * step_seconds
            try:
                result = step(
                    config_from_document(strategy.document),
                    state,
                    build_window(
                        strategy.document,
                        asset_ids,
                        _real_map(bars, strategy.assets),
                        origin,
                        next_time,
                        step_seconds,
                    ),
                    BookView(),
                )
            except Exception:
                logger.warning(
                    "live decision failed strategy_id=%s version=%s open_time=%s",
                    strategy.strategy_id,
                    strategy.version,
                    next_time,
                )
                return
            wrote = self._store.commit_live_step(
                user_id=self._user_id,
                strategy_id=strategy.strategy_id,
                version=strategy.version,
                origin=origin,
                open_time=next_time,
                state_json=_dump_state(result.state),
                decision_json=_dump_decision(result.decision),
                log_kind="late" if late else None,
                log_detail="following bar already closed" if late else "",
            )
            if late:
                logger.info(
                    "late bar strategy_id=%s version=%s open_time=%s",
                    strategy.strategy_id,
                    strategy.version,
                    next_time,
                )
            if wrote:
                decided.add(next_time)
                state = result.state
            else:
                self._note(strategy, next_time, "duplicate", "already evaluated")
                saved = self._store.live_cursor(
                    user_id=self._user_id,
                    strategy_id=strategy.strategy_id,
                    version=strategy.version,
                )
                if saved is not None:
                    state = _load_state(saved[2], asset_ids)
            last_handled = next_time
            next_time += step_seconds
        if last_handled is not None:
            self._scan_behind(strategy, bars, last_handled, decided, step_seconds, now)

    def _refresh(
        self,
        strategy: PaperStrategy,
        cursor: tuple[int, int, str] | None,
        closed_through: int,
        step_seconds: int,
    ) -> None:
        if cursor is None:
            count = strategy.warmup_bars + 1
            start = closed_through - (count - 1) * step_seconds
        else:
            start = cursor[1]
        end = closed_through + step_seconds
        if start < 0:
            start = 0
        if start > end:
            start = end
        for _asset_id, mint in strategy.assets:
            try:
                self._feed.fetch(mint, strategy.bar_size, start, end)
            except SourceError:
                logger.warning(
                    "live feed failed mint=%s timeframe=%s",
                    mint,
                    strategy.bar_size,
                )

    def _load_bars(
        self, strategy: PaperStrategy, end: int
    ) -> dict[str, dict[int, Bar]]:
        loaded: dict[str, dict[int, Bar]] = {}
        for asset_id, mint in strategy.assets:
            rows = self._store.load(
                user_id=self._user_id,
                mint=mint,
                timeframe=strategy.bar_size,
                start=0,
                end=end,
            )
            loaded[asset_id] = {bar.open_time: bar for bar in rows}
        return loaded

    def _advance_skip(
        self,
        strategy: PaperStrategy,
        origin: int,
        open_time: int,
        state: RunnerState,
        kind: str,
        detail: str,
    ) -> None:
        self._store.commit_live_skip(
            user_id=self._user_id,
            strategy_id=strategy.strategy_id,
            version=strategy.version,
            origin=origin,
            open_time=open_time,
            state_json=_dump_state(state),
            log_kind=kind,
            log_detail=detail,
        )
        if kind == "missed":
            logger.info(
                "missed bar strategy_id=%s version=%s open_time=%s",
                strategy.strategy_id,
                strategy.version,
                open_time,
            )

    def _note(self, strategy: PaperStrategy, open_time: int, kind: str, detail: str) -> None:
        if self._store.live_bar_logged(
            user_id=self._user_id,
            strategy_id=strategy.strategy_id,
            version=strategy.version,
            open_time=open_time,
            kind=kind,
        ):
            return
        self._store.log_live_bar(
            user_id=self._user_id,
            strategy_id=strategy.strategy_id,
            version=strategy.version,
            open_time=open_time,
            kind=kind,
            detail=detail,
        )
        logger.info(
            "%s bar strategy_id=%s version=%s open_time=%s",
            kind,
            strategy.strategy_id,
            strategy.version,
            open_time,
        )

    def _scan_behind(
        self,
        strategy: PaperStrategy,
        bars: Mapping[str, Mapping[int, Bar]],
        last_handled: int,
        decided: set[int],
        step_seconds: int,
        now: int,
    ) -> None:
        known = self._store.live_decision_times(
            user_id=self._user_id,
            strategy_id=strategy.strategy_id,
            version=strategy.version,
        )
        seen: set[int] = set()
        for _asset_id, by_time in _pairs(strategy, bars):
            for open_time, bar in by_time.items():
                if open_time in seen or open_time in decided:
                    continue
                if open_time > last_handled:
                    continue
                if bar.synthetic or not _closed_bar(bar, step_seconds, now):
                    continue
                seen.add(open_time)
                if open_time in known:
                    self._note(strategy, open_time, "duplicate", "already evaluated")
                else:
                    self._note(strategy, open_time, "late", "cursor already passed")


def _pairs(
    strategy: PaperStrategy, bars: Mapping[str, Mapping[int, Bar]]
) -> list[tuple[str, Mapping[int, Bar]]]:
    return [(asset_id, bars[asset_id]) for asset_id, _mint in strategy.assets]


def _closed_bar(bar: Bar, step_seconds: int, now: int) -> bool:
    return bar.open_time + step_seconds <= now


def _classify(
    bars: Mapping[str, Mapping[int, Bar]],
    assets: Sequence[tuple[str, str]],
    open_time: int,
    step_seconds: int,
    now: int,
) -> str:
    if open_time + step_seconds > now:
        return "in_progress"
    saw_real = False
    saw_synthetic = False
    for asset_id, _mint in assets:
        bar = bars[asset_id].get(open_time)
        if bar is None:
            continue
        if bar.synthetic:
            saw_synthetic = True
        elif _closed_bar(bar, step_seconds, now):
            saw_real = True
    if saw_real:
        return "real"
    if saw_synthetic:
        return "synthetic"
    return "missing"


def _real_map(
    bars: Mapping[str, Mapping[int, Bar]], assets: Sequence[tuple[str, str]]
) -> dict[str, dict[int, Bar]]:
    """Bars the window builder may see. Synthetics stay so they become holes."""
    return {asset_id: dict(bars[asset_id]) for asset_id, _mint in assets}


def _first_real(bars: Mapping[str, Mapping[int, Bar]], step_seconds: int, now: int) -> int | None:
    found: list[int] = []
    for by_time in bars.values():
        for open_time, bar in by_time.items():
            if bar.synthetic:
                continue
            if _closed_bar(bar, step_seconds, now):
                found.append(open_time)
    if not found:
        return None
    return min(found)


def _strategy(strategy_id: str, version: int, document: Mapping[str, Any]) -> PaperStrategy:
    if (
        not isinstance(strategy_id, str)
        or strategy_id == ""
        or len(strategy_id) > 128
        or any(char.isspace() for char in strategy_id)
    ):
        raise ValueError("strategy_id must be a short non-empty string")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("version must be a positive whole number")
    issues = validate_ir(document)
    if issues:
        raise ValueError(format_issues(issues))
    if not isinstance(document, dict):
        raise ValueError("strategy document must be an object")
    bar_size = document["engine"]["bar_size"]
    if bar_size not in TIMEFRAME_SECONDS:
        raise ValueError(f"live feed has no native bars for {bar_size}")
    assets: list[tuple[str, str]] = []
    for asset in document["assets"]:
        symbol = str(asset["symbol"])
        known = BY_SYMBOL.get(symbol)
        if known is None:
            raise ValueError(f"no live feed for symbol {symbol}")
        assets.append((str(asset["id"]), known.mint))
    warmup = document["engine"]["warmup_bars"]
    if isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 0:
        raise ValueError("warmup_bars must be a whole number")
    return PaperStrategy(
        strategy_id,
        version,
        dict(document),
        str(bar_size),
        warmup,
        tuple(assets),
    )


def _dump_state(state: RunnerState) -> str:
    payload = []
    for asset_id, memory in state.memory:
        payload.append(
            {
                "asset_id": asset_id,
                "cross_prev": [
                    [list(path), [left, right]] for path, (left, right) in memory.cross_prev
                ],
                "since": [[list(path), count] for path, count in memory.since],
            }
        )
    return json.dumps(payload, separators=(",", ":"))


def _load_state(text: str, asset_ids: Sequence[str]) -> RunnerState:
    raw = json.loads(text)
    by_id: dict[str, MemoryState] = {}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            cross = tuple(
                (tuple(str(part) for part in path), (pair[0], pair[1]))
                for path, pair in item.get("cross_prev", [])
            )
            since = tuple(
                (tuple(str(part) for part in path), count)
                for path, count in item.get("since", [])
            )
            by_id[str(item.get("asset_id", ""))] = MemoryState(cross, since)
    return RunnerState(
        tuple((asset_id, by_id.get(asset_id, MemoryState())) for asset_id in asset_ids)
    )


def _dump_decision(decision: BarDecision) -> str:
    oco = []
    for item in decision.oco:
        if isinstance(item, OcoDrop):
            oco.append({"kind": "drop", "asset_id": item.asset_id})
            continue
        if isinstance(item, OcoTouch):
            oco.append(
                {
                    "kind": "touch",
                    "asset_id": item.asset_id,
                    "leg": item.leg,
                    "raw_price": format(item.raw_price, "f"),
                    "limit_below_low": item.limit_below_low,
                }
            )
    rules = [
        {
            "action_id": rule.action_id,
            "asset_id": rule.asset_id,
            "skip": rule.skip,
            "close": None if rule.close is None else format(rule.close, "f"),
        }
        for rule in decision.rules
    ]
    signals = [
        {"signal_id": item.signal_id, "asset_id": item.asset_id, "is_true": item.is_true}
        for item in decision.signals
    ]
    return json.dumps(
        {"signals": signals, "oco": oco, "rules": rules}, separators=(",", ":")
    )


def open_scheduler() -> PaperScheduler:
    """Scheduler for the process. Off unless the caller constructs it.

    Birdeye is constructed from the environment. An unset key leaves it
    disabled, and this function does not read the key into a log line.
    """
    from app.ohlcv.adapters.birdeye import BirdeyeSource
    from app.ohlcv.adapters.geckoterminal import GeckoTerminalSource

    data_dir = Path(os.environ.get("OHLCV_DATA_DIR") or "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    store = SqliteBarStore(data_dir / "ohlcv.sqlite", ttl_seconds=MAX_CACHE_TTL_SECONDS)
    payloads: list[bytes] = []
    birdeye_payloads: list[bytes] = []
    primary = GeckoTerminalSource(rng=random.random, payloads=payloads)
    fallback = BirdeyeSource(rng=random.random, payloads=birdeye_payloads)
    feed = LiveFeed(store, primary, fallback, now=lambda: int(time.time()))
    return PaperScheduler(store, feed, owns_store=True, owns_feed=True)
