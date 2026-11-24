"""Paper broker. Sockets stay blocked. Bars are built in the test."""

from __future__ import annotations

import json
import logging
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app.auth import LOCAL_USER_ID
from app.backtest import run_backtest
from app.live.feed import CLOSE_GRACE_SECONDS
from app.live.scheduler import PaperScheduler, register_paper_strategy
from app.ohlcv.bars import Bar
from app.ohlcv.errors import SourceError
from app.ohlcv.registry import JUP_MINT, SOL_MINT
from app.ohlcv.store import MAX_CACHE_TTL_SECONDS, SqliteBarStore
from app.paper.broker import ORDER_TTL_SECONDS, PaperBroker, reconcile_book

_STEP = 60
_T = 1_700_000_040
_GRACE = CLOSE_GRACE_SECONDS


class _Feed:
    def fetch(self, *_args: object, **_kwargs: object) -> list[Bar]:
        return []

    def close(self) -> None:
        return None


def _bar(
    open_time: int,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    synthetic: bool = False,
) -> Bar:
    if synthetic:
        return Bar(SOL_MINT, "1m", open_time, close, close, close, close, 0.0, True)
    return Bar(
        SOL_MINT,
        "1m",
        open_time,
        close,
        close if high is None else high,
        close if low is None else low,
        close,
        2.0,
        False,
    )


def _gt(level: int) -> dict:
    return {
        "op": "gt",
        "left": {"op": "ref", "name": "close"},
        "right": {"op": "lit", "value": level},
    }


def _document() -> dict:
    return {
        "ir_version": "1.0",
        "strategy": {"id": "hand", "name": "Hand"},
        "engine": {
            "bar_size": "1m",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 0,
        },
        "capital": {"initial": 10000, "currency": "USDC"},
        "costs": {"commission_bps": 10, "slippage_bps": 5},
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
        "signals": [
            {"id": "go", "expr": _gt(50)},
            {"id": "out", "expr": _gt(129)},
        ],
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
                    "take_profit": {"mode": "percent", "percent": 50},
                    "stop_loss": {"mode": "percent", "percent": 5},
                },
            },
            {
                "id": "exit_sol",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 100},
            },
        ],
        "rules": [
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "out", "do": "exit_sol"},
        ],
        "portfolio": {
            "mode": "spot",
            "direction": "long_only",
            "chain": "solana",
            "max_positions": 1,
            "allow_pyramiding": False,
            "quote_currency": "USDC",
        },
    }


def _always_in() -> dict:
    document = _document()
    document["signals"] = [{"id": "go", "expr": {"op": "lit", "value": True}}]
    document["rules"] = [{"id": "enter_rule", "when": "go", "do": "enter_sol"}]
    document["costs"] = {"commission_bps": 0, "slippage_bps": 0}
    return document


def _store(tmp_path: Path, clock: dict[str, int]) -> SqliteBarStore:
    return SqliteBarStore(tmp_path / "ohlcv.sqlite", now=lambda: clock["now"])


def _register(store: SqliteBarStore, clock: dict[str, int], document: dict) -> None:
    scheduler = PaperScheduler(store, _Feed(), now=lambda: clock["now"], interval=3600)
    register_paper_strategy(scheduler, "sol-paper", 1, document)


def _broker(store: SqliteBarStore, clock: dict[str, int]) -> PaperBroker:
    return PaperBroker(store, now=lambda: clock["now"])


def _kinds(store: SqliteBarStore) -> list[str]:
    return [
        kind
        for _open, kind, _detail in store.paper_events(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        )
    ]


def _details(store: SqliteBarStore) -> list[tuple[str, dict]]:
    rows = []
    for _open, kind, detail in store.paper_events(
        user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
    ):
        rows.append((kind, json.loads(detail)))
    return rows


def _positions(store: SqliteBarStore) -> list[tuple[object, ...]]:
    return store.paper_positions(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)


def _fills(store: SqliteBarStore) -> list[tuple[object, ...]]:
    return store.paper_fills(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)


def _open_positions(rows: list[tuple[object, ...]]) -> list[tuple[str, Decimal, Decimal]]:
    return [
        (str(row[0]), Decimal(str(row[1])), Decimal(str(row[3])))
        for row in rows
        if row[1] is not None
    ]


def _backtest_open(bars: list[Bar]) -> list[tuple[str, Decimal, Decimal]]:
    result = run_backtest(_document(), {"sol": bars})
    return [(item.asset_id, item.qty, item.entry_price) for item in result.final_positions]


