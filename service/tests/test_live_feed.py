"""Live bar feed and paper scheduler. Sockets stay blocked; feeds are mocks."""

from __future__ import annotations

import ast
import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import LOCAL_USER_ID
from app.live.feed import (
    CLOSE_GRACE_SECONDS,
    LiveFeed,
    bar_is_closed,
    closed_real_bars,
    last_closed_open,
)
from app.live.scheduler import PaperScheduler, register_paper_strategy
from app.live.series import build_window
from app.main import app, create_app
from app.ohlcv.adapters.birdeye import BirdeyeSource
from app.ohlcv.adapters.geckoterminal import GeckoTerminalSource
from app.ohlcv.bars import Bar
from app.ohlcv.errors import RateLimitExhausted, SourceError
from app.ohlcv.registry import SOL_MINT
from app.ohlcv.source import FetchResult
from app.ohlcv.store import MAX_CACHE_TTL_SECONDS, SqliteBarStore
from app.runner import BookView, config_from_document, initial_state
from app.runner import step as real_step

_STEP = 60
_T0 = 1_700_000_040
_DUE = _STEP + CLOSE_GRACE_SECONDS
_DAY = 86_400
_DAY_OPEN = 1_699_920_000
_KEY = "test-birdeye-key"


def _bar(
    open_time: int,
    *,
    synthetic: bool = False,
    timeframe: str = "1m",
    close: float = 1.5,
) -> Bar:
    if synthetic:
        return Bar(SOL_MINT, timeframe, open_time, close, close, close, close, 0.0, True)
    return Bar(SOL_MINT, timeframe, open_time, close, close + 0.1, close - 0.1, close, 2.0, False)


def _paper(bar_size: str = "1m") -> dict:
    return {
        "ir_version": "1.0",
        "strategy": {"id": "hand", "name": "Hand"},
        "engine": {
            "bar_size": bar_size,
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 0,
        },
        "capital": {"initial": 10000, "currency": "USDC"},
        "costs": {"commission_bps": 0, "slippage_bps": 0},
        "assets": [
            {
                "id": "sol",
                "symbol": "SOL",
                "chain": "solana",
                "market": "spot",
                "quote": "USDC",
            }
        ],
        "derived_series": [],
        "signals": [{"id": "go", "expr": {"op": "lit", "value": True}}],
        "actions": [
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 100},
                "risk": {
                    "type": "oco",
                    "evaluation": "on_entry_fill",
                    "movement": "tighten_only",
                    "take_profit": {"mode": "percent", "percent": 6},
                    "stop_loss": {"mode": "percent", "percent": 3},
                },
            }
        ],
        "rules": [{"id": "enter_rule", "when": "go", "do": "enter_sol"}],
        "portfolio": {
            "mode": "spot",
            "direction": "long_only",
            "chain": "solana",
            "max_positions": 1,
            "allow_pyramiding": False,
            "quote_currency": "USDC",
        },
    }


class _Quiet:
    """No HTTP. Bars under test are already in the store."""

    def fetch(self, mint: str, timeframe: str, start: int, end: int) -> list[Bar]:
        return []

    def close(self) -> None:
        return None


class _Scripted:
    name = "geckoterminal"
    enabled = True

    def __init__(
        self, result: FetchResult | None = None, *, error: BaseException | None = None
    ) -> None:
        self._result = result or FetchResult([], [])
        self._error = error
        self.calls = 0
        self.payloads: list[bytes] = []

    def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult:
        self.calls += 1
        self.payloads.append(b'{"synthetic":true}')
        if self._error is not None:
            raise self._error
        return self._result

    def close(self) -> None:
        return None


def _store(
    tmp_path: Path, clock: dict[str, int], *, ttl: int = MAX_CACHE_TTL_SECONDS
) -> SqliteBarStore:
    return SqliteBarStore(tmp_path / "ohlcv.sqlite", ttl_seconds=ttl, now=lambda: clock["now"])


def _scheduler(store: SqliteBarStore, feed: object, clock: dict[str, int]) -> PaperScheduler:
    return PaperScheduler(store, feed, now=lambda: clock["now"], interval=3600)


def _register(scheduler: PaperScheduler, *, version: int = 1, bar_size: str = "1m") -> None:
    register_paper_strategy(scheduler, "sol-paper", version, _paper(bar_size))


def _decisions(store: SqliteBarStore, version: int = 1) -> list[tuple[int, dict]]:
    rows = store.live_decisions(
        user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=version
    )
    return [(open_time, json.loads(payload)) for open_time, payload in rows]


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _candle(open_time: int, close: float = 1.5) -> list[float]:
    return [open_time, close, close + 0.1, close - 0.1, close, 2.0]


def _gecko_json(candles: list[list[float]]) -> dict:
    return {"data": {"attributes": {"ohlcv_list": candles}}}


def test_closed_bar_rule_uses_the_injected_clock() -> None:
    step = _STEP
    assert _T0 % step == 0
    assert CLOSE_GRACE_SECONDS >= 40
    assert bar_is_closed(_T0, step, _T0 + _DUE)
    assert not bar_is_closed(_T0, step, _T0 + _DUE - 1)
    assert last_closed_open(_T0 + 150, step) == _T0
    forming = _bar(_T0 + 2 * step)
    closed = _bar(_T0)
    kept = closed_real_bars([forming, closed, _bar(_T0 + step, synthetic=True)], "1m", _T0 + 150)
    assert [bar.open_time for bar in kept] == [_T0]
    # Probe: the bar opened at _T0+60 still has 30s left, and the clock is 40s fast.
    fast = _T0 + 90 + 40
    assert not bar_is_closed(_T0 + step, step, fast)
    assert bar_is_closed(_T0, step, fast)
    assert forming.open_time not in {
        bar.open_time for bar in closed_real_bars([forming, _bar(_T0 + step)], "1m", fast)
    }


