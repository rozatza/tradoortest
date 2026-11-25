"""GET /paper matches the stored book. Sockets stay blocked."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_paper_broker import _GRACE, _STEP
from tests.test_paper_deploy import (
    _T,
    _bar,
    _deploy,
    _document,
    _events,
    _positions,
    _save,
    _undeploy,
    _world,
)

from app.auth import LOCAL_USER_ID, CurrentUser, get_current_user
from app.paper.broker import ORDER_TTL_SECONDS, PaperBroker


def test_the_dashboard_reads_the_stored_book(tmp_path: Path) -> None:
    clock = {"now": _T}
    client, strategies, bars, application = _world(tmp_path, clock)
    strategy_id, _version, _hash = _save(strategies, _document())
    try:
        empty = client.get("/paper")
        assert empty.status_code == 200
        assert empty.json() == {
            "mode": "paper",
            "simulated": True,
            "strategies": [],
            "positions": [],
            "events": [],
            "events_cursor": 0,
            "events_has_more": False,
            "drift": [],
            "drift_threshold": "0.01",
            "drift_threshold_name": "PAPER_DRIFT_THRESHOLD",
            "shadow": {
                "enabled": False,
                "build_enabled": False,
                "slippage_bps": 50,
                "min_interval_seconds": "1.0",
                "daily_cap": 1000,
                "amount_mismatch_rate": None,
                "build_attempts": 0,
                "amount_mismatches": 0,
                "quotes": [],
            },
        }
        assert _deploy(client, strategy_id).status_code == 201
        broker = PaperBroker(bars, now=lambda: clock["now"])
        bars.upsert([_bar(_T, 100, high=101, low=99)], user_id=LOCAL_USER_ID)
        clock["now"] = _T + _STEP + _GRACE
        broker.tick()
        missed_open = _T + _STEP
        clock["now"] = missed_open + _STEP + ORDER_TTL_SECONDS + 1
        broker.tick()
        bars.upsert([_bar(missed_open + _STEP, 1, synthetic=True)], user_id=LOCAL_USER_ID)

        body = client.get("/paper").json()
        stored = _positions(bars, strategy_id)[0]
        assert body["mode"] == "paper"
        assert body["simulated"] is True
        assert "cash" not in body
        assert "wallet" not in body
        assert body["strategies"] == [
            {
                "strategy_id": strategy_id,
                "version": 1,
                "name": "Hand",
                "bar_size": "1m",
                "registered_at": _T,
                "status": "running",
            }
        ]
        position = body["positions"][0]
        assert position["state"] == "open"
        assert position["asset_id"] == "sol"
        assert position["qty"] == stored[1]
        assert position["entry"] == stored[3]
        assert position["basis"] == stored[4]
        assert position["realized"] == stored[5]
        assert position["mark"] == {
            "label": "mark",
            "price": "100",
            "open_time": _T,
            "timeframe": "1m",
            "source": "last_stored_close",
        }
        kinds = [event["kind"] for event in body["events"]]
        assert "BAR_MISSED" in kinds
        missed = [event for event in body["events"] if event["kind"] == "BAR_MISSED"]
        assert len(missed) == 1
        assert isinstance(missed[0]["id"], int) and missed[0]["id"] > 0
        shown_miss = {
            key: missed[0][key]
            for key in ("strategy_id", "version", "open_time", "kind", "detail")
        }
        assert shown_miss == {
            "strategy_id": strategy_id,
            "version": 1,
            "open_time": missed_open,
            "kind": "BAR_MISSED",
            "detail": json.dumps({"reason": "ttl"}, sort_keys=True, separators=(",", ":")),
        }
        stored_events = _events(bars, strategy_id)
        shown = [
            (event["open_time"], event["kind"], event["detail"]) for event in body["events"]
        ]
        assert shown == list(stored_events)

        stopped = _undeploy(client, strategy_id)
        assert stopped.json()["position_state"] == "open_unmanaged"
        after = client.get("/paper").json()
        assert after["strategies"] == []
        assert after["positions"][0]["state"] == "open_unmanaged"
        assert after["positions"][0]["realized"] == stored[5]
        assert after["positions"][0]["qty"] == stored[1]
        assert after["positions"][0]["mark"]["price"] == "100"
        after_kinds = [event["kind"] for event in after["events"]]
        assert "BAR_MISSED" in after_kinds
        assert "STRATEGY_UNDEPLOYED" in after_kinds
        text = json.dumps(after)
        assert "wallet" not in text
        assert "cash" not in text
        for key in ("pnl", "equity", "unrealized"):
            assert key not in after["positions"][0]

        application.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id="other")
        forbidden = client.get("/paper")
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "forbidden"
    finally:
        bars.close()
        strategies.close()