def _assert_fills_match(bars: list[Bar], store: SqliteBarStore) -> None:
    result = run_backtest(_document(), {"sol": bars})
    paper = _fills(store)
    assert len(paper) == len(result.fills)
    for got, fill in zip(paper, result.fills, strict=True):
        assert got[0] == fill.open_time
        assert got[1] == fill.asset_id
        assert got[2] == fill.side
        assert Decimal(str(got[3])) == fill.qty
        assert Decimal(str(got[4])) == fill.price
        assert Decimal(str(got[5])) == fill.notional
        assert Decimal(str(got[6])) == fill.commission
        assert got[7] == fill.leg
        assert bool(got[8]) == fill.limit_below_low


def _assert_book_matches(bars: list[Bar], store: SqliteBarStore) -> None:
    """Fills, open positions, and booked cash match ``run_backtest`` on ``bars``.

    Cash is the booked P&L. An open long's quantity and entry are the position.
    Together with the fills they are the book the backtester would still hold.
    """
    result = run_backtest(_document(), {"sol": bars})
    _assert_fills_match(bars, store)
    assert _open_positions(_positions(store)) == _backtest_open(bars)
    wallet = store.paper_wallet(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
    assert wallet is not None
    assert Decimal(wallet[0]) == result.final_cash


def _age(path: Path, *tables: str) -> None:
    connection = sqlite3.connect(path)
    try:
        for table in tables:
            connection.execute(
                f"UPDATE {table} SET stored_at = stored_at - ?",
                (MAX_CACHE_TTL_SECONDS + 1,),
            )
        connection.commit()
    finally:
        connection.close()


def _columns(path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def test_fixture_matches_the_backtester_and_survives_the_fill_purge(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Prompt bars match the engine. After the fill log is purged, the book remains."""
    clock = {"now": _T}
    store = _store(tmp_path, clock)
    _register(store, clock, _document())
    broker = _broker(store, clock)
    slot0 = _bar(_T, 100, high=101, low=99)
    slot1 = _bar(_T + _STEP, 90, synthetic=True)
    slot3 = _bar(_T + 3 * _STEP, 110, high=112, low=100)
    caplog.set_level(logging.ERROR)
    try:
        store.upsert([slot0], user_id=LOCAL_USER_ID)
        clock["now"] = _T + _STEP + _GRACE
        broker.tick()
        assert _kinds(store) == ["SIGNAL_FIRED", "ORDER_PLACED", "ORDER_FILLED"]
        _assert_fills_match([slot0], store)
        assert _open_positions(_positions(store)) == _backtest_open([slot0])
        entry = _positions(store)[0]
        assert entry[7] is not None and entry[8] is not None and entry[9] is not None
        assert "/" in str(entry[4])

        store.upsert([slot1], user_id=LOCAL_USER_ID)
        clock["now"] = _T + 2 * _STEP + _GRACE
        broker.tick()
        _assert_fills_match([slot0, slot1], store)
        assert _open_positions(_positions(store)) == _backtest_open([slot0, slot1])
        assert _positions(store)[0][7] == entry[7]
        assert any(
            kind == "ORDER_REJECTED" and detail["reason"] == "synthetic_bar"
            for kind, detail in _details(store)
        )

        clock["now"] = _T + 3 * _STEP + _GRACE
        broker.tick()
        store.upsert([slot3], user_id=LOCAL_USER_ID)
        # The empty minute between the synthetic bar and slot3 stays open
        # until the order TTL after its close. slot3 itself is still inside
        # that window, so this tick steps the gap and then reads slot3.
        clock["now"] = (_T + 2 * _STEP) + _STEP + ORDER_TTL_SECONDS + 1
        broker.tick()
        held = [slot0, slot1, slot3]
        _assert_fills_match(held, store)
        assert _open_positions(_positions(store)) == _backtest_open(held)
        wallet = store.paper_wallet(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
        assert wallet is not None
        identity, replay = reconcile_book(
            cash=wallet[0],
            initial=wallet[1],
            positions=_positions(store),
            fills=_fills(store),
            marks={"sol": Decimal("110")},
        )
        assert identity is True
        assert replay is True
        qty_before = _positions(store)[0][1]

        _age(store.path, "paper_fills", "paper_orders", "paper_events")
        assert _fills(store) == []
        assert store.paper_orders(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1) == []
        assert _kinds(store) == []
        assert _positions(store)[0][1] == qty_before
        identity, replay = reconcile_book(
            cash=wallet[0],
            initial=wallet[1],
            positions=_positions(store),
            fills=_fills(store),
            marks={"sol": Decimal("110")},
        )
        assert identity is True
        assert replay is False
        restarted = _broker(store, clock)
        restarted.tick()
        assert _positions(store)[0][1] == qty_before
        assert _fills(store) == []

        slot4 = _bar(_T + 4 * _STEP, 130, high=131, low=120)
        store.upsert([slot4], user_id=LOCAL_USER_ID)
        # Same instant: slot4 is closed, and it is still inside its order TTL.
        restarted.tick()
        finished = [slot0, slot1, slot3, slot4]
        result = run_backtest(_document(), {"sol": finished})
        paper = _fills(store)
        assert len(paper) == 1
        exit_fill = result.fills[-1]
        assert paper[0][0] == exit_fill.open_time
        assert paper[0][2] == exit_fill.side
        assert Decimal(str(paper[0][3])) == exit_fill.qty
        assert Decimal(str(paper[0][4])) == exit_fill.price
        assert Decimal(str(paper[0][5])) == exit_fill.notional
        assert Decimal(str(paper[0][6])) == exit_fill.commission
        assert paper[0][7] == exit_fill.leg
        assert result.final_positions == ()
        assert _open_positions(_positions(store)) == []
        flat = _positions(store)[0]
        assert flat[1] is None
        assert flat[2] is None
        assert flat[3] is None
        assert flat[4] is None
        assert flat[6] is None
        assert flat[7] is None
        assert flat[8] is None
        assert flat[9] is None
        assert flat[10] is None
        assert flat[5] not in {None, "0/1"}
        wallet = store.paper_wallet(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
        assert wallet is not None
        identity, _replay = reconcile_book(
            cash=wallet[0],
            initial=wallet[1],
            positions=_positions(store),
            fills=_fills(store),
            marks={},
        )
        assert identity is True

        for table in ("paper_positions", "paper_wallets"):
            columns = _columns(store.path, table)
            assert columns.isdisjoint({"open", "high", "low", "close", "volume", "body", "payload"})
        _age(store.path, "paper_fills", "paper_orders", "paper_events", "bars", "live_decisions")
        assert _fills(store) == []
        assert _positions(store)[0][5] == flat[5]
        assert store.paper_wallet(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == wallet
    finally:
        store.close()
    assert "paper tick failed" not in caplog.text


def test_a_late_closed_bar_is_stored_after_the_tick_and_matches_backtest(
    tmp_path: Path,
) -> None:
    """Probe C. The tick that should decide a closed bar does not pass it.

    The bar is not in the store yet, and the following bar is still open, so
    the cursor stays on the previous bar. Once the bar is stored, the next
    tick fills it and the fill matches ``run_backtest``.
    """
    prior = _bar(_T, 40, high=41, low=39)
    late = _bar(_T + _STEP, 100, high=101, low=99)
    clock = {"now": _T}
    store = _store(tmp_path, clock)
    _register(store, clock, _document())
    store.upsert([prior], user_id=LOCAL_USER_ID)
    broker = _broker(store, clock)
    try:
        clock["now"] = _T + 2 * _STEP + _GRACE
        broker.tick()
        assert store.paper_cursor(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == _T
        assert _fills(store) == []
        log = store.live_bar_log(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
        assert (_T + _STEP, "not_ready", "bar not stored") in log

        store.upsert([late], user_id=LOCAL_USER_ID)
        broker.tick()
        _assert_fills_match([prior, late], store)
        assert store.paper_cursor(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == _T + _STEP
    finally:
        store.close()


def test_a_failed_fetch_does_not_advance_past_an_unstored_bar(tmp_path: Path) -> None:
    """``hold`` is the scheduler's not_ready path. The hole is not skipped."""
    prior = _bar(_T, 40, high=41, low=39)
    late = _bar(_T + _STEP, 100, high=101, low=99)
    clock = {"now": _T}
    store = _store(tmp_path, clock)
    _register(store, clock, _document())
    store.upsert([prior], user_id=LOCAL_USER_ID)
    broker = _broker(store, clock)
    try:
        clock["now"] = _T + _STEP + _GRACE
        broker.tick()
        # The following bar is closed. A failed fetch still waits, including
        # after the order TTL, and it does not declare a hole.
        clock["now"] = _T + 4 * _STEP + _GRACE
        broker.tick(hold={(SOL_MINT, "1m")})
        late_close = late.open_time + _STEP
        clock["now"] = late_close + ORDER_TTL_SECONDS + 1
        broker.tick(hold={(SOL_MINT, "1m")})
        assert store.paper_cursor(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == _T
        assert _fills(store) == []
        assert "BAR_MISSED" not in _kinds(store)

        clock["now"] = late_close + ORDER_TTL_SECONDS
        store.upsert([late], user_id=LOCAL_USER_ID)
        broker.tick()
        _assert_fills_match([prior, late], store)
    finally:
        store.close()


def test_a_confirmed_hole_is_not_a_fill(tmp_path: Path) -> None:
    """A missing slot waits through the order TTL, then steps with no fill.

    The following bar being closed is not enough. At the close plus
    ``ORDER_TTL_SECONDS`` the cursor is still on the previous bar. One second
    later the empty slot is ``BAR_MISSED`` and the stored bar after it fills.
    """
    entered = _bar(_T, 100, high=101, low=99)
    after = _bar(_T + 2 * _STEP, 110, high=112, low=100)
    hole = _T + _STEP
    hole_close = hole + _STEP
    clock = {"now": _T}
    store = _store(tmp_path, clock)
    _register(store, clock, _document())
    broker = _broker(store, clock)
    try:
        store.upsert([entered], user_id=LOCAL_USER_ID)
        clock["now"] = _T + _STEP + _GRACE
        broker.tick()
        store.upsert([after], user_id=LOCAL_USER_ID)
        clock["now"] = hole_close + ORDER_TTL_SECONDS
        broker.tick()
        assert store.paper_cursor(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == _T
        assert [fill[0] for fill in _fills(store)] == [_T]
        assert "BAR_MISSED" not in _kinds(store)

        clock["now"] = hole_close + ORDER_TTL_SECONDS + 1
        broker.tick()
        assert [fill[0] for fill in _fills(store)] == [_T]
        missed = [
            (open_time, json.loads(detail))
            for open_time, kind, detail in store.paper_events(
                user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
            )
            if kind == "BAR_MISSED"
        ]
        assert missed == [(hole, {"reason": "ttl"})]
        _assert_book_matches([entered, after], store)
        assert store.paper_cursor(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == _T + 2 * _STEP
    finally:
        store.close()


def test_a_bar_two_bars_late_within_the_order_ttl_matches_backtest(tmp_path: Path) -> None:
    """A bar omitted by a successful fetch still fills if it lands inside the TTL.

    The following bar is already closed, with order time left. Paper on the
    final stored series matches ``run_backtest``: entry on the late bar, the
    stop on the next bar, and the re-entry.
    """
    prior = _bar(_T, 40, high=41, low=39)
    late = _bar(_T + _STEP, 100, high=101, low=99)
    following = _bar(_T + 2 * _STEP, 60, high=61, low=59)
    clock = {"now": _T}
    store = _store(tmp_path, clock)
    _register(store, clock, _document())
    store.upsert([prior], user_id=LOCAL_USER_ID)
    broker = _broker(store, clock)
    try:
        clock["now"] = _T + 2 * _STEP + _GRACE
        broker.tick(hold=set())
        assert store.paper_cursor(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == _T
        assert _fills(store) == []

        store.upsert([following], user_id=LOCAL_USER_ID)
        clock["now"] = _T + 3 * _STEP + _GRACE
        assert clock["now"] - (late.open_time + _STEP) < ORDER_TTL_SECONDS
        broker.tick(hold=set())
        assert store.paper_cursor(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == _T
        assert _fills(store) == []
        assert "BAR_MISSED" not in _kinds(store)

        store.upsert([late], user_id=LOCAL_USER_ID)
        broker.tick(hold=set())
        _assert_book_matches([prior, late, following], store)
        assert any(fill[0] == late.open_time for fill in _fills(store))
    finally:
        store.close()


def test_a_bar_two_bars_late_through_the_scheduler_matches_backtest(tmp_path: Path) -> None:
    """The scheduler stores a late bar before the broker gives the slot up."""
    prior = _bar(_T, 40, high=41, low=39)
    late = _bar(_T + _STEP, 100, high=101, low=99)
    following = _bar(_T + 2 * _STEP, 60, high=61, low=59)
    clock = {"now": _T}
    store = _store(tmp_path, clock)
    store.upsert([prior], user_id=LOCAL_USER_ID)
    calls = {"n": 0}

    class _LateFeed:
        def fetch(self, *_args: object, **_kwargs: object) -> list[Bar]:
            calls["n"] += 1
            # The page that first sees the following bar closed still omits
            # the late slot. The page after that includes it.
            if calls["n"] <= 2:
                chosen = [bar for bar in (prior, following) if bar.open_time < clock["now"]]
            else:
                chosen = [prior, late, following]
            store.upsert(chosen, user_id=LOCAL_USER_ID)
            return chosen

        def close(self) -> None:
            return None

    scheduler = PaperScheduler(store, _LateFeed(), now=lambda: clock["now"], interval=3600)
    register_paper_strategy(scheduler, "sol-paper", 1, _document())
    try:
        clock["now"] = _T + 2 * _STEP + _GRACE
        scheduler.tick()
        assert store.paper_cursor(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == _T
        clock["now"] = _T + 3 * _STEP + _GRACE
        assert clock["now"] - (late.open_time + _STEP) < ORDER_TTL_SECONDS
        scheduler.tick()
        assert store.paper_cursor(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == _T
        assert _fills(store) == []
        scheduler.tick()
        _assert_book_matches([prior, late, following], store)
    finally:
        store.close()


def test_a_permanent_gap_advances_after_the_order_ttl(tmp_path: Path) -> None:
    """After the order TTL an empty slot is ``BAR_MISSED`` and the book moves on."""
    entered = _bar(_T, 100, high=101, low=99)
    after = _bar(_T + 2 * _STEP, 110, high=112, low=100)
    hole = _T + _STEP
    clock = {"now": _T}
    store = _store(tmp_path, clock)
    _register(store, clock, _document())
    broker = _broker(store, clock)
    try:
        store.upsert([entered, after], user_id=LOCAL_USER_ID)
        clock["now"] = _T + _STEP + _GRACE
        broker.tick()
        assert [fill[0] for fill in _fills(store)] == [_T]
        clock["now"] = hole + _STEP + ORDER_TTL_SECONDS
        broker.tick()
        assert store.paper_cursor(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == _T
        assert "BAR_MISSED" not in _kinds(store)

        clock["now"] = hole + _STEP + ORDER_TTL_SECONDS + 1
        broker.tick()
        missed = [
            (open_time, json.loads(detail))
            for open_time, kind, detail in store.paper_events(
                user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
            )
            if kind == "BAR_MISSED"
        ]
        assert missed == [(hole, {"reason": "ttl"})]
        assert hole not in {fill[0] for fill in _fills(store)}
        _assert_book_matches([entered, after], store)
        assert store.paper_cursor(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == after.open_time
    finally:
        store.close()


def test_the_scheduler_stores_the_fetch_before_the_broker_ticks(tmp_path: Path) -> None:
    """C4. One tick is feed fetch, then the store, then the broker."""
    bar = _bar(_T, 100, high=101, low=99)
    clock = {"now": _T}
    events: list[str] = []
    store = _store(tmp_path, clock)

    class _Feed:
        def fetch(self, *_args: object, **_kwargs: object) -> list[Bar]:
            events.append("fetch")
            store.upsert([bar], user_id=LOCAL_USER_ID)
            return [bar]

        def close(self) -> None:
            return None

    class _Broker(PaperBroker):
        def __init__(self, store: SqliteBarStore, clock: dict[str, int]) -> None:
            super().__init__(store, now=lambda: clock["now"])

        def tick(self, **kwargs: object) -> None:
            stored = store.load(
                user_id=LOCAL_USER_ID,
                mint=SOL_MINT,
                timeframe="1m",
                start=0,
                end=bar.open_time + _STEP,
            )
            assert any(item.open_time == bar.open_time for item in stored)
            events.append("broker")
            super().tick(**kwargs)  # type: ignore[arg-type]

    scheduler = PaperScheduler(
        store,
        _Feed(),
        now=lambda: clock["now"],
        interval=3600,
        broker=_Broker(store, clock),
    )
    register_paper_strategy(scheduler, "sol-paper", 1, _document())
    clock["now"] = _T + _STEP + _GRACE
    try:
        scheduler.tick()
        assert events == ["fetch", "broker"]
        _assert_fills_match([bar], store)
    finally:
        store.close()


def test_a_failed_scheduler_fetch_does_not_let_the_broker_pass_the_bar(
    tmp_path: Path,
) -> None:
    """The broker waits when the feed raises, then fills the bar it stores later."""
    prior = _bar(_T, 40, high=41, low=39)
    late = _bar(_T + _STEP, 100, high=101, low=99)
    clock = {"now": _T}
    store = _store(tmp_path, clock)
    store.upsert([prior], user_id=LOCAL_USER_ID)

    class _Feed:
        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, *_args: object, **_kwargs: object) -> list[Bar]:
            self.calls += 1
            if self.calls == 1:
                raise SourceError("not yet")
            store.upsert([late], user_id=LOCAL_USER_ID)
            return [late]

        def close(self) -> None:
            return None

    scheduler = PaperScheduler(store, _Feed(), now=lambda: clock["now"], interval=3600)
    register_paper_strategy(scheduler, "sol-paper", 1, _document())
    clock["now"] = _T + 4 * _STEP + _GRACE
    try:
        scheduler.tick()
        assert store.paper_cursor(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == _T
        assert _fills(store) == []
        scheduler.tick()
        _assert_fills_match([prior, late], store)
    finally:
        store.close()


def test_a_bar_closed_before_registration_is_decided_and_not_filled(
    tmp_path: Path,
) -> None:
    clock = {"now": _T + _STEP + _GRACE}
    store = _store(tmp_path, clock)
    store.upsert([_bar(_T, 100, high=101, low=99)], user_id=LOCAL_USER_ID)
    scheduler = PaperScheduler(store, _Feed(), now=lambda: clock["now"], interval=3600)
    register_paper_strategy(scheduler, "sol-paper", 1, _always_in())
    broker = _broker(store, clock)
    try:
        scheduler.tick()
        broker.tick()
        decided = store.live_decisions(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        )
        assert [open_time for open_time, _payload in decided] == [_T]
        assert _fills(store) == []
        assert _positions(store) == []

        store.upsert(
            [
                _bar(_T + _STEP, 101, high=102, low=100),
                _bar(_T + 2 * _STEP, 102, high=103, low=101),
            ],
            user_id=LOCAL_USER_ID,
        )
        clock["now"] = _T + 3 * _STEP + _GRACE
        scheduler.tick()
        broker.tick()
        decided = store.live_decisions(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        )
        decided_at = {open_time for open_time, _payload in decided}
        assert {_T, _T + _STEP, _T + 2 * _STEP} <= decided_at
        fills = _fills(store)
        assert [fill[0] for fill in fills] == [_T + 2 * _STEP]
    finally:
        store.close()


def test_a_late_tick_expires_the_order_and_does_not_fill(tmp_path: Path) -> None:
    """A 1d bar is the only closed slot 301s after it closes, and that is too late."""
    day = 1_699_920_000
    clock = {"now": day}
    store = _store(tmp_path, clock)
    document = _always_in()
    document["engine"]["bar_size"] = "1d"
    _register(store, clock, document)
    clock["now"] = day + 86400 + ORDER_TTL_SECONDS + 1
    store.upsert(
        [Bar(SOL_MINT, "1d", day, 100, 101, 99, 100, 2.0, False)],
        user_id=LOCAL_USER_ID,
    )
    broker = _broker(store, clock)
    try:
        broker.tick()
        assert _fills(store) == []
        wallet = store.paper_wallet(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
        assert wallet is not None
        assert Decimal(wallet[0]) == Decimal(wallet[1])
        assert _kinds(store) == ["SIGNAL_FIRED", "ORDER_PLACED", "ORDER_EXPIRED"]
        orders = store.paper_orders(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
        assert orders[0][3] == "expired"
        assert store.paper_open_orders(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == []
    finally:
        store.close()


def test_one_open_order_blocks_a_second_until_it_expires(tmp_path: Path) -> None:
    clock = {"now": _T}
    store = _store(tmp_path, clock)
    _register(store, clock, _always_in())
    store.insert_paper_order(
        user_id=LOCAL_USER_ID,
        strategy_id="sol-paper",
        version=1,
        open_time=_T,
        asset_id="sol",
        side="buy",
        status="open",
        placed_at=_T,
        expires_at=_T + 150,
        leg="market",
        reason="resting",
    )
    store.upsert(
        [
            _bar(_T, 100, high=101, low=99),
            _bar(_T + _STEP, 101, high=102, low=100),
        ],
        user_id=LOCAL_USER_ID,
    )
    broker = _broker(store, clock)
    try:
        clock["now"] = _T + _STEP + _GRACE
        broker.tick()
        assert _fills(store) == []
        assert store.paper_open_orders(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        )
        rejected = [
            detail
            for kind, detail in _details(store)
            if kind == "ORDER_REJECTED" and detail["reason"] == "open_order"
        ]
        assert rejected

        clock["now"] = _T + 2 * _STEP + _GRACE
        broker.tick()
        fills = _fills(store)
        assert [fill[0] for fill in fills] == [_T + _STEP]
        assert store.paper_open_orders(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        ) == []
        assert any(kind == "ORDER_EXPIRED" for kind in _kinds(store))
        assert sum(kind == "ORDER_FILLED" for kind in _kinds(store)) == 1
    finally:
        store.close()


def test_orders_and_events_purge_with_the_bar_cache_and_the_book_does_not(
    tmp_path: Path,
) -> None:
    clock = {"now": _T}
    store = _store(tmp_path, clock)
    _register(store, clock, _always_in())
    store.upsert([_bar(_T, 100, high=101, low=99)], user_id=LOCAL_USER_ID)
    clock["now"] = _T + _STEP + _GRACE
    broker = _broker(store, clock)
    try:
        broker.tick()
        assert _fills(store)
        _age(
            store.path,
            "paper_fills",
            "paper_orders",
            "paper_events",
            "bars",
            "live_decisions",
        )
        assert _fills(store) == []
        assert store.paper_orders(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1) == []
        assert _kinds(store) == []
        assert store.load(
            user_id=LOCAL_USER_ID,
            mint=SOL_MINT,
            timeframe="1m",
            start=0,
            end=clock["now"],
        ) == []
        assert _positions(store)[0][1] is not None
        wallet = store.paper_wallet(
            user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1
        )
        assert wallet is not None
        assert store.paper_cursor(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1) == _T
    finally:
        store.close()


def _asset(asset_id: str, symbol: str) -> dict:
    return {
        "id": asset_id,
        "symbol": symbol,
        "chain": "solana",
        "market": "spot",
        "quote": "USDC",
    }


def _enter_action(action_id: str, asset_id: str, percent: int) -> dict:
    return {
        "id": action_id,
        "type": "enter",
        "side": "long",
        "asset": asset_id,
        "order": {"type": "market"},
        "sizing": {"mode": "percent_equity", "percent": percent},
        "risk": {
            "type": "oco",
            "evaluation": "on_entry_fill",
            "movement": "tighten_only",
            "take_profit": {"mode": "percent", "percent": 50},
            "stop_loss": {"mode": "percent", "percent": 5},
        },
    }


def _enter_only(
    assets: list[dict],
    signals: list[dict],
    actions: list[dict],
    rules: list[dict],
    max_positions: int,
) -> dict:
    document = _document()
    document["assets"] = assets
    document["signals"] = signals
    document["actions"] = actions
    document["rules"] = rules
    document["portfolio"]["max_positions"] = max_positions
    return document


def _candle(
    mint: str,
    open_time: int,
    close: float,
    *,
    high: float,
    low: float,
    open_: float,
) -> Bar:
    return Bar(mint, "1m", open_time, open_, high, low, close, 2.0, False)


def _tick_at(
    store: SqliteBarStore,
    broker: PaperBroker,
    clock: dict[str, int],
    bars: list[Bar],
) -> None:
    for open_time in sorted({bar.open_time for bar in bars}):
        batch = [bar for bar in bars if bar.open_time == open_time]
        store.upsert(batch, user_id=LOCAL_USER_ID)
        clock["now"] = open_time + _STEP + _GRACE
        broker.tick()


def _assert_matches(
    store: SqliteBarStore,
    document: dict,
    series: dict[str, list[Bar]],
    marks: dict[str, Decimal],
) -> None:
    result = run_backtest(document, series)
    paper = _fills(store)
    assert len(paper) == len(result.fills)
    for got, fill in zip(paper, result.fills, strict=True):
        assert got[0] == fill.open_time
        assert got[1] == fill.asset_id
        assert got[2] == fill.side
        assert Decimal(str(got[3])) == fill.qty
        assert Decimal(str(got[4])) == fill.price
        assert Decimal(str(got[5])) == fill.notional
        assert Decimal(str(got[6])) == fill.commission
        assert got[7] == fill.leg
        assert bool(got[8]) == fill.limit_below_low
    expected = sorted(
        (item.asset_id, item.qty, item.entry_price) for item in result.final_positions
    )
    assert sorted(_open_positions(_positions(store))) == expected
    wallet = store.paper_wallet(user_id=LOCAL_USER_ID, strategy_id="sol-paper", version=1)
    assert wallet is not None
    identity, replay = reconcile_book(
        cash=wallet[0],
        initial=wallet[1],
        positions=_positions(store),
        fills=paper,
        marks=marks,
    )
    assert identity is True
    assert replay is True
    open_ids = {asset_id for asset_id, _qty, _entry in expected}
    for row in _positions(store):
        if str(row[0]) not in open_ids:
            continue
        assert row[1] is not None
        assert row[2] is not None
        assert row[3] is not None
        assert row[4] is not None and "/" in str(row[4])
        assert row[7] is not None
        assert row[8] is not None
        assert row[9] is not None
        assert row[10] is not None


def test_same_bar_oco_reenter_keeps_the_new_long(tmp_path: Path) -> None:
    """An OCO exit and a same-bar re-buy leave the new long on the durable row."""
    document = _enter_only(
        [_asset("sol", "SOL")],
        [{"id": "go", "expr": _gt(50)}],
        [_enter_action("enter_sol", "sol", 100)],
        [{"id": "enter_rule", "when": "go", "do": "enter_sol"}],
        1,
    )
    cases = (
        (
            _candle(SOL_MINT, _T + _STEP, 94, high=96, low=93, open_=96),
            _candle(SOL_MINT, _T + 2 * _STEP, 96, high=97, low=95, open_=95),
            _candle(SOL_MINT, _T + 3 * _STEP, 97, high=98, low=96, open_=96),
        ),
        (
            _candle(SOL_MINT, _T + _STEP, 151, high=152, low=100, open_=100),
            _candle(SOL_MINT, _T + 2 * _STEP, 155, high=160, low=150, open_=152),
            _candle(SOL_MINT, _T + 3 * _STEP, 156, high=158, low=152, open_=155),
        ),
    )
    for index, (touch, hold, later) in enumerate(cases):
        clock = {"now": _T}
        root = tmp_path / f"case-{index}"
        root.mkdir()
        store = _store(root, clock)
        _register(store, clock, document)
        broker = _broker(store, clock)
        try:
            enter = _candle(SOL_MINT, _T, 100, high=101, low=99, open_=100)
            _tick_at(store, broker, clock, [enter, touch])
            _assert_matches(
                store,
                document,
                {"sol": [enter, touch]},
                {"sol": Decimal(str(touch.close))},
            )
            _tick_at(store, broker, clock, [hold])
            _assert_matches(
                store,
                document,
                {"sol": [enter, touch, hold]},
                {"sol": Decimal(str(hold.close))},
            )
            restarted = _broker(store, clock)
            _tick_at(store, restarted, clock, [later])
            _assert_matches(
                store,
                document,
                {"sol": [enter, touch, hold, later]},
                {"sol": Decimal(str(later.close))},
            )
        finally:
            store.close()


def test_same_bar_oco_reenter_keeps_sol_beside_jup(tmp_path: Path) -> None:
    """SOL stopped and re-bought on one bar stays in the book next to JUP."""
    document = _enter_only(
        [_asset("sol", "SOL"), _asset("jup", "JUP")],
        [{"id": "go_sol", "expr": _gt(50)}, {"id": "go_jup", "expr": _gt(80)}],
        [_enter_action("enter_sol", "sol", 40), _enter_action("enter_jup", "jup", 40)],
        [
            {"id": "r_sol", "when": "go_sol", "do": "enter_sol"},
            {"id": "r_jup", "when": "go_jup", "do": "enter_jup"},
        ],
        2,
    )
    sol0 = _candle(SOL_MINT, _T, 100, high=101, low=99, open_=100)
    jup0 = _candle(JUP_MINT, _T, 40, high=41, low=39, open_=40)
    sol1 = _candle(SOL_MINT, _T + _STEP, 101, high=102, low=100, open_=101)
    jup1 = _candle(JUP_MINT, _T + _STEP, 90, high=91, low=89, open_=90)
    sol2 = _candle(SOL_MINT, _T + 2 * _STEP, 94, high=96, low=93, open_=96)
    jup2 = _candle(JUP_MINT, _T + 2 * _STEP, 92, high=93, low=89, open_=90)
    sol3 = _candle(SOL_MINT, _T + 3 * _STEP, 96, high=97, low=95, open_=95)
    jup3 = _candle(JUP_MINT, _T + 3 * _STEP, 93, high=94, low=91, open_=92)
    sol4 = _candle(SOL_MINT, _T + 4 * _STEP, 97, high=98, low=96, open_=96)
    jup4 = _candle(JUP_MINT, _T + 4 * _STEP, 94, high=95, low=92, open_=93)
    clock = {"now": _T}
    store = _store(tmp_path, clock)
    _register(store, clock, document)
    broker = _broker(store, clock)
    try:
        first = [sol0, jup0, sol1, jup1, sol2, jup2]
        _tick_at(store, broker, clock, first)
        series = {"sol": [sol0, sol1, sol2], "jup": [jup0, jup1, jup2]}
        _assert_matches(
            store,
            document,
            series,
            {"sol": Decimal("94"), "jup": Decimal("92")},
        )
        _tick_at(store, broker, clock, [sol3, jup3])
        held = {"sol": [sol0, sol1, sol2, sol3], "jup": [jup0, jup1, jup2, jup3]}
        _assert_matches(store, document, held, {"sol": Decimal("96"), "jup": Decimal("93")})
        restarted = _broker(store, clock)
        _tick_at(store, restarted, clock, [sol4, jup4])
        finished = {
            "sol": [sol0, sol1, sol2, sol3, sol4],
            "jup": [jup0, jup1, jup2, jup3, jup4],
        }
        _assert_matches(
            store,
            document,
            finished,
            {"sol": Decimal("97"), "jup": Decimal("94")},
        )
    finally:
        store.close()