def test_gecko_is_primary_and_birdeye_without_a_key_is_not_called(tmp_path: Path) -> None:
    clock = {"now": _T0 + _DUE}
    store = _store(tmp_path, clock)
    called = {"birdeye": 0}

    class _Off(BirdeyeSource):
        def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult:
            called["birdeye"] += 1
            raise AssertionError("birdeye was called")

    disabled = _Off(api_key=None, client=_client(lambda _request: httpx.Response(500)))
    assert disabled.enabled is False
    primary = _Scripted(FetchResult([_bar(_T0)], []))
    feed = LiveFeed(store, primary, disabled, now=lambda: clock["now"])
    try:
        bars = feed.fetch(SOL_MINT, "1m", _T0, _T0 + _STEP)
        bodies = store.live_payload_bodies(user_id=LOCAL_USER_ID)
    finally:
        store.close()
    assert called["birdeye"] == 0
    assert primary.calls == 1
    assert [bar.open_time for bar in bars] == [_T0]
    assert bodies == ['{"synthetic":true}']


def test_disabled_birdeye_is_not_called_when_gecko_fails(tmp_path: Path) -> None:
    clock = {"now": _T0 + _DUE}
    store = _store(tmp_path, clock)
    disabled = BirdeyeSource(api_key="  ", client=_client(lambda _request: httpx.Response(500)))
    assert disabled.enabled is False

    def boom(mint: str, timeframe: str, start: int, end: int) -> FetchResult:
        raise AssertionError("birdeye was called")

    disabled.fetch_bars = boom  # type: ignore[method-assign]
    primary = _Scripted(error=SourceError("GeckoTerminal returned HTTP 503"))
    feed = LiveFeed(store, primary, disabled, now=lambda: clock["now"])
    try:
        with pytest.raises(SourceError, match="HTTP 503"):
            feed.fetch(SOL_MINT, "1m", _T0, _T0 + _STEP)
    finally:
        store.close()
    assert primary.calls == 1


def test_birdeye_is_the_fallback_only_after_gecko_fails(tmp_path: Path) -> None:
    clock = {"now": _T0 + _DUE}
    store = _store(tmp_path, clock)
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["key"] = request.headers.get("X-API-KEY", "")
        return httpx.Response(
            200,
            json={
                "data": {
                    "items": [
                        {
                            "o": 1.5,
                            "h": 1.6,
                            "l": 1.4,
                            "c": 1.5,
                            "v": 2.0,
                            "unix_time": _T0,
                        }
                    ]
                }
            },
        )

    fallback = BirdeyeSource(
        api_key=_KEY,
        client=_client(handler),
        min_interval=1,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
        payloads=[],
    )
    primary = _Scripted(error=SourceError("GeckoTerminal returned HTTP 503"))
    feed = LiveFeed(store, primary, fallback, now=lambda: clock["now"])
    try:
        bars = feed.fetch(SOL_MINT, "1m", _T0, _T0 + _STEP)
        bodies = store.live_payload_bodies(user_id=LOCAL_USER_ID)
    finally:
        fallback.close()
        store.close()
    assert primary.calls == 1
    assert seen["host"] == "public-api.birdeye.so"
    assert seen["key"] == _KEY
    assert [bar.open_time for bar in bars] == [_T0]
    assert bodies
    assert _KEY not in bodies[0]
    assert _KEY not in repr(fallback)


def test_retry_after_and_attempt_cap_are_bounded(tmp_path: Path) -> None:
    clock = {"now": _T0 + _DUE}
    store = _store(tmp_path, clock)
    sleeps: list[float] = []
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(
                429,
                headers={"Retry-After": "5"},
                json={"message": "secret-token-should-not-leak"},
            )
        return httpx.Response(200, json=_gecko_json([_candle(_T0)]))

    source = GeckoTerminalSource(
        _client(handler),
        min_interval=0,
        sleep=sleeps.append,
        rng=lambda: 0.0,
        payloads=[],
    )
    feed = LiveFeed(store, source, now=lambda: clock["now"])
    try:
        bars = feed.fetch(SOL_MINT, "1m", _T0, _T0 + _STEP)
        bodies = store.live_payload_bodies(user_id=LOCAL_USER_ID)
    finally:
        source.close()
        store.close()
    assert [bar.open_time for bar in bars] == [_T0]
    assert calls["n"] == 3
    assert sleeps == [5.0, 5.0]
    assert bodies
    assert "secret-token-should-not-leak" not in bodies[0]


def test_exhausted_retries_stop_at_five(tmp_path: Path) -> None:
    clock = {"now": _T0 + _DUE}
    store = _store(tmp_path, clock)
    sleeps: list[float] = []
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "10000"}, text="secret-body")

    source = GeckoTerminalSource(
        _client(handler),
        min_interval=0,
        sleep=sleeps.append,
        rng=lambda: 0.0,
        payloads=[],
    )
    feed = LiveFeed(store, source, now=lambda: clock["now"])
    try:
        with pytest.raises(SourceError, match="HTTP 429") as caught:
            feed.fetch(SOL_MINT, "1m", _T0, _T0 + _STEP)
    finally:
        source.close()
        store.close()
    assert calls["n"] == 5
    assert sleeps == [120.0, 120.0, 120.0, 120.0]
    assert "secret-body" not in str(caught.value)


