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
from app.ohlcv.registry import SOL_MINT
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
        clock["now"] = _T + 4 * _STEP + _GRACE
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
        clock["now"] = _T + 5 * _STEP + _GRACE
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
