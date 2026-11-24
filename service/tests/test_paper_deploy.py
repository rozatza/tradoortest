"""Paper deploy and undeploy. Sockets stay blocked. No wallet and no fill is invented."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.test_paper_broker import _GRACE, _STEP, _T, _bar, _document

from app.auth import LOCAL_USER_ID, CurrentUser, get_current_user
from app.main import create_app
from app.ohlcv.routes import OhlcvRuntime
from app.ohlcv.store import SqliteBarStore
from app.paper.limits import DEFAULT_PAPER_MAX_RUNNING, paper_max_running
from app.strategies.compile import compile_body
from app.strategies.store import StrategyStore


class _Quiet:
    def close(self) -> None:
        return None


def _world(tmp_path: Path, clock: dict[str, int]):
    bars = SqliteBarStore(tmp_path / "ohlcv.sqlite", now=lambda: clock["now"])
    strategies = StrategyStore(tmp_path / "strategies.sqlite")
    application = create_app(
        strategies=strategies,
        ohlcv=OhlcvRuntime(bars, _Quiet(), lambda: clock["now"]),
    )
    client = TestClient(application)
    return client, strategies, bars, application


def _save(strategies: StrategyStore, document: dict) -> tuple[str, int, str]:
    compiled = compile_body(json.dumps({"ir": document}).encode())
    record, version = strategies.create(LOCAL_USER_ID, compiled)
    return record.strategy_id, version.version, version.ir_hash


def _revise(strategies: StrategyStore, strategy_id: str, document: dict) -> str:
    compiled = compile_body(json.dumps({"ir": document}).encode())
    _record, version = strategies.add_version(LOCAL_USER_ID, strategy_id, compiled)
    return version.ir_hash


def _deploy(client: TestClient, strategy_id: str, version: int = 1):
    return client.post(f"/strategies/{strategy_id}/versions/{version}/paper")


def _undeploy(client: TestClient, strategy_id: str, version: int = 1):
    return client.delete(f"/strategies/{strategy_id}/versions/{version}/paper")


def _positions(bars: SqliteBarStore, strategy_id: str):
    return bars.paper_positions(user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1)


def _fills(bars: SqliteBarStore, strategy_id: str):
    return bars.paper_fills(user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1)


def _orders(bars: SqliteBarStore, strategy_id: str):
    return bars.paper_orders(user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1)


def _events(bars: SqliteBarStore, strategy_id: str):
    return bars.paper_events(user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1)


def test_the_default_running_cap_is_three(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAPER_MAX_RUNNING", raising=False)
    assert DEFAULT_PAPER_MAX_RUNNING == 3
    assert paper_max_running() == 3
    assert paper_max_running("3") == 3
    with pytest.raises(ValueError):
        paper_max_running("0")
    with pytest.raises(ValueError):
        paper_max_running("nope")


def test_deploy_pins_the_saved_version_and_a_later_edit_does_not_change_it(
    tmp_path: Path,
) -> None:
    clock = {"now": _T}
    client, strategies, bars, _application = _world(tmp_path, clock)
    strategy_id, version, ir_hash = _save(strategies, _document())
    try:
        created = _deploy(client, strategy_id, version)
        assert created.status_code == 201
        assert created.headers["location"].endswith(f"/versions/{version}/paper")
        body = created.json()
        assert body["status"] == "running"
        assert body["ir_hash"] == ir_hash
        assert body["registered_at"] == _T
        assert body["version"] == 1

        again = _deploy(client, strategy_id, version)
        assert again.status_code == 200
        assert again.json()["registered_at"] == _T

        edited = _document()
        edited["signals"][0]["expr"]["right"]["value"] = 1000
        later_hash = _revise(strategies, strategy_id, edited)
        assert later_hash != ir_hash

        pinned = bars.live_registrations(user_id=LOCAL_USER_ID)
        assert len(pinned) == 1
        assert pinned[0][0] == strategy_id
        assert pinned[0][1] == 1
        assert json.loads(pinned[0][2])["signals"][0]["expr"]["right"]["value"] == 50

        from app.paper.broker import PaperBroker

        broker = PaperBroker(bars, now=lambda: clock["now"])
        candle = _bar(_T, 100, high=101, low=99)
        bars.upsert([candle], user_id=LOCAL_USER_ID)
        clock["now"] = _T + _STEP + _GRACE
        broker.tick()
        fills = _fills(bars, strategy_id)
        assert len(fills) == 1
        assert fills[0][0] == _T
        assert _positions(bars, strategy_id)[0][1] is not None
    finally:
        bars.close()
        strategies.close()


def test_a_bar_that_closed_before_deploy_is_not_filled(tmp_path: Path) -> None:
    clock = {"now": _T + _STEP + _GRACE}
    client, strategies, bars, _application = _world(tmp_path, clock)
    strategy_id, _version, _ir_hash = _save(strategies, _document())
    before = _bar(_T, 100, high=101, low=99)
    after = _bar(_T + 2 * _STEP, 100, high=101, low=99)
    try:
        deployed = _deploy(client, strategy_id)
        assert deployed.status_code == 201
        assert deployed.json()["registered_at"] == clock["now"]
        bars.upsert([before, after], user_id=LOCAL_USER_ID)
        from app.paper.broker import PaperBroker

        broker = PaperBroker(bars, now=lambda: clock["now"])
        clock["now"] = _T + 3 * _STEP + _GRACE
        broker.tick()
        fills = _fills(bars, strategy_id)
        assert [fill[0] for fill in fills] == [_T + 2 * _STEP]
    finally:
        bars.close()
        strategies.close()


def test_the_running_cap_is_a_typed_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPER_MAX_RUNNING", "2")
    clock = {"now": _T}
    client, strategies, bars, _application = _world(tmp_path, clock)
    first, _version, _hash = _save(strategies, _document())
    second, _version, _hash = _save(strategies, _document())
    third, _version, _hash = _save(strategies, _document())
    try:
        assert _deploy(client, first).status_code == 201
        assert _deploy(client, second).status_code == 201
        blocked = _deploy(client, third)
        assert blocked.status_code == 409
        assert blocked.json()["error"] == {
            "code": "paper_limit",
            "message": "paper strategy limit reached",
            "path": None,
        }
        assert len(bars.live_registrations(user_id=LOCAL_USER_ID)) == 2
        assert _undeploy(client, second).status_code == 200
        assert _deploy(client, third).status_code == 201
        assert _deploy(client, first).status_code == 200
    finally:
        bars.close()
        strategies.close()


def test_a_bad_cap_setting_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPER_MAX_RUNNING", "0")
    clock = {"now": _T}
    client, strategies, bars, _application = _world(tmp_path, clock)
    strategy_id, _version, _hash = _save(strategies, _document())
    try:
        response = _deploy(client, strategy_id)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "service_unavailable"
        assert bars.live_registrations(user_id=LOCAL_USER_ID) == []
    finally:
        bars.close()
        strategies.close()


def test_undeploy_leaves_an_open_position_and_cancels_a_resting_order(tmp_path: Path) -> None:
    """No fabricated fill. The long stays. The resting order is canceled."""
    clock = {"now": _T}
    client, strategies, bars, _application = _world(tmp_path, clock)
    strategy_id, _version, _hash = _save(strategies, _document())
    try:
        assert _deploy(client, strategy_id).status_code == 201
        from app.paper.broker import PaperBroker

        broker = PaperBroker(bars, now=lambda: clock["now"])
        bars.upsert([_bar(_T, 100, high=101, low=99)], user_id=LOCAL_USER_ID)
        clock["now"] = _T + _STEP + _GRACE
        broker.tick()
        held = _positions(bars, strategy_id)
        assert held[0][1] is not None
        fills_before = _fills(bars, strategy_id)
        wallet_before = bars.paper_wallet(
            user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1
        )
        cursor_before = bars.paper_cursor(
            user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1
        )
        bars.insert_paper_order(
            user_id=LOCAL_USER_ID,
            strategy_id=strategy_id,
            version=1,
            open_time=_T,
            asset_id="sol",
            side="sell",
            status="open",
            placed_at=_T + _STEP,
            expires_at=_T + _STEP + 300,
            leg="market",
            reason="resting",
        )
        stopped = _undeploy(client, strategy_id)
        assert stopped.status_code == 200
        assert stopped.json() == {
            "strategy_id": strategy_id,
            "version": 1,
            "status": "undeployed",
            "orders_canceled": 1,
            "position_state": "open_unmanaged",
        }
        assert bars.live_registrations(user_id=LOCAL_USER_ID) == []
        assert _positions(bars, strategy_id) == held
        assert _fills(bars, strategy_id) == fills_before
        assert bars.paper_wallet(
            user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1
        ) == wallet_before
        assert bars.paper_cursor(
            user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1
        ) == cursor_before
        orders = _orders(bars, strategy_id)
        assert orders[-1][3] == "canceled"
        assert bars.paper_open_orders(
            user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1
        ) == []
        kinds = [kind for _open, kind, _detail in _events(bars, strategy_id)]
        assert "ORDER_CANCELED" in kinds
        assert "STRATEGY_UNDEPLOYED" in kinds
        canceled = [
            json.loads(detail)
            for _open, kind, detail in _events(bars, strategy_id)
            if kind == "ORDER_CANCELED"
        ]
        assert canceled == [{"asset_id": "sol", "reason": "undeploy"}]
        undeployed = [
            json.loads(detail)
            for _open, kind, detail in _events(bars, strategy_id)
            if kind == "STRATEGY_UNDEPLOYED"
        ]
        assert undeployed == [{"orders_canceled": 1, "position_state": "open_unmanaged"}]
        for detail in undeployed + canceled:
            assert "price" not in detail
            assert "qty" not in detail

        bars.upsert(
            [_bar(_T + _STEP, 130, high=131, low=120)],
            user_id=LOCAL_USER_ID,
        )
        clock["now"] = _T + 2 * _STEP + _GRACE
        broker.tick()
        assert _fills(bars, strategy_id) == fills_before
        assert _positions(bars, strategy_id) == held
        missing = _undeploy(client, strategy_id)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_deployed"
    finally:
        bars.close()
        strategies.close()


def test_undeploy_of_a_flat_book_cancels_the_order_and_does_not_fill(tmp_path: Path) -> None:
    clock = {"now": _T}
    client, strategies, bars, _application = _world(tmp_path, clock)
    strategy_id, _version, _hash = _save(strategies, _document())
    try:
        assert _deploy(client, strategy_id).status_code == 201
        bars.insert_paper_order(
            user_id=LOCAL_USER_ID,
            strategy_id=strategy_id,
            version=1,
            open_time=_T,
            asset_id="sol",
            side="buy",
            status="open",
            placed_at=_T,
            expires_at=_T + 300,
            leg="market",
            reason="resting",
        )
        stopped = _undeploy(client, strategy_id)
        assert stopped.status_code == 200
        assert stopped.json()["position_state"] == "flat"
        assert stopped.json()["orders_canceled"] == 1
        assert _fills(bars, strategy_id) == []
        assert _positions(bars, strategy_id) == []
        assert _orders(bars, strategy_id)[0][3] == "canceled"
    finally:
        bars.close()
        strategies.close()


def test_deploy_rejects_unknown_ids_and_a_symbol_without_a_feed(tmp_path: Path) -> None:
    clock = {"now": _T}
    client, strategies, bars, application = _world(tmp_path, clock)
    strategy_id, _version, _hash = _save(strategies, _document())
    foreign = _document()
    foreign["assets"][0]["symbol"] = "BTC"
    foreign["assets"][0]["id"] = "btc"
    foreign["actions"][0]["asset"] = "btc"
    foreign["actions"][1]["asset"] = "btc"
    other_id, _version, _hash = _save(strategies, foreign)
    try:
        assert _deploy(client, "nope").status_code == 422
        assert _deploy(client, strategy_id, version=0).status_code == 422
        missing = _deploy(client, "a" * 32)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"
        refused = _deploy(client, other_id)
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "not_deployable"
        assert bars.live_registrations(user_id=LOCAL_USER_ID) == []
        application.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id="other")
        forbidden = _deploy(client, strategy_id)
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "forbidden"
    finally:
        bars.close()
        strategies.close()


def test_deploy_does_not_import_a_chain_client() -> None:
    """The paper package stays off wallets, keys, and swaps."""
    import app.paper.deploy as deploy
    import app.paper.routes as routes

    text = Path(deploy.__file__).read_text(encoding="utf-8")
    text += Path(routes.__file__).read_text(encoding="utf-8")
    for word in ("private_key", "sign_transaction", "swap", "wallet_address"):
        assert word not in text
