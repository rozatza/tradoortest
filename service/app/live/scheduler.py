"""Paper scheduler. One background thread, one decision per closed bar.

The thread is not the request event loop. ``GET /health`` does not wait for
a tick. Each tick asks the feed for bars the lookback does not already
hold, then rebuilds runner memory by calling ``step`` from the initial
state over that lookback, holes included. Warmup 0 still keeps the bar
before the decision, and a recursive indicator keeps a convergence window
long enough that the dropped tail does not change the decision. The cursor
stores the grid origin and the last open time. It does not store a price.

A new scheduler on the same file continues after the last open time, so a
restart does not repeat a bar and does not skip one that was committed.
Memory is rebuilt from the bars still in the cache, not from a saved blob.

Missed slots are logged once the following bar is already closed. Until
then the tick waits, so a bar that is only a poll late is still decided
once. A real bar that shows up after the cursor has moved past its open
time is logged late and is not decided. Synthetic bars are not passed to
the runner as a close. A hole inside the lookback is stepped, so a cross
cannot use a close from the other side of the gap.

The book view passed to ``step`` is empty. This module records the decision.
It does not fill, attach an OCO, or send an order.
"""

from __future__ import annotations

import json
import logging
import math
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
from app.live.feed import LiveFeed, bar_is_closed, last_closed_open
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
    StepResult,
    config_from_document,
    initial_state,
    step,
)

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
        stop_event: threading.Event | None = None,
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
        self._stop = threading.Event() if stop_event is None else stop_event
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
            strategies = self._load()
            if self._stop.is_set():
                return
            errors = self._prefetch(strategies, now)
            if errors is None:
                return
            for strategy in strategies:
                if self._stop.is_set():
                    return
                try:
                    self._tick_one(strategy, now, errors)
                except Exception as exc:
                    self._record_error(strategy, 0, exc)
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
            target=self._loop, name="live-bar-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to leave and wait up to ``join_timeout``.

        The thread is a daemon. Backoff sleeps that share ``stop_event``
        wake up as soon as this is called, and the tick checks the flag
        between strategies. A tick that ignores the flag still unblocks
        this call when the join times out.
        """
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

    def _tick_one(
        self,
        strategy: PaperStrategy,
        now: int,
        fetch_errors: Mapping[tuple[str, str], BaseException],
    ) -> None:
        step_seconds = TIMEFRAME_SECONDS[strategy.bar_size]
        closed_through = last_closed_open(now, step_seconds)
        if closed_through is None:
            return
        for _asset_id, mint in strategy.assets:
            failure = fetch_errors.get((mint, strategy.bar_size))
            if failure is not None:
                self._not_ready(strategy, closed_through, failure)
                return
        cursor = self._store.live_cursor(
            user_id=self._user_id,
            strategy_id=strategy.strategy_id,
            version=strategy.version,
        )
        bars = self._load_bars(strategy, closed_through + step_seconds)
        asset_ids = tuple(asset_id for asset_id, _mint in strategy.assets)
        if cursor is None:
            origin = _first_real(bars, step_seconds, now)
            if origin is None:
                return
            next_time = origin
            last_handled: int | None = None
        else:
            origin, last_handled, _state_json = cursor
            next_time = last_handled + step_seconds
        decided: set[int] = set()
        while next_time <= closed_through:
            if self._stop.is_set():
                return
            kind = _classify(bars, strategy.assets, next_time, step_seconds, now)
            if kind == "in_progress":
                break
            if kind == "missing":
                if _following_closed(next_time, step_seconds, now):
                    self._advance_skip(strategy, origin, next_time, "missed", "missing")
                    last_handled = next_time
                    next_time += step_seconds
                    continue
                break
            if kind != "real":
                self._advance_skip(strategy, origin, next_time, "skipped", "synthetic_bar")
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
                last_handled = next_time
                next_time += step_seconds
                continue
            late = _following_closed(next_time, step_seconds, now)
            try:
                result = self._replay(
                    strategy, bars, asset_ids, next_time, step_seconds
                )
            except Exception as exc:
                self._record_error(strategy, next_time, exc)
                logger.warning(
                    "live decision failed strategy_id=%s version=%s open_time=%s",
                    strategy.strategy_id,
                    strategy.version,
                    next_time,
                )
                return
            if result is None:
                return
            wrote = self._store.commit_live_step(
                user_id=self._user_id,
                strategy_id=strategy.strategy_id,
                version=strategy.version,
                origin=origin,
                open_time=next_time,
                state_json="",
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
            else:
                self._note(strategy, next_time, "duplicate", "already evaluated")
            last_handled = next_time
            next_time += step_seconds
        if last_handled is not None:
            self._scan_behind(strategy, bars, last_handled, decided, step_seconds, now)

    def _replay(
        self,
        strategy: PaperStrategy,
        bars: Mapping[str, Mapping[int, Bar]],
        asset_ids: Sequence[str],
        open_time: int,
        step_seconds: int,
    ) -> StepResult | None:
        """Rebuild memory for one bar from the lookback, holes included.

        The window starts at the lookback, not at the cursor origin, and
        every slot up to ``open_time`` is stepped from the initial state.
        A missing slot is a hole. Nothing carried from before the window
        is used. ``None`` means the tick was asked to stop.
        """
        needed = _bars_needed(strategy)
        window_origin = _lookback_origin(open_time, step_seconds, needed)
        state = initial_state(asset_ids)
        config = config_from_document(strategy.document)
        mapped = _real_map(bars, strategy.assets)
        result: StepResult | None = None
        slot = window_origin
        while slot <= open_time:
            if self._stop.is_set():
                return None
            result = step(
                config,
                state,
                build_window(
                    strategy.document,
                    asset_ids,
                    mapped,
                    window_origin,
                    slot,
                    step_seconds,
                ),
                BookView(),
            )
            state = result.state
            slot += step_seconds
        return result

    def _record_error(self, strategy: PaperStrategy, open_time: int, exc: BaseException) -> None:
        message = " ".join(str(exc).split())
        if len(message) > 200:
            message = message[:200]
        try:
            self._store.log_live_error(
                user_id=self._user_id,
                strategy_id=strategy.strategy_id,
                version=strategy.version,
                open_time=open_time,
                exc_type=type(exc).__name__,
                message=message,
            )
        except Exception:
            logger.warning(
                "live error row was not stored strategy_id=%s version=%s",
                strategy.strategy_id,
                strategy.version,
            )

    def _prefetch(
        self, strategies: Sequence[PaperStrategy], now: int
    ) -> dict[tuple[str, str], BaseException] | None:
        """Fetch each missing lookback once. ``None`` means the tick stopped.

        One call per mint and timeframe. The window is the union of the
        spans those strategies still need. A provider error is recorded and
        the strategy later goes not-ready. An empty page is not an error.
        """
        spans: dict[tuple[str, str], tuple[int, int]] = {}
        for strategy in strategies:
            if self._stop.is_set():
                return None
            step_seconds = TIMEFRAME_SECONDS[strategy.bar_size]
            closed_through = last_closed_open(now, step_seconds)
            if closed_through is None:
                continue
            cursor = self._store.live_cursor(
                user_id=self._user_id,
                strategy_id=strategy.strategy_id,
                version=strategy.version,
            )
            bars = self._load_bars(strategy, closed_through + step_seconds)
            start = _fetch_start(strategy, bars, cursor, closed_through, step_seconds, now)
            end = closed_through + step_seconds
            for _asset_id, mint in strategy.assets:
                key = (mint, strategy.bar_size)
                previous = spans.get(key)
                if previous is None:
                    spans[key] = (start, end)
                else:
                    spans[key] = (min(previous[0], start), max(previous[1], end))
        errors: dict[tuple[str, str], BaseException] = {}
        for (mint, timeframe), (start, end) in spans.items():
            if self._stop.is_set():
                return None
            try:
                self._feed.fetch(mint, timeframe, start, end)
            except SourceError as exc:
                if self._stop.is_set() or str(exc) == "stopped":
                    return None
                errors[(mint, timeframe)] = exc
                logger.warning(
                    "live feed failed mint=%s timeframe=%s",
                    mint,
                    timeframe,
                )
        return errors

    def _not_ready(
        self, strategy: PaperStrategy, open_time: int, exc: BaseException
    ) -> None:
        """Record a failed re-fetch. The cursor stays put and no bar is decided."""
        self._record_error(strategy, open_time, exc)
        self._note(strategy, open_time, "not_ready", type(exc).__name__)
        logger.warning(
            "live feed not ready strategy_id=%s version=%s open_time=%s",
            strategy.strategy_id,
            strategy.version,
            open_time,
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
        kind: str,
        detail: str,
    ) -> None:
        self._store.commit_live_skip(
            user_id=self._user_id,
            strategy_id=strategy.strategy_id,
            version=strategy.version,
            origin=origin,
            open_time=open_time,
            state_json="",
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
    return bar_is_closed(bar.open_time, step_seconds, now)


def _following_closed(open_time: int, step_seconds: int, now: int) -> bool:
    """True once the bar after ``open_time`` is closed, including the grace."""
    return bar_is_closed(open_time + step_seconds, step_seconds, now)


def _classify(
    bars: Mapping[str, Mapping[int, Bar]],
    assets: Sequence[tuple[str, str]],
    open_time: int,
    step_seconds: int,
    now: int,
) -> str:
    if not bar_is_closed(open_time, step_seconds, now):
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


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 0
    return value


def _decay_steps(kept: float) -> int:
    """Steps that leave a seed weighing about ``e^-20``.

    ``kept`` is the fraction of the previous value the smoother carries
    forward. ``kept ** steps ≈ e^-20``.
    """
    if kept <= 0.0 or kept >= 1.0:
        return 0
    return math.ceil(20.0 / -math.log(kept))


def _ema_window(period: int, warmup: int) -> int:
    """Bars so an EMA tail dropped before the window weighs about ``e^-20``.

    An EMA keeps ``(period - 1) / (period + 1)`` of its previous value.
    The window is also at least ``warmup + 10 * period``. The extra bar
    after the decay is the bar before the decision, so that bar is
    converged and not only the decision bar.
    """
    if period < 1:
        return 0
    steps = 0 if period <= 1 else _decay_steps((period - 1) / (period + 1))
    return max(warmup + 10 * period, period + steps + 1)


def _wilder_window(period: int) -> int:
    """Bars so a Wilder tail (RSI, ATR) dropped before the window is about ``e^-20``.

    The smoother keeps ``(period - 1) / period``. The first value sits
    ``period`` bars into the run, and the bar before the decision has to
    be that many steps past the seed.
    """
    if period < 1:
        return 0
    steps = 0 if period <= 1 else _decay_steps((period - 1) / period)
    return period + steps + 2


def _indicator_window(spec: Mapping[str, Any], warmup: int) -> int:
    """Replay length for one derived series, counted through the decision bar."""
    params = spec.get("params")
    if not isinstance(params, dict):
        params = {}
    kind = str(spec.get("indicator") or "")
    period = _positive_int(params.get("period"))
    fast = _positive_int(params.get("fast"))
    slow = _positive_int(params.get("slow"))
    signal = _positive_int(params.get("signal"))
    k_period = _positive_int(params.get("k_period"))
    d_period = _positive_int(params.get("d_period"))
    if kind == "ema":
        return _ema_window(period, warmup)
    if kind in {"rsi", "atr"}:
        return _wilder_window(period)
    if kind == "macd":
        # The line is an EMA pair and the signal is an EMA of that line.
        # The longest period sets both the e^-20 tail and the 10× floor.
        return _ema_window(max(fast, slow, signal), warmup)
    if kind == "stochastic":
        # %D is an SMA of unsmoothed %K, so the window is the rolling length.
        if k_period < 1 or d_period < 1:
            return 0
        return k_period + d_period
    if kind in {"sma", "bollinger"}:
        return period + 1 if period >= 1 else 0
    span = max(period, fast, slow, signal, k_period, d_period)
    return span + 1 if span >= 1 else 0


def _bars_needed(strategy: PaperStrategy) -> int:
    """Closed bars in one replay, including the bar before the decision.

    ``warmup_bars`` 0 still keeps that previous bar, so a cross can fire.
    Each indicator on the IR extends the window: rolling indicators by
    their period, recursive ones by a convergence window from their own
    smoothing constant.
    """
    warmup = strategy.warmup_bars
    needed = max(warmup, 1) + 1
    series = strategy.document.get("derived_series")
    if isinstance(series, list):
        for spec in series:
            if isinstance(spec, dict):
                needed = max(needed, _indicator_window(spec, warmup))
    return needed


def _lookback_origin(open_time: int, step_seconds: int, needed: int) -> int:
    start = open_time - (needed - 1) * step_seconds
    if start < 0:
        return 0
    return start


def _slot_real(
    bars: Mapping[str, Mapping[int, Bar]],
    asset_id: str,
    open_time: int,
    step_seconds: int,
    now: int,
) -> bool:
    bar = bars[asset_id].get(open_time)
    return bar is not None and not bar.synthetic and _closed_bar(bar, step_seconds, now)


def _fetch_start(
    strategy: PaperStrategy,
    bars: Mapping[str, Mapping[int, Bar]],
    cursor: tuple[int, int, str] | None,
    closed_through: int,
    step_seconds: int,
    now: int,
) -> int:
    """First open time the provider still needs to cover, once per asset."""
    needed = _bars_needed(strategy)
    lookback = _lookback_origin(closed_through, step_seconds, needed)
    missing: int | None = None
    for asset_id, _mint in strategy.assets:
        slot = lookback
        while slot <= closed_through:
            if not _slot_real(bars, asset_id, slot, step_seconds, now):
                missing = slot if missing is None else min(missing, slot)
                break
            slot += step_seconds
    if missing is not None:
        return missing
    if cursor is None:
        return lookback
    return cursor[1]


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
    stop = threading.Event()

    def sleep(seconds: float) -> None:
        if stop.wait(seconds):
            raise SourceError("stopped")

    primary = GeckoTerminalSource(
        rng=random.random, payloads=payloads, sleep=sleep, stop=stop
    )
    fallback = BirdeyeSource(
        rng=random.random, payloads=birdeye_payloads, sleep=sleep, stop=stop
    )
    feed = LiveFeed(store, primary, fallback, now=lambda: int(time.time()))
    return PaperScheduler(
        store, feed, owns_store=True, owns_feed=True, stop_event=stop
    )