def test_timeouts_use_jitter_and_stop_at_five(tmp_path: Path) -> None:
    clock = {"now": _T0 + _DUE}
    store = _store(tmp_path, clock)
    sleeps: list[float] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out", request=request)

    source = GeckoTerminalSource(
        _client(handler),
        min_interval=0,
        sleep=sleeps.append,
        rng=lambda: 0.5,
        payloads=[],
    )
    feed = LiveFeed(store, source, now=lambda: clock["now"])
    try:
        with pytest.raises(SourceError, match="timed out"):
            feed.fetch(SOL_MINT, "1m", _T0, _T0 + _STEP)
    finally:
        source.close()
        store.close()
    assert calls["n"] == 5
    assert sleeps == [1.5, 3.0, 6.0, 12.0]


def test_bars_and_payloads_are_purged_after_24h_and_the_cursor_remains(tmp_path: Path) -> None:
    assert _DAY_OPEN % _DAY == 0
    clock = {"now": _DAY_OPEN + _DAY + CLOSE_GRACE_SECONDS}
    store = _store(tmp_path, clock, ttl=MAX_CACHE_TTL_SECONDS)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=_gecko_json([_candle(_DAY_OPEN, 3.0)]))
        return httpx.Response(200, json=_gecko_json([]))

    source = GeckoTerminalSource(
        _client(handler),
        min_interval=0,
        sleep=lambda _seconds: None,
        rng=lambda: 0.0,
        payloads=[],
    )
    feed = LiveFeed(store, source, now=lambda: clock["now"])
    scheduler = _scheduler(store, feed, clock)
    _register(scheduler, bar_size="1d")
    try:
        scheduler.tick()
        decided = _decisions(store)
        assert [open_time for open_time, _payload in decided] == [_DAY_OPEN]
        assert store.live_payload_bodies(user_id=LOCAL_USER_ID)
        cursor = store.live_cursor(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        )
        assert cursor is not None
        assert cursor[1] == _DAY_OPEN
        clock["now"] += MAX_CACHE_TTL_SECONDS
        assert (
            store.load(
                user_id=LOCAL_USER_ID,
                mint=SOL_MINT,
                timeframe="1d",
                start=0,
                end=clock["now"],
            )
            == []
        )
        assert store.live_payload_bodies(user_id=LOCAL_USER_ID) == []
        assert _decisions(store) == []
        kept = store.live_cursor(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
        assert kept is not None and kept[1] == _DAY_OPEN
        assert kept[2].strip() in {"", "null", "[]"}
        restarted = PaperScheduler(store, feed, now=lambda: clock["now"], interval=3600)
        restarted.tick()
        assert _decisions(store) == []
        again = store.live_cursor(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
        assert again is not None and again[1] == _DAY_OPEN
    finally:
        source.close()
        store.close()


def test_in_progress_and_synthetic_bars_do_not_reach_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = _T0 + 150
    clock = {"now": now}
    store = _store(tmp_path, clock)
    synthetic = _bar(_T0 + _STEP, synthetic=True)
    forming = _bar(_T0 + 2 * _STEP)
    store.upsert([synthetic], user_id=LOCAL_USER_ID)
    primary = _Scripted(FetchResult([_bar(_T0), synthetic, forming], []))
    feed = LiveFeed(store, primary, now=lambda: clock["now"])
    scheduler = _scheduler(store, feed, clock)
    _register(scheduler)
    seen: list[object] = []

    def spy(config, state, window, view):
        seen.append((window, view))
        return real_step(config, state, window, view)

    monkeypatch.setattr("app.live.scheduler.step", spy)
    try:
        scheduler.tick()
        stored = store.load(
            user_id=LOCAL_USER_ID,
            mint=SOL_MINT,
            timeframe="1m",
            start=0,
            end=now + _STEP,
        )
        decisions = _decisions(store)
    finally:
        store.close()
    assert [bar.open_time for bar in stored if not bar.synthetic] == [_T0]
    assert any(bar.synthetic and bar.open_time == _T0 + _STEP for bar in stored)
    assert forming.open_time not in {bar.open_time for bar in stored}
    assert [open_time for open_time, _payload in decisions] == [_T0]
    assert len(seen) == 1
    window, view = seen[0]
    assert window.open_time == _T0
    assert window.bars["sol"] is not None
    assert window.bars["sol"].synthetic is False
    assert view.entries == ()
    assert view.ocos == ()
    payload = decisions[0][1]
    assert payload["oco"] == []
    assert payload["rules"][0]["skip"] is None
    assert payload["rules"][0]["close"] is not None


def test_missed_and_late_bars_are_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.live")
    t1 = _T0 + _STEP
    t2 = _T0 + 2 * _STEP
    clock = {"now": _T0 + 4 * _STEP + CLOSE_GRACE_SECONDS}
    store = _store(tmp_path, clock)
    store.upsert([_bar(_T0), _bar(t2)], user_id=LOCAL_USER_ID)
    scheduler = _scheduler(store, _Quiet(), clock)
    _register(scheduler)
    try:
        scheduler.tick()
        log = store.live_bar_log(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
        assert (t1, "missed", "missing") in [(item[0], item[1], item[2]) for item in log]
        assert (t2, "late", "following bar already closed") in [
            (item[0], item[1], item[2]) for item in log
        ]
        assert [open_time for open_time, _payload in _decisions(store)] == [_T0, t2]
        assert f"missed bar strategy_id=sol-paper version=1 open_time={t1}" in caplog.text
        assert f"late bar strategy_id=sol-paper version=1 open_time={t2}" in caplog.text
        store.upsert([_bar(t1)], user_id=LOCAL_USER_ID)
        scheduler.tick()
        log = store.live_bar_log(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
        logged = [(item[0], item[1], item[2]) for item in log]
        assert (t1, "late", "cursor already passed") in logged
        assert [open_time for open_time, _payload in _decisions(store)] == [_T0, t2]
    finally:
        store.close()


def test_a_bar_is_not_evaluated_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": _T0 + _DUE}
    store = _store(tmp_path, clock)
    store.upsert([_bar(_T0)], user_id=LOCAL_USER_ID)
    scheduler = _scheduler(store, _Quiet(), clock)
    _register(scheduler)
    calls = {"n": 0}

    def spy(config, state, window, view):
        calls["n"] += 1
        return real_step(config, state, window, view)

    monkeypatch.setattr("app.live.scheduler.step", spy)
    try:
        scheduler.tick()
        scheduler.tick()
        assert calls["n"] == 1
        assert [open_time for open_time, _payload in _decisions(store)] == [_T0]
        kinds = {
            kind
            for _open_time, kind, _detail in store.live_bar_log(
                user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
            )
        }
        assert "duplicate" in kinds
    finally:
        store.close()


def test_restart_does_not_double_fire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": _T0 + _DUE}
    store = _store(tmp_path, clock)
    store.upsert([_bar(_T0)], user_id=LOCAL_USER_ID)
    first = _scheduler(store, _Quiet(), clock)
    _register(first)
    calls = {"n": 0}

    def spy(config, state, window, view):
        calls["n"] += 1
        return real_step(config, state, window, view)

    monkeypatch.setattr("app.live.scheduler.step", spy)
    try:
        first.tick()
        assert calls["n"] == 1
        second = PaperScheduler(store, _Quiet(), now=lambda: clock["now"], interval=3600)
        second.tick()
        assert calls["n"] == 1
        assert [open_time for open_time, _payload in _decisions(store)] == [_T0]
        clock["now"] = _T0 + 2 * _STEP + CLOSE_GRACE_SECONDS
        store.upsert([_bar(_T0 + _STEP)], user_id=LOCAL_USER_ID)
        second.tick()
        assert calls["n"] == 2
        assert [open_time for open_time, _payload in _decisions(store)] == [_T0, _T0 + _STEP]
    finally:
        store.close()


def test_duplicate_and_out_of_order_bars(tmp_path: Path) -> None:
    t2 = _T0 + 2 * _STEP
    clock = {"now": _T0 + 4 * _STEP + CLOSE_GRACE_SECONDS}
    store = _store(tmp_path, clock)
    batches = [
        FetchResult([_bar(t2), _bar(_T0), _bar(_T0)], []),
        FetchResult([_bar(_T0 - _STEP), _bar(t2)], []),
    ]

    class _Batches:
        name = "geckoterminal"
        payloads: list[bytes]

        def __init__(self) -> None:
            self.payloads = []
            self.calls = 0

        def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult:
            self.calls += 1
            self.payloads.append(b"{}")
            return batches.pop(0)

        def close(self) -> None:
            return None

    feed = LiveFeed(store, _Batches(), now=lambda: clock["now"])
    scheduler = _scheduler(store, feed, clock)
    _register(scheduler)
    try:
        scheduler.tick()
        assert [open_time for open_time, _payload in _decisions(store)] == [_T0, t2]
        stored = store.load(
            user_id=LOCAL_USER_ID, mint=SOL_MINT, timeframe="1m", start=0, end=clock["now"]
        )
        assert [bar.open_time for bar in stored if not bar.synthetic] == [_T0, t2]
        scheduler.tick()
        assert [open_time for open_time, _payload in _decisions(store)] == [_T0, t2]
        log = store.live_bar_log(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
        assert (_T0 - _STEP, "late") in {(item[0], item[1]) for item in log}
        assert (_T0, "duplicate") in {(item[0], item[1]) for item in log}
        assert (t2, "duplicate") in {(item[0], item[1]) for item in log}
    finally:
        store.close()


def test_versions_do_not_share_a_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": _T0 + _DUE}
    store = _store(tmp_path, clock)
    store.upsert([_bar(_T0)], user_id=LOCAL_USER_ID)
    scheduler = _scheduler(store, _Quiet(), clock)
    _register(scheduler, version=1)
    _register(scheduler, version=2)
    calls = {"n": 0}

    def spy(config, state, window, view):
        calls["n"] += 1
        return real_step(config, state, window, view)

    monkeypatch.setattr("app.live.scheduler.step", spy)
    try:
        scheduler.tick()
        scheduler.tick()
        assert calls["n"] == 2
        assert [open_time for open_time, _payload in _decisions(store, 1)] == [_T0]
        assert [open_time for open_time, _payload in _decisions(store, 2)] == [_T0]
    finally:
        store.close()


def test_health_stays_fast_while_the_scheduler_ticks(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def slow() -> None:
        entered.set()
        release.wait(5)

    store = _store(tmp_path, {"now": _T0})
    scheduler = PaperScheduler(
        store,
        _Quiet(),
        now=lambda: _T0,
        interval=3600,
        on_tick=slow,
    )
    application = create_app(live=scheduler)
    try:
        with TestClient(application) as client:
            assert entered.wait(2)
            health: dict[str, object] = {}

            def get_health() -> None:
                started = time.perf_counter()
                health["response"] = client.get("/health")
                health["elapsed"] = time.perf_counter() - started

            checker = threading.Thread(target=get_health)
            checker.start()
            checker.join(0.3)
            blocked = checker.is_alive()
            release.set()
            checker.join(2)
        assert not blocked
        response = health["response"]
        assert isinstance(response, httpx.Response)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert float(health["elapsed"]) < 0.3
        assert not scheduler.running
    finally:
        release.set()
        scheduler.stop()
        store.close()


def test_scheduler_starts_and_stops_with_lifespan(tmp_path: Path) -> None:
    store = _store(tmp_path, {"now": _T0})
    scheduler = PaperScheduler(store, _Quiet(), now=lambda: _T0, interval=3600)
    application = create_app(live=scheduler)
    try:
        with TestClient(application) as client:
            assert scheduler.running
            assert client.get("/health").json() == {"status": "ok"}
        assert not scheduler.running
    finally:
        store.close()


def test_scheduler_defaults_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVE_FEED_ENABLED", raising=False)
    assert app.state.live_scheduler is None
    fresh = create_app()
    assert fresh.state.live_scheduler is None
    for value in ("", "0", "false", "no", "off"):
        monkeypatch.setenv("LIVE_FEED_ENABLED", value)
        assert create_app().state.live_scheduler is None


def test_enabled_flag_does_not_fetch_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OHLCV_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BIRDEYE_API_KEY", raising=False)
    application = create_app(live_enabled=True)
    scheduler = application.state.live_scheduler
    assert scheduler is not None
    assert scheduler.running is False
    scheduler.stop()


def _cross_doc() -> dict:
    document = _paper()
    document["engine"]["warmup_bars"] = 3
    document["derived_series"] = [
        {
            "id": "ema_fast",
            "kind": "indicator",
            "indicator": "ema",
            "source": "close",
            "params": {"period": 2},
        }
    ]
    document["signals"] = [
        {
            "id": "go",
            "expr": {
                "op": "cross_above",
                "args": [
                    {"op": "ref", "name": "close"},
                    {"op": "ref", "name": "ema_fast"},
                ],
            },
        }
    ]
    return document


def _table_cells(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        cells: list[str] = []
        for table in tables:
            columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
            for row in connection.execute(f"SELECT * FROM {table}"):
                for column, value in zip(columns, row, strict=True):
                    cells.append(f"{table}.{column}={value}")
        return cells
    finally:
        connection.close()


class _Batches:
    name = "geckoterminal"
    enabled = True

    def __init__(self, batches: list[FetchResult | BaseException]) -> None:
        self._batches = list(batches)
        self.payloads: list[bytes] = []
        self.calls = 0

    def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult:
        self.calls += 1
        self.payloads.append(b"{}")
        if not self._batches:
            return FetchResult([], [])
        item = self._batches.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        return None


def test_purged_memory_keeps_no_prices_and_the_next_tick_warms_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closes = [10.0, 10.2, 10.1, 10.5, 10.4, 11.0, 10.8]
    last = _T0 + (len(closes) - 1) * _STEP
    clock = {"now": last + _DUE}
    store = _store(tmp_path, clock)
    first = [_bar(_T0 + index * _STEP, close=close) for index, close in enumerate(closes)]
    primary = _Batches([FetchResult(first, [])])
    feed = LiveFeed(store, primary, now=lambda: clock["now"])
    scheduler = _scheduler(store, feed, clock)
    register_paper_strategy(scheduler, "sol-paper", 1, _cross_doc())
    seen: list[int] = []
    decided_at: list[int] = []

    def spy(config, state, window, view):
        seen.append(window.t)
        result = real_step(config, state, window, view)
        if any(rule.close is not None for rule in result.decision.rules):
            decided_at.append(window.t)
        return result

    monkeypatch.setattr("app.live.scheduler.step", spy)
    try:
        scheduler.tick()
        cursor = store.live_cursor(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
        assert cursor is not None
        assert _cursor_price_floats(tmp_path / "ohlcv.sqlite") == []
        clock["now"] += MAX_CACHE_TTL_SECONDS
        assert (
            store.load(
                user_id=LOCAL_USER_ID,
                mint=SOL_MINT,
                timeframe="1m",
                start=0,
                end=clock["now"],
            )
            == []
        )
        kept = store.live_cursor(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
        assert kept is not None and kept[1] == last
        assert store.live_registrations(user_id=LOCAL_USER_ID)
        blob = "\n".join(_table_cells(tmp_path / "ohlcv.sqlite"))
        for close in closes:
            assert str(close) not in blob
        assert _cursor_price_floats(tmp_path / "ohlcv.sqlite") == []
        end = last_closed_open(clock["now"], _STEP)
        assert end is not None and end > last
        opens = [end - index * _STEP for index in range(3, -1, -1)]
        primary._batches.append(
            FetchResult(
                [_bar(stamp, close=20.0 + index) for index, stamp in enumerate(opens)],
                [],
            )
        )
        seen.clear()
        decided_at.clear()
        scheduler.tick()
        assert seen[:3] == [0, 1, 2]
        assert any(index >= 3 for index in seen)
        assert all(index >= 3 for index in decided_at)
        assert _cursor_price_floats(tmp_path / "ohlcv.sqlite") == []
    finally:
        store.close()


def test_failover_does_not_overwrite_a_decided_bar(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="app.live")
    t2 = _T0 + _STEP
    clock = {"now": _T0 + 4 * _STEP + CLOSE_GRACE_SECONDS}
    store = _store(tmp_path, clock)
    gecko = _Batches(
        [
            FetchResult([_bar(_T0, close=100.0), _bar(t2, close=101.0)], []),
            SourceError("gecko down"),
        ]
    )
    birdeye = _Batches([FetchResult([_bar(t2, close=999.0)], [])])
    birdeye.name = "birdeye"
    feed = LiveFeed(store, gecko, birdeye, now=lambda: clock["now"])
    scheduler = _scheduler(store, feed, clock)
    _register(scheduler)
    try:
        scheduler.tick()
        scheduler.tick()
        stored = store.load(
            user_id=LOCAL_USER_ID,
            mint=SOL_MINT,
            timeframe="1m",
            start=0,
            end=clock["now"],
        )
        by_close = {bar.open_time: bar.close for bar in stored}
        conflicts = store.live_bar_conflicts(user_id=LOCAL_USER_ID)
        decisions = _decisions(store)
    finally:
        store.close()
    assert by_close[_T0] == 100.0
    assert by_close[t2] == 101.0
    assert 999.0 not in by_close.values()
    assert [(item[0], item[1], item[4]) for item in conflicts] == [(t2, "birdeye", "999")]
    decided = {open_time: payload for open_time, payload in decisions}
    assert decided[t2]["rules"][0]["close"] != "999.0"
    assert "live bar conflict provider=birdeye" in caplog.text
    assert "999" not in caplog.text


def test_a_strategy_failure_is_stored_and_purged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": _T0 + _DUE}
    store = _store(tmp_path, clock)
    store.upsert([_bar(_T0)], user_id=LOCAL_USER_ID)
    scheduler = _scheduler(store, _Quiet(), clock)
    _register(scheduler)

    def boom(config, state, window, view):
        raise RuntimeError("indicator failed")

    monkeypatch.setattr("app.live.scheduler.step", boom)
    try:
        scheduler.tick()
        errors = store.live_errors(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
        assert _decisions(store) == []
        assert errors == [(_T0, "RuntimeError", "indicator failed")]
        clock["now"] += MAX_CACHE_TTL_SECONDS
        assert store.live_errors(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1) == []
    finally:
        store.close()


def test_stop_returns_while_a_tick_is_blocked(tmp_path: Path) -> None:
    started = threading.Event()
    stop = threading.Event()

    class _Block:
        def fetch(self, mint: str, timeframe: str, start: int, end: int) -> list[Bar]:
            started.set()
            stop.wait(30)
            return []

        def close(self) -> None:
            return None

    clock = {"now": _T0 + _DUE}
    store = _store(tmp_path, clock)
    scheduler = PaperScheduler(
        store,
        _Block(),
        now=lambda: clock["now"],
        interval=3600,
        stop_event=stop,
    )
    _register(scheduler)
    scheduler.start()
    try:
        assert started.wait(2)
        began = time.perf_counter()
        scheduler.stop()
        elapsed = time.perf_counter() - began
    finally:
        stop.set()
        scheduler.stop()
        store.close()
    assert elapsed < 1
    assert scheduler.running is False


def test_retry_backoff_waits_on_the_stop_event() -> None:
    stop = threading.Event()
    started = threading.Event()
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "30"})

    def sleep(seconds: float) -> None:
        started.set()
        stop.wait(seconds)

    source = GeckoTerminalSource(
        _client(handler), min_interval=0, sleep=sleep, stop=stop, rng=lambda: 0.0
    )
    outcome: dict[str, BaseException] = {}

    def run() -> None:
        try:
            source.fetch_bars(SOL_MINT, "1m", _T0, _T0 + _STEP)
        except SourceError as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert started.wait(2)
        stop.set()
        worker.join(1)
    finally:
        stop.set()
        worker.join(2)
        source.close()
    assert not worker.is_alive()
    assert calls["n"] < 5
    assert str(outcome["error"]) == "stopped"


def _cursor_price_floats(path: Path) -> list[str]:
    """Decimal tokens stored on the cursor. A blank state has none."""
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT state_json FROM live_cursors").fetchall()
    finally:
        connection.close()
    found: list[str] = []
    for (text,) in rows:
        found.extend(re.findall(r"\d+\.\d+", "" if text is None else str(text)))
    return found


_ORIGIN = 1_699_920_000


def _lit_cross(bar_size: str, threshold: float) -> dict:
    document = _paper(bar_size)
    document["signals"] = [
        {
            "id": "go",
            "expr": {
                "op": "cross_above",
                "args": [
                    {"op": "ref", "name": "close"},
                    {"op": "lit", "value": threshold},
                ],
            },
        }
    ]
    return document


def _sma_cross(bar_size: str, period: int) -> dict:
    document = _paper(bar_size)
    document["engine"]["warmup_bars"] = period
    document["derived_series"] = [
        {
            "id": "sma_slow",
            "kind": "indicator",
            "indicator": "sma",
            "source": "close",
            "params": {"period": period},
        }
    ]
    document["signals"] = [
        {
            "id": "go",
            "expr": {
                "op": "cross_above",
                "args": [
                    {"op": "ref", "name": "close"},
                    {"op": "ref", "name": "sma_slow"},
                ],
            },
        }
    ]
    return document


def _rule_fired(payload: dict) -> bool:
    return any(rule.get("close") is not None for rule in payload.get("rules", []))


def test_a_gap_does_not_fire_a_cross_from_pre_gap_memory(tmp_path: Path) -> None:
    """1h and 4h outages must not let a both-finite cross use the pre-gap close."""
    cases = (
        ("1h", 3600, 30, [10.0, 10.05, 10.1, 10.2, 10.25], 10.3, 11.0),
        ("4h", 14400, 9, [500.0, 500.01, 500.02], 500.5, 501.0),
    )
    for timeframe, step, gap_ticks, closes, threshold, new_close in cases:
        root = tmp_path / f"lit-{timeframe}"
        root.mkdir()
        clock = {"now": _ORIGIN + step + CLOSE_GRACE_SECONDS}
        store = _store(root, clock)
        scheduler = _scheduler(store, _Quiet(), clock)
        register_paper_strategy(scheduler, "sol-paper", 1, _lit_cross(timeframe, threshold))
        path = root / "ohlcv.sqlite"
        try:
            for index, close in enumerate(closes):
                open_time = _ORIGIN + index * step
                clock["now"] = open_time + step + CLOSE_GRACE_SECONDS
                store.upsert(
                    [_bar(open_time, timeframe=timeframe, close=close)],
                    user_id=LOCAL_USER_ID,
                )
                scheduler.tick()
                assert _cursor_price_floats(path) == []
                decided = _decisions(store)
                assert decided
                assert _rule_fired(decided[-1][1]) is False
            for _tick in range(gap_ticks):
                clock["now"] += step
                scheduler.tick()
                assert _cursor_price_floats(path) == []
            cursor = store.live_cursor(
                user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
            )
            assert cursor is not None
            before = {open_time for open_time, _payload in _decisions(store)}
            stamp = cursor[1] + step
            clock["now"] = stamp + step + CLOSE_GRACE_SECONDS
            store.upsert(
                [_bar(stamp, timeframe=timeframe, close=new_close)],
                user_id=LOCAL_USER_ID,
            )
            scheduler.tick()
            assert _cursor_price_floats(path) == []
            fresh = [
                payload
                for open_time, payload in _decisions(store)
                if open_time not in before
            ]
            assert fresh
            assert all(_rule_fired(payload) is False for payload in fresh)
            for payload in fresh:
                assert all(signal["is_true"] is False for signal in payload["signals"])
        finally:
            store.close()

    # The sma probe: six rising bars after a 30h 1h gap must not fire either.
    root = tmp_path / "sma-1h"
    root.mkdir()
    step = 3600
    clock = {"now": _ORIGIN + step + CLOSE_GRACE_SECONDS}
    store = _store(root, clock)
    scheduler = _scheduler(store, _Quiet(), clock)
    register_paper_strategy(scheduler, "sol-paper", 1, _sma_cross("1h", 3))
    path = root / "ohlcv.sqlite"
    try:
        for index, close in enumerate([10.0, 10.2, 10.4, 10.3, 10.25]):
            open_time = _ORIGIN + index * step
            clock["now"] = open_time + step + CLOSE_GRACE_SECONDS
            store.upsert(
                [_bar(open_time, timeframe="1h", close=close)],
                user_id=LOCAL_USER_ID,
            )
            scheduler.tick()
            assert _cursor_price_floats(path) == []
        for _tick in range(30):
            clock["now"] += step
            scheduler.tick()
            assert _cursor_price_floats(path) == []
        cursor = store.live_cursor(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
        assert cursor is not None
        before = {open_time for open_time, _payload in _decisions(store)}
        for index, close in enumerate([11.0, 11.2, 11.4, 11.5, 11.6, 11.8]):
            stamp = cursor[1] + (index + 1) * step
            clock["now"] = stamp + step + CLOSE_GRACE_SECONDS
            store.upsert(
                [_bar(stamp, timeframe="1h", close=close)],
                user_id=LOCAL_USER_ID,
            )
        scheduler.tick()
        assert _cursor_price_floats(path) == []
        fresh = [payload for open_time, payload in _decisions(store) if open_time not in before]
        assert fresh
        assert all(_rule_fired(payload) is False for payload in fresh)
    finally:
        store.close()


class _History:
    """Serves a fixed series for whatever window the scheduler asks for."""

    def __init__(self, store: SqliteBarStore, bars: list[Bar], clock: dict[str, int]) -> None:
        self.store = store
        self.bars = bars
        self.clock = clock
        self.calls: list[tuple[int, int, int]] = []

    def fetch(self, mint: str, timeframe: str, start: int, end: int) -> list[Bar]:
        self.calls.append((self.clock["now"], start, end))
        chosen = [
            bar
            for bar in self.bars
            if bar.timeframe == timeframe and start <= bar.open_time < end
        ]
        if chosen:
            self.store.upsert(chosen, user_id=LOCAL_USER_ID)
        return chosen

    def close(self) -> None:
        return None


def _wave(index: int) -> float:
    """Deterministic swing so a close can cross its SMA."""
    if (index // 8) % 2:
        return 120.0 - (index % 8) * 0.7
    return 100.0 + (index % 8) * 0.7


def _walk_decisions(document: dict, bars: list[Bar], step: int) -> dict[int, object]:
    """The backtester's per-bar decision: ``step`` from the first bar, holes included."""
    origin = bars[0].open_time
    by_time = {bar.open_time: bar for bar in bars}
    config = config_from_document(document)
    state = initial_state(("sol",))
    found: dict[int, object] = {}
    slot = origin
    while slot <= bars[-1].open_time:
        window = build_window(document, ("sol",), {"sol": by_time}, origin, slot, step)
        result = real_step(config, state, window, BookView())
        state = result.state
        found[slot] = result.decision
        slot += step
    return found


def _decision_signature(decision: object) -> tuple:
    signals = tuple(
        (item.signal_id, item.asset_id, item.is_true) for item in decision.signals  # type: ignore[attr-defined]
    )
    rules = tuple(
        (
            rule.action_id,
            rule.skip,
            None if rule.close is None else format(rule.close, "f"),
        )
        for rule in decision.rules  # type: ignore[attr-defined]
    )
    return signals, rules


def _payload_signature(payload: dict) -> tuple:
    signals = tuple(
        (item["signal_id"], item["asset_id"], item["is_true"]) for item in payload["signals"]
    )
    rules = tuple((item["action_id"], item["skip"], item["close"]) for item in payload["rules"])
    return signals, rules


def test_long_lookback_stays_ready_past_48h_and_matches_the_backtest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.backtest.engine import run_backtest

    cases = (
        ("1h", 3600, 50, 48),
        ("4h", 14400, 20, 12),
    )
    for timeframe, step, period, extra in cases:
        total = period + extra
        bars = [
            _bar(_ORIGIN + index * step, timeframe=timeframe, close=_wave(index))
            for index in range(total + 1)
        ]
        clock = {"now": _ORIGIN + step + CLOSE_GRACE_SECONDS}
        root = tmp_path / timeframe
        root.mkdir()
        store = _store(root, clock, ttl=MAX_CACHE_TTL_SECONDS)
        feed = _History(store, bars, clock)
        scheduler = _scheduler(store, feed, clock)
        document = _sma_cross(timeframe, period)
        register_paper_strategy(scheduler, "sol-paper", 1, document)
        finite = {"late": False}
        cutoff = _ORIGIN + 48 * 3600

        def spy(config, state, window, view, *, _cutoff=cutoff):
            value = window.series["sol"]["sma_slow"][window.t]
            if window.open_time >= _cutoff and value == value and abs(value) != float("inf"):
                finite["late"] = True
            return real_step(config, state, window, view)

        monkeypatch.setattr("app.live.scheduler.step", spy)
        expected = _walk_decisions(document, bars, step)
        seen: set[int] = set()
        live_closes: set[int] = set()
        try:
            for index in range(total + 1):
                clock["now"] = _ORIGIN + index * step + step + CLOSE_GRACE_SECONDS
                scheduler.tick()
                assert _cursor_price_floats(root / "ohlcv.sqlite") == []
                for open_time, payload in _decisions(store):
                    if open_time in seen:
                        continue
                    assert _payload_signature(payload) == _decision_signature(expected[open_time])
                    if _rule_fired(payload):
                        live_closes.add(open_time)
                    seen.add(open_time)
            assert seen == {bar.open_time for bar in bars}
            result = run_backtest(document, {"sol": bars})
            for fill in result.fills:
                if fill.leg == "market":
                    assert fill.open_time in live_closes
            assert finite["late"] is True
            wide = [
                end - start
                for now, start, end in feed.calls
                if now >= _ORIGIN + 24 * 3600 + step
            ]
            assert wide
            assert max(wide) >= period * step
        finally:
            monkeypatch.setattr("app.live.scheduler.step", real_step)
            store.close()


class _FailFeed:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def fetch(self, mint: str, timeframe: str, start: int, end: int) -> list[Bar]:
        self.calls += 1
        raise self.exc

    def close(self) -> None:
        return None


def test_a_failed_refetch_is_not_ready_and_decides_nothing(tmp_path: Path) -> None:
    failures = (
        SourceError("gecko down"),
        RateLimitExhausted("Birdeye rate limit exhausted"),
    )
    for index, exc in enumerate(failures):
        root = tmp_path / f"fail-{index}"
        root.mkdir()
        step = 3600
        last = _ORIGIN + 50 * step
        clock = {"now": last + step + CLOSE_GRACE_SECONDS}
        store = _store(root, clock)
        feed = _FailFeed(exc)
        scheduler = _scheduler(store, feed, clock)
        register_paper_strategy(scheduler, "sol-paper", 1, _sma_cross("1h", 50))
        try:
            scheduler.tick()
            scheduler.tick()
            errors = store.live_errors(
                user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
            )
            log = store.live_bar_log(
                user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
            )
            stored = store.load(
                user_id=LOCAL_USER_ID,
                mint=SOL_MINT,
                timeframe="1h",
                start=0,
                end=clock["now"] + step,
            )
            cursor = store.live_cursor(
                user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
            )
            decided = _decisions(store)
        finally:
            store.close()
        assert decided == []
        assert feed.calls == 2
        assert errors
        assert all(row[1] == type(exc).__name__ for row in errors)
        assert any(kind == "not_ready" for _open, kind, _detail in log)
        assert stored == []
        assert cursor is None
        assert _cursor_price_floats(root / "ohlcv.sqlite") == []


def test_live_package_has_no_broker_or_wallet_code() -> None:
    root = Path(__file__).resolve().parents[1] / "app" / "live"
    forbidden = ("wallet", "private_key", "mnemonic", "keypair", "sign_transaction")
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in text
        tree = ast.parse(text)
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
        assert "app.backtest.engine" not in modules
        assert "app.backtest" not in modules
