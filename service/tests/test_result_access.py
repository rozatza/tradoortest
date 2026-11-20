"""Owner isolation, loop affinity, and corrupt stored results for backtest reads.

These cases follow the review probes. They use the app's TestClient and
JobQueue instead of the probe harness, which is not part of this repo.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.jobs.routes as routes
from app.auth import CurrentUser, get_current_user
from app.jobs.queue import JobQueue
from app.main import create_app
from test_backtest import _START, _strategy

_LOCAL = "local-user"
_ROUTES = (
    "",
    "/results",
    "/exports/trades.csv",
    "/exports/trades.json",
    "/exports/metrics.csv",
    "/exports/metrics.json",
)
_EXPORTS = _ROUTES[1:]


def _bars(count: int = 240) -> list[dict[str, int]]:
    return [
        {
            "open_time": _START + index * 3600,
            "open": 100 + (index % 17),
            "high": 120,
            "low": 90,
            "close": 100 + (index % 17),
            "volume": 1,
        }
        for index in range(count)
    ]


def _succeeded(tmp_path: Path) -> tuple[JobQueue, TestClient, str]:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = TestClient(create_app(jobs=queue))
    created = client.post(
        "/backtests",
        json={
            "request_key": f"guard-{time.time_ns()}",
            "ir": _strategy(),
            "bars": {"sol": _bars()},
        },
    )
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    deadline = time.monotonic() + 20
    status = ""
    while time.monotonic() < deadline:
        status = client.get(f"/backtests/{job_id}").json()["status"]
        if status in {"succeeded", "failed"}:
            break
        time.sleep(0.01)
    assert status == "succeeded"
    return queue, client, job_id


def _store(path: Path, job_id: str, text: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE job_results SET result_json = ? WHERE job_id = ?",
            (text, job_id),
        )


def test_second_user_404_everywhere_and_queue_filters_owner(tmp_path: Path) -> None:
    queue, client, job_id = _succeeded(tmp_path)
    try:
        client.app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id="bob")
        for route in _ROUTES:
            response = client.get(f"/backtests/{job_id}{route}")
            assert response.status_code == 404, route
            assert b"trades" not in response.content, route
            assert b"equity" not in response.content, route
        assert queue.result_json("bob", job_id) is None
        assert queue.result_json(_LOCAL, job_id) is not None
    finally:
        queue.close()


def test_results_and_exports_load_the_row_off_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue, client, job_id = _succeeded(tmp_path)
    seen: list[str] = []
    original = routes._load_result

    def spy(*args: object, **kwargs: object) -> object:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen.append("thread")
        else:
            seen.append("LOOP")
        return original(*args, **kwargs)

    monkeypatch.setattr(routes, "_load_result", spy)
    try:
        for route in _EXPORTS:
            assert client.get(f"/backtests/{job_id}{route}").status_code == 200, route
    finally:
        queue.close()
    assert seen
    assert set(seen) == {"thread"}, seen


def test_bad_stored_results_are_422_not_500_and_missing_row_is_409(tmp_path: Path) -> None:
    queue, client, job_id = _succeeded(tmp_path)
    try:
        good = client.get(f"/backtests/{job_id}/results").json()
        assert good["trades"], "the fixture job produced no trade to corrupt"
        assert "sharpe" in good["metrics"]["values"]

        def case(
            mutate,
            raw: str | None = None,
        ) -> tuple[list[int], list[int]]:
            stored = copy.deepcopy(good)
            mutate(stored)
            _store(tmp_path / "jobs.sqlite", job_id, raw if raw is not None else json.dumps(stored))
            trades = [
                client.get(f"/backtests/{job_id}/exports/{name}").status_code
                for name in ("trades.csv", "trades.json")
            ]
            metrics = [
                client.get(f"/backtests/{job_id}/exports/{name}").status_code
                for name in ("metrics.csv", "metrics.json")
            ]
            return trades, metrics

        assert case(lambda row: row["trades"][0].pop("commission")) == ([422, 422], [200, 200])
        assert case(lambda row: row["trades"][0].__setitem__("commission", 1.5)) == (
            [422, 422],
            [200, 200],
        )
        assert case(lambda row: row.__setitem__("trades", "x")) == ([422, 422], [200, 200])
        assert case(lambda row: row["metrics"]["values"].pop("sharpe")) == ([200, 200], [422, 422])
        assert case(lambda row: row["metrics"]["values"].__setitem__("sharpe", 3)) == (
            [200, 200],
            [422, 422],
        )
        assert case(lambda row: None, raw="{not json") == ([422, 422], [422, 422])
        assert client.get(f"/backtests/{job_id}/results").status_code == 422
        assert case(lambda row: None, raw="[1,2]") == ([422, 422], [422, 422])
        assert client.get(f"/backtests/{job_id}/results").status_code == 422
        with sqlite3.connect(tmp_path / "jobs.sqlite") as conn:
            conn.execute("DELETE FROM job_results WHERE job_id = ?", (job_id,))
        for route in _EXPORTS:
            assert client.get(f"/backtests/{job_id}{route}").status_code == 409, route
    finally:
        queue.close()
