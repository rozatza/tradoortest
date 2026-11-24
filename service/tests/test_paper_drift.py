"""Backtest versus paper drift. Sockets stay blocked. The report is not stored."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from tests.test_paper_broker import _GRACE, _STEP, _age
from tests.test_paper_deploy import (
    _T,
    _bar,
    _deploy,
    _document,
    _events,
    _fills,
    _revise,
    _save,
    _undeploy,
    _world,
)

from app.auth import LOCAL_USER_ID
from app.ohlcv.registry import SOL_MINT
from app.ohlcv.store import MAX_CACHE_TTL_SECONDS
from app.paper.broker import ORDER_TTL_SECONDS, PaperBroker
from app.paper.drift import format_amount, paper_drift_threshold


def test_amount_text_is_a_decimal() -> None:
    assert format_amount(Fraction(-250749, 50)) == "-5014.98"
    assert format_amount(Fraction(1, 2)) == "0.50"
    assert format_amount(Fraction(41, 2)) == "20.50"
    assert format_amount(Decimal("0")) == "0.00"


def test_the_threshold_blank_is_one_cent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAPER_DRIFT_THRESHOLD", raising=False)
    assert paper_drift_threshold() == Decimal("0.01")
    assert paper_drift_threshold("  ") == Decimal("0.01")
    assert paper_drift_threshold("0") == Decimal("0")
    assert paper_drift_threshold("1.5") == Decimal("1.5")
    with pytest.raises(ValueError):
        paper_drift_threshold("-1")
    with pytest.raises(ValueError):
        paper_drift_threshold("00.1")
    with pytest.raises(ValueError):
        paper_drift_threshold("1.123456789")


def test_a_matching_window_is_not_flagged(tmp_path: Path) -> None:
    client, strategies, bars, _clock, strategy_id = _running(tmp_path)
    try:
        before = len(_events(bars, strategy_id))
        body = client.get("/paper").json()
        assert len(_events(bars, strategy_id)) == before
        report = body["drift"][0]
        assert report["strategy_id"] == strategy_id
        assert report["status"] == "ok"
        assert report["flagged"] is False
        assert report["reason"] is None
        assert report["fill_count"] == {"paper": 1, "backtest": 1}
        assert report["side_time_mismatches"] == 0
        assert Decimal(report["price_delta"]) == 0
        assert report["realized_pnl"]["paper"] == "0.00"
        assert report["realized_pnl"]["backtest"] == "0.00"
        assert report["realized_pnl"]["delta"] == "0.00"
        assert report["costs"] == {"commission_bps": "10", "slippage_bps": "5"}
        assert body["drift_threshold"] == "0.01"
        assert body["drift_threshold_name"] == "PAPER_DRIFT_THRESHOLD"
        text = str(body["drift"])
        for banned in ("wallet", "private_key", "signature", "swap"):
            assert banned not in text
        names = _tables(bars.path)
        assert not any("drift" in name for name in names)
    finally:
        bars.close()
        strategies.close()


def test_the_pinned_document_is_the_one_that_is_compared(tmp_path: Path) -> None:
    client, strategies, bars, _clock, strategy_id = _running(tmp_path)
    try:
        edited = _document()
        edited["signals"][0]["expr"]["right"]["value"] = 1000
        _revise(strategies, strategy_id, edited)
        report = client.get("/paper").json()["drift"][0]
        assert report["status"] == "ok"
        assert report["flagged"] is False
        assert report["fill_count"] == {"paper": 1, "backtest": 1}
    finally:
        bars.close()
        strategies.close()


def test_a_changed_fill_price_is_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAPER_DRIFT_THRESHOLD", raising=False)
    client, strategies, bars, _clock, strategy_id = _running(tmp_path)
    try:
        price = Decimal(str(_fills(bars, strategy_id)[0][4]))
        _set_price(bars.path, format(price + Decimal("0.5"), "f"))
        report = client.get("/paper").json()["drift"][0]
        assert report["status"] == "drift"
        assert report["flagged"] is True
        assert report["fill_count"] == {"paper": 1, "backtest": 1}
        assert report["side_time_mismatches"] == 0
        assert Decimal(report["price_delta"]) == Decimal("0.5")
        assert Decimal(report["realized_pnl"]["delta"]) == 0
        monkeypatch.setenv("PAPER_DRIFT_THRESHOLD", "1")
        inside = client.get("/paper").json()["drift"][0]
        assert inside["status"] == "ok"
        assert inside["flagged"] is False
        assert inside["threshold"] == "1.00"
        assert Decimal(inside["price_delta"]) == Decimal("0.5")
    finally:
        bars.close()
        strategies.close()


def test_a_fresh_deploy_with_no_bars_has_no_window(tmp_path: Path) -> None:
    clock = {"now": _T}
    client, strategies, bars, _application = _world(tmp_path, clock)
    strategy_id, _version, _hash = _save(strategies, _document())
    try:
        assert _deploy(client, strategy_id).status_code == 201
        assert (
            bars.paper_cursor(user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1) is None
        )
        report = client.get("/paper").json()["drift"][0]
        assert report["status"] == "insufficient_data"
        assert report["reason"] == "no_window"
        assert report["flagged"] is False
        assert report["fill_count"] is None
        assert report["side_time_mismatches"] is None
        assert report["price_delta"] is None
        assert report["realized_pnl"] is None
    finally:
        bars.close()
        strategies.close()


def test_stored_bars_before_the_cursor_advances_have_no_window(tmp_path: Path) -> None:
    clock = {"now": _T}
    client, strategies, bars, _application = _world(tmp_path, clock)
    strategy_id, _version, _hash = _save(strategies, _document())
    try:
        assert _deploy(client, strategy_id).status_code == 201
        bars.upsert([_bar(_T, 100, high=101, low=99)], user_id=LOCAL_USER_ID)
        stored = bars.load(
            user_id=LOCAL_USER_ID,
            mint=SOL_MINT,
            timeframe="1m",
            start=_T,
            end=_T + _STEP,
        )
        assert stored
        assert (
            bars.paper_cursor(user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1) is None
        )
        report = client.get("/paper").json()["drift"][0]
        assert report["status"] == "insufficient_data"
        assert report["reason"] == "no_window"
        assert report["flagged"] is False
        assert report["fill_count"] is None
        assert report["side_time_mismatches"] is None
        assert report["price_delta"] is None
        assert report["realized_pnl"] is None
    finally:
        bars.close()
        strategies.close()


def test_a_real_window_with_no_fills_stays_ok(tmp_path: Path) -> None:
    clock = {"now": _T}
    client, strategies, bars, _application = _world(tmp_path, clock)
    strategy_id, _version, _hash = _save(strategies, _document())
    try:
        assert _deploy(client, strategy_id).status_code == 201
        broker = PaperBroker(bars, now=lambda: clock["now"])
        bars.upsert([_bar(_T, 40, high=41, low=39)], user_id=LOCAL_USER_ID)
        clock["now"] = _T + _STEP + _GRACE
        broker.tick()
        assert bars.paper_cursor(user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1) == _T
        assert _fills(bars, strategy_id) == []
        report = client.get("/paper").json()["drift"][0]
        assert report["status"] == "ok"
        assert report["flagged"] is False
        assert report["reason"] is None
        assert report["fill_count"] == {"paper": 0, "backtest": 0}
        assert report["side_time_mismatches"] == 0
    finally:
        bars.close()
        strategies.close()


def test_warmup_bars_cached_before_deploy_match(tmp_path: Path) -> None:
    clock = {"now": _T}
    client, strategies, bars, _application = _world(tmp_path, clock)
    document = _document()
    document["engine"]["warmup_bars"] = 3
    strategy_id, _version, _hash = _save(strategies, document)
    try:
        cached = [
            _bar(_T + index * _STEP, 100 + index, high=101 + index, low=99 + index)
            for index in range(5)
        ]
        bars.upsert(cached, user_id=LOCAL_USER_ID)
        clock["now"] = _T + 5 * _STEP
        assert _deploy(client, strategy_id).status_code == 201
        broker = PaperBroker(bars, now=lambda: clock["now"])
        for index in range(5, 9):
            bars.upsert(
                [_bar(_T + index * _STEP, 100 + index, high=101 + index, low=99 + index)],
                user_id=LOCAL_USER_ID,
            )
            clock["now"] = _T + (index + 1) * _STEP + _GRACE
            broker.tick()
        assert [(fill[0] - _T) // _STEP for fill in _fills(bars, strategy_id)] == [5]
        report = client.get("/paper").json()["drift"][0]
        assert report["status"] == "ok"
        assert report["flagged"] is False
        assert report["reason"] is None
        assert report["fill_count"] == {"paper": 1, "backtest": 1}
        assert report["side_time_mismatches"] == 0
        assert Decimal(report["price_delta"]) == 0
    finally:
        bars.close()
        strategies.close()


def test_a_redeployed_book_is_not_drift(tmp_path: Path) -> None:
    clock = {"now": _T}
    client, strategies, bars, _application = _world(tmp_path, clock)
    strategy_id, _version, _hash = _save(strategies, _document())
    try:
        assert _deploy(client, strategy_id).status_code == 201
        broker = PaperBroker(bars, now=lambda: clock["now"])

        def step(index: int) -> None:
            bars.upsert(
                [_bar(_T + index * _STEP, 100 + index, high=101 + index, low=99 + index)],
                user_id=LOCAL_USER_ID,
            )
            clock["now"] = _T + (index + 1) * _STEP + _GRACE
            broker.tick()

        step(0)
        step(1)
        assert client.get("/paper").json()["drift"][0]["status"] == "ok"
        assert _undeploy(client, strategy_id).json()["position_state"] == "open_unmanaged"
        step(2)
        clock["now"] = _T + 3 * _STEP + _GRACE
        assert _deploy(client, strategy_id).status_code == 201
        for index in (3, 4, 5):
            step(index)
        assert [(fill[0] - _T) // _STEP for fill in _fills(bars, strategy_id)] == [0]
        report = client.get("/paper").json()["drift"][0]
        assert report["status"] == "insufficient_data"
        assert report["reason"] == "prior_book"
        assert report["flagged"] is False
        assert report["fill_count"] is None
        assert report["status"] != "drift"
    finally:
        bars.close()
        strategies.close()


def test_a_short_warmup_history_is_insufficient(tmp_path: Path) -> None:
    clock = {"now": _T}
    client, strategies, bars, _application = _world(tmp_path, clock)
    document = _document()
    document["engine"]["warmup_bars"] = 14
    strategy_id, _version, _hash = _save(strategies, document)
    try:
        assert _deploy(client, strategy_id).status_code == 201
        broker = PaperBroker(bars, now=lambda: clock["now"])
        for index in range(3):
            bars.upsert(
                [_bar(_T + index * _STEP, 100 + index, high=101 + index, low=99 + index)],
                user_id=LOCAL_USER_ID,
            )
            clock["now"] = _T + (index + 1) * _STEP + _GRACE
            broker.tick()
        report = client.get("/paper").json()["drift"][0]
        assert report["status"] == "insufficient_data"
        assert report["reason"] == "short_history"
        assert report["flagged"] is False
        assert report["fill_count"] is None
        assert report["status"] != "drift"
    finally:
        bars.close()
        strategies.close()


def test_a_bar_missed_before_this_registration_is_ignored(tmp_path: Path) -> None:
    client, strategies, bars, clock, strategy_id = _running(tmp_path)
    try:
        connection = sqlite3.connect(bars.path)
        try:
            connection.execute(
                """
                INSERT INTO paper_events (
                    user_id, strategy_id, version, open_time, kind, detail, stored_at
                ) VALUES (?, ?, 1, 0, 'BAR_MISSED', '{}', ?)
                """,
                (LOCAL_USER_ID, strategy_id, clock["now"]),
            )
            connection.commit()
        finally:
            connection.close()
        report = client.get("/paper").json()["drift"][0]
        assert report["status"] == "ok"
        assert report["reason"] is None
        assert report["flagged"] is False
    finally:
        bars.close()
        strategies.close()


def test_bar_missed_is_insufficient_data(tmp_path: Path) -> None:
    clock = {"now": _T}
    client, strategies, bars, _application = _world(tmp_path, clock)
    strategy_id, _version, _hash = _save(strategies, _document())
    try:
        assert _deploy(client, strategy_id).status_code == 201
        broker = PaperBroker(bars, now=lambda: clock["now"])
        bars.upsert([_bar(_T, 100, high=101, low=99)], user_id=LOCAL_USER_ID)
        clock["now"] = _T + _STEP + _GRACE
        broker.tick()
        missed_open = _T + _STEP
        clock["now"] = missed_open + _STEP + ORDER_TTL_SECONDS + 1
        broker.tick()
        report = client.get("/paper").json()["drift"][0]
        assert report["status"] == "insufficient_data"
        assert report["reason"] == "bar_missed"
        assert report["flagged"] is False
        assert report["fill_count"] is None
        assert report["side_time_mismatches"] is None
        assert report["price_delta"] is None
        assert report["realized_pnl"] is None
        assert report["status"] != "ok"
        assert report["status"] != "drift"
    finally:
        bars.close()
        strategies.close()


def test_purged_fills_are_insufficient_data(tmp_path: Path) -> None:
    client, strategies, bars, _clock, strategy_id = _running(tmp_path)
    try:
        assert _fills(bars, strategy_id)
        _age(bars.path, "paper_fills")
        body = client.get("/paper").json()
        report = body["drift"][0]
        assert _fills(bars, strategy_id) == []
        positions = bars.paper_positions(user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1)
        assert positions[0][1] is not None
        assert report["status"] == "insufficient_data"
        assert report["reason"] == "purged_fills"
        assert report["flagged"] is False
        assert report["fill_count"] is None
        assert report["price_delta"] is None
        assert report["realized_pnl"] is None
    finally:
        bars.close()
        strategies.close()


def test_a_window_before_the_retained_data_is_insufficient(tmp_path: Path) -> None:
    client, strategies, bars, clock, strategy_id = _running(tmp_path)
    try:
        assert _fills(bars, strategy_id)
        connection = sqlite3.connect(bars.path)
        try:
            connection.execute(
                "UPDATE live_registrations SET registered_at = ? WHERE user_id = ?",
                (clock["now"] - MAX_CACHE_TTL_SECONDS - 1, LOCAL_USER_ID),
            )
            connection.commit()
        finally:
            connection.close()
        still = bars.load(
            user_id=LOCAL_USER_ID,
            mint=SOL_MINT,
            timeframe="1m",
            start=_T,
            end=_T + _STEP,
        )
        assert still
        report = client.get("/paper").json()["drift"][0]
        assert report["status"] == "insufficient_data"
        assert report["reason"] == "window_before_retained"
        assert report["flagged"] is False
        assert report["fill_count"] is None
        assert report["side_time_mismatches"] is None
        assert report["price_delta"] is None
        assert report["realized_pnl"] is None
    finally:
        bars.close()
        strategies.close()


def test_events_page_forward_from_a_cursor(tmp_path: Path) -> None:
    client, strategies, bars, _clock, strategy_id = _running(tmp_path)
    try:
        stored = list(_events(bars, strategy_id))
        assert len(stored) >= 2
        newest = client.get("/paper", params={"events_limit": "1"}).json()
        assert len(newest["events"]) == 1
        assert newest["events_has_more"] is True
        assert newest["events"][0]["kind"] == stored[-1][1]
        assert newest["events_cursor"] == newest["events"][0]["id"]
        caught_up = client.get(
            "/paper",
            params={"events_since": str(newest["events_cursor"]), "events_limit": "50"},
        ).json()
        assert caught_up["events"] == []
        assert caught_up["events_has_more"] is False
        assert caught_up["events_cursor"] == newest["events_cursor"]

        walked = []
        since = 0
        for _page in range(len(stored) + 1):
            body = client.get(
                "/paper", params={"events_since": str(since), "events_limit": "1"}
            ).json()
            if not body["events"]:
                assert body["events_has_more"] is False
                break
            assert len(body["events"]) == 1
            walked.append(body["events"][0])
            assert body["events_cursor"] == body["events"][0]["id"]
            since = body["events_cursor"]
            if not body["events_has_more"]:
                break
        assert [(event["open_time"], event["kind"], event["detail"]) for event in walked] == stored
        assert [event["id"] for event in walked] == sorted(event["id"] for event in walked)

        rejected = client.get("/paper", params={"events_limit": "0"})
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "invalid_query"
        duplicate = client.get("/paper?events_limit=1&events_limit=2")
        assert duplicate.status_code == 422
    finally:
        bars.close()
        strategies.close()


def test_an_unusable_threshold_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPER_DRIFT_THRESHOLD", "nope")
    client, strategies, bars, _application = _world(tmp_path, {"now": _T})
    try:
        response = client.get("/paper")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "service_unavailable"
    finally:
        bars.close()
        strategies.close()


def _running(tmp_path: Path):
    clock = {"now": _T}
    client, strategies, bars, _application = _world(tmp_path, clock)
    strategy_id, _version, _hash = _save(strategies, _document())
    assert _deploy(client, strategy_id).status_code == 201
    broker = PaperBroker(bars, now=lambda: clock["now"])
    bars.upsert([_bar(_T, 100, high=101, low=99)], user_id=LOCAL_USER_ID)
    clock["now"] = _T + _STEP + _GRACE
    broker.tick()
    return client, strategies, bars, clock, strategy_id


def _set_price(path: Path, price: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE paper_fills SET price = ? WHERE user_id = ?",
            (price, LOCAL_USER_ID),
        )
        connection.commit()
    finally:
        connection.close()


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
    finally:
        connection.close()

