"""Async backtest jobs: start, status, results, and both 429s.

The bars are synthetic. Nothing here calls the network or opens the default
job database.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.backtest import metrics_csv, metrics_json, run_backtest, trade_log_csv, trade_log_json
from app.backtest.engine import MAX_GRID_BARS
from app.backtest.log import METRIC_COLUMNS, TRADE_COLUMNS
from app.dsl import format_strategy, parse_strategy
from app.dsl.errors import StrategyError
from app.indicators.errors import IndicatorError
from app.ir.canonical import canonicalize
from app.ir.depth import JsonShapeError
from app.ir.validate import validate_ir
from app.jobs.export import ExportError, csv_cell, render_export
from app.jobs.queue import JobQueue, QueueBusy
from app.jobs.routes import _prepare
from app.jobs.run import _bars, run_backtest_job
from app.main import create_app
from app.tools.specs import OUTPUTS
from test_backtest import _START, _risk, _strategy

_LOCAL = "local-user"


def _row(offset: int, close: float, **extra: object) -> dict:
    row: dict = {
        "open_time": _START + offset,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1,
    }
    row.update(extra)
    return row


def _payload(
    key: str,
    document: dict | None = None,
    bars: list | None = None,
    **extra: object,
) -> dict:
    body: dict = {
        "request_key": key,
        "ir": document if document is not None else _strategy(),
        "bars": {"sol": bars if bars is not None else [_row(0, 100), _row(60, 110)]},
    }
    body.update(extra)
    return body


def _client(queue: JobQueue) -> TestClient:
    return TestClient(create_app(jobs=queue))


def _wait(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 10
    last: dict | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/backtests/{job_id}")
        assert response.status_code == 200
        last = response.json()
        if last["status"] in {"succeeded", "failed"}:
            return last
        time.sleep(0.01)
    raise AssertionError(last)


def test_start_status_and_results_round_trip(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    try:
        created = client.post("/backtests", json=_payload("round"))
        assert created.status_code == 202
        body = created.json()
        assert body["user_id"] == _LOCAL
        assert body["request_key"] == "round"
        assert body["error"] is None
        status = _wait(client, body["job_id"])
        assert status["status"] == "succeeded"
        assert status["job_id"] == body["job_id"]
        results = client.get(f"/backtests/{body['job_id']}/results")
        assert results.status_code == 200
        payload = results.json()
        assert payload["schema"] == "tradoortest.backtest_result.v1"
        assert isinstance(payload["final_equity"], str)
        assert payload["metrics"]["values"]["net_return"] is not None
        assert "NaN" not in results.text
        assert "Infinity" not in results.text
    finally:
        queue.close()


def test_a_duplicate_request_key_returns_the_same_job(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    try:
        body = _payload("same", bars=[_row(0, 100)])
        first = client.post("/backtests", json=body)
        assert first.status_code == 202
        job_id = first.json()["job_id"]
        second = client.post("/backtests", json=body)
        assert second.status_code == 200
        assert second.json()["job_id"] == job_id
        reused = client.post("/backtests", json=_payload("same", bars=[_row(0, 50)]))
        assert reused.status_code == 409
        assert reused.json()["error"]["code"] == "request_key_reused"
        status = _wait(client, job_id)
        assert status["status"] == "succeeded"
        results = client.get(f"/backtests/{job_id}/results")
        assert results.status_code == 200
        qty = results.json()["fills"][0]["qty"]
        assert Decimal(qty) == Decimal("100")
    finally:
        queue.close()


def test_results_keep_undefined_ratios_and_the_quality_counts(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 100},
                "risk": _risk(1, 50),
            }
        ],
    )
    bars = [
        _row(0, 100),
        {
            "open_time": _START + 60,
            "open": 110,
            "high": 120,
            "low": 105,
            "close": 110,
            "volume": 1,
        },
    ]
    try:
        created = client.post("/backtests", json=_payload("ratios", document, bars))
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        results = client.get(f"/backtests/{job_id}/results")
        text = results.text
        assert "NaN" not in text
        assert "Infinity" not in text
        metrics = results.json()["metrics"]
        assert metrics["take_profit_limit_below_low"] == 1
        short = client.post("/backtests", json=_payload("one-bar", bars=[_row(0, 100)]))
        short_id = short.json()["job_id"]
        assert _wait(client, short_id)["status"] == "succeeded"
        short_body = client.get(f"/backtests/{short_id}/results")
        assert "NaN" not in short_body.text
        assert "Infinity" not in short_body.text
        short_metrics = short_body.json()["metrics"]
        assert short_metrics["values"]["sharpe"] is None
        assert short_metrics["undefined"]["sharpe"] == "too_few_returns"
        assert metrics["synthetic_bars"] == 0
        assert metrics["missing_bars"] == 0

        quiet = client.post(
            "/backtests",
            json=_payload(
                "quiet",
                bars=[
                    _row(0, 100),
                    _row(60, 100, synthetic=True),
                    _row(120, 100, synthetic=True),
                ],
                long_no_trade_minutes=1,
            ),
        )
        quiet_id = quiet.json()["job_id"]
        assert _wait(client, quiet_id)["status"] == "succeeded"
        quiet_metrics = client.get(f"/backtests/{quiet_id}/results").json()["metrics"]
        assert quiet_metrics["synthetic_bars"] == 2
        assert quiet_metrics["suspect_outage"] is True
        assert quiet_metrics["long_no_trade_bars"] == 2

        hole = client.post(
            "/backtests",
            json=_payload("hole", bars=[_row(0, 100), _row(180, 100)]),
        )
        hole_id = hole.json()["job_id"]
        assert _wait(client, hole_id)["status"] == "succeeded"
        hole_metrics = client.get(f"/backtests/{hole_id}/results").json()["metrics"]
        assert hole_metrics["missing_bars"] >= 1
    finally:
        queue.close()


def test_concurrency_limit_is_429_and_a_duplicate_is_not(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def runner(body: dict) -> dict:
        started.set()
        assert release.wait(5)
        return run_backtest_job(body)

    queue = JobQueue(
        tmp_path / "jobs.sqlite",
        max_concurrent=1,
        per_minute=30,
        runner=runner,
    )
    client = _client(queue)
    try:
        first = client.post("/backtests", json=_payload("one"))
        assert first.status_code == 202
        job_id = first.json()["job_id"]
        assert started.wait(5)
        blocked = client.get(f"/backtests/{job_id}/results")
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "not_ready"
        second = client.post("/backtests", json=_payload("two"))
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "concurrency_limit"
        replay = client.post("/backtests", json=_payload("one"))
        assert replay.status_code == 200
        assert replay.json()["job_id"] == job_id
        release.set()
        assert _wait(client, job_id)["status"] == "succeeded"
    finally:
        release.set()
        queue.close()


def test_per_minute_limit_is_429_and_a_duplicate_is_not(tmp_path: Path) -> None:
    queue = JobQueue(
        tmp_path / "jobs.sqlite",
        max_concurrent=4,
        per_minute=1,
        clock=lambda: 1_700_000_000.0,
    )
    client = _client(queue)
    try:
        first = client.post("/backtests", json=_payload("one"))
        assert first.status_code == 202
        job_id = first.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        second = client.post("/backtests", json=_payload("two"))
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "rate_limit"
        replay = client.post("/backtests", json=_payload("one"))
        assert replay.status_code == 200
        assert replay.json()["job_id"] == job_id
    finally:
        queue.close()


def test_a_year_of_one_minute_slots_is_422(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    year = 365 * 86400
    try:
        response = client.post(
            "/backtests",
            json=_payload("wide", bars=[_row(0, 1.0), _row(year, 1.0)]),
        )
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "grid_too_large"
        assert str(MAX_GRID_BARS) in error["message"]
        assert client.get("/backtests/missing").status_code == 404
    finally:
        queue.close()


def test_deep_ir_and_deep_dsl_are_422(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    node: object = {"op": "lit", "value": True}
    for _ in range(120):
        node = {"op": "neg", "arg": node}
    nested = "(" * 100 + "true" + ")" * 100
    dsl = format_strategy(_strategy()).replace("signal go = true", f"signal go = {nested}", 1)
    try:
        ir = client.post(
            "/backtests",
            json={"request_key": "deep-ir", "ir": node, "bars": {"sol": [_row(0, 1)]}},
        )
        assert ir.status_code == 422
        ir_error = ir.json()["error"]
        assert ir_error["code"] == "expr_too_deep"
        assert ir_error["path"].startswith("$")
        assert ".arg" in ir_error["path"]
        script = client.post(
            "/backtests",
            json={"request_key": "deep-dsl", "dsl": dsl, "bars": {"sol": [_row(0, 1)]}},
        )
        assert script.status_code == 422
        assert script.json()["error"]["code"] == "expr_too_deep"
    finally:
        queue.close()


def test_a_route_recursion_error_is_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RecursionError("nested")

    monkeypatch.setattr("app.jobs.routes.validate_ir", boom)
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    try:
        response = client.post("/backtests", json=_payload("recurse"))
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "expr_too_deep"
    finally:
        queue.close()


def test_lone_surrogates_are_positioned_422(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    raw_ir = (
        '{"request_key":"surrogate-ir","ir":{"strategy":{"name":"bad\\ud800"}},'
        '"bars":{"sol":[{"open_time":1,"close":1}]}}'
    )
    raw_dsl = (
        '{"request_key":"surrogate-dsl","dsl":"strategy hand:\\n  name: \\"bad\\ud800\\"\\n",'
        '"bars":{"sol":[{"open_time":1,"close":1}]}}'
    )
    headers = {"content-type": "application/json"}
    try:
        ir = client.post("/backtests", content=raw_ir.encode("ascii"), headers=headers)
        assert ir.status_code == 422
        ir_error = ir.json()["error"]
        assert ir_error["code"] == "invalid_string"
        assert ir_error["path"] == "$.ir.strategy.name"
        assert "index" in ir_error["message"]
        script = client.post("/backtests", content=raw_dsl.encode("ascii"), headers=headers)
        assert script.status_code == 422
        script_error = script.json()["error"]
        assert script_error["code"] == "invalid_string"
        assert script_error["path"] == "$.dsl"
    finally:
        queue.close()


def test_indicator_failures_are_coded_422_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _strategy(
        derived_series=[
            {
                "id": "sma_2",
                "kind": "indicator",
                "indicator": "sma",
                "source": "close",
                "params": {"period": 2},
            }
        ]
    )
    def overflow(*_args: object, **_kwargs: object) -> list[float]:
        raise OverflowError("big")

    def library(*_args: object, **_kwargs: object) -> list[float]:
        raise IndicatorError("bad period")

    def infinite(*_args: object, **_kwargs: object) -> list[float]:
        return [math.inf, math.inf]

    cases = {
        "overflow": (overflow, "indicator_overflow"),
        "library": (library, "indicator_error"),
        "infinite": (infinite, "indicator_non_finite"),
    }
    for key, (fn, code) in cases.items():
        monkeypatch.setattr("app.backtest.engine.sma", fn)
        queue = JobQueue(tmp_path / f"{key}.sqlite")
        client = _client(queue)
        try:
            created = client.post("/backtests", json=_payload(key, document))
            assert created.status_code == 202
            job_id = created.json()["job_id"]
            status = _wait(client, job_id)
            assert status["status"] == "failed"
            assert status["error"]["code"] == code
            results = client.get(f"/backtests/{job_id}/results")
            assert results.status_code == 422
            assert results.json()["error"]["code"] == code
        finally:
            queue.close()


def test_a_worker_recursion_error_is_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RecursionError("nested")

    monkeypatch.setattr("app.jobs.run.run_backtest", boom)
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    try:
        created = client.post("/backtests", json=_payload("worker-deep"))
        job_id = created.json()["job_id"]
        status = _wait(client, job_id)
        assert status["status"] == "failed"
        assert status["error"]["code"] == "expr_too_deep"
        results = client.get(f"/backtests/{job_id}/results")
        assert results.status_code == 422
        assert results.json()["error"]["code"] == "expr_too_deep"
    finally:
        queue.close()


def test_results_survive_reopening_the_database(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    queue = JobQueue(path)
    client = _client(queue)
    created = client.post("/backtests", json=_payload("kept"))
    job_id = created.json()["job_id"]
    assert _wait(client, job_id)["status"] == "succeeded"
    first = client.get(f"/backtests/{job_id}/results").json()
    queue.close()
    again = JobQueue(path)
    client = _client(again)
    try:
        status = client.get(f"/backtests/{job_id}")
        assert status.status_code == 200
        assert status.json()["status"] == "succeeded"
        second = client.get(f"/backtests/{job_id}/results")
        assert second.status_code == 200
        assert second.json() == first
    finally:
        again.close()


def test_a_queued_job_from_a_dead_process_does_not_hold_a_slot(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    queue = JobQueue(path, max_concurrent=1, per_minute=10)
    queue.close()
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO jobs (job_id, user_id, request_key, status, created_at, error_json)
            VALUES ('stuck', ?, 'stuck', 'queued', ?, NULL)
            """,
            (_LOCAL, time.time()),
        )
    reopened = JobQueue(path, max_concurrent=1, per_minute=10)
    client = _client(reopened)
    try:
        stuck = client.get("/backtests/stuck")
        assert stuck.status_code == 200
        assert stuck.json()["status"] == "failed"
        assert stuck.json()["error"]["code"] == "interrupted"
        created = client.post("/backtests", json=_payload("fresh"))
        assert created.status_code == 202
    finally:
        reopened.close()


def test_health_does_not_open_the_job_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    app = create_app()
    assert app.state.jobs is None
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert app.state.jobs is None
    assert not (tmp_path / "data" / "backtest-jobs.sqlite").exists()


def test_a_job_for_another_user_is_not_visible(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    try:
        other = queue.start(
            user_id="someone-else",
            request_key="theirs",
            body={"ir": _strategy(), "bars": {"sol": [_row(0, 1)]}},
        )
        hidden = client.get(f"/backtests/{other.job_id}")
        assert hidden.status_code == 404
        assert queue.get("someone-else", other.job_id).job_id == other.job_id
    finally:
        queue.close()


def test_dsl_start_runs_the_formatted_strategy(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    try:
        created = client.post(
            "/backtests",
            json={
                "request_key": "from-dsl",
                "dsl": format_strategy(_strategy()),
                "bars": {"sol": [_row(0, 100)]},
            },
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
    finally:
        queue.close()


def test_validate_ir_rejects_a_lone_surrogate_before_the_hash() -> None:
    document = {"strategy": {"name": "bad\ud800"}}
    issues = validate_ir(document)
    assert issues[0].keyword == "invalid_string"
    assert issues[0].path == "$.strategy.name"
    with pytest.raises(JsonShapeError) as caught:
        canonicalize(document)
    assert caught.value.code == "invalid_string"
    assert caught.value.path == "$.strategy.name"


_MARKER = "424242.5"
_JSON = {"content-type": "application/json"}


def _raw(body: str) -> bytes:
    return body.encode("utf-8")


def _db_bytes(path: Path) -> bytes:
    blob = bytearray()
    for item in sorted(path.parent.glob(path.name + "*")):
        if item.is_file():
            blob.extend(item.read_bytes())
    return bytes(blob)


def _post_marked(client: TestClient, key: str) -> object:
    row = _row(0, 100, high=31337.25, volume=424242.5)
    return client.post("/backtests", json=_payload(key, bars=[row]))


def test_non_finite_and_non_utf8_bodies_are_422(tmp_path: Path) -> None:
    """NaN, Infinity, 1e400, a NaN inside ir, and a body that is not UTF-8."""
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    cases = [
        '{"request_key":"nan","bars":{"sol":[{"open_time":1,"close":NaN}]}}',
        '{"request_key":"inf","bars":{"sol":[{"open_time":1,"close":Infinity}]}}',
        '{"request_key":"huge","bars":{"sol":[{"open_time":1,"close":1e400}]}}',
        '{"request_key":"nan-ir","ir":{"capital":{"initial":NaN}},"bars":{}}',
    ]
    try:
        for text in cases:
            response = client.post("/backtests", content=_raw(text), headers=_JSON)
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_json"
        raw = b'{"request_key": "bad\xff"}'
        response = client.post("/backtests", content=raw, headers=_JSON)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_json"
        utf8 = '{"request_key":"wide","bars":{}}'
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            response = client.post(
                "/backtests", content=utf8.encode(encoding), headers=_JSON
            )
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_json"
    finally:
        queue.close()


def test_bar_values_do_not_remain_after_success_failure_or_interrupt(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"

    def boom(_body: dict) -> dict:
        raise RuntimeError("stop")

    failed = JobQueue(path, runner=boom)
    failed_client = _client(failed)
    try:
        created = _post_marked(failed_client, "fails")
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        assert _wait(failed_client, job_id)["status"] == "failed"
        assert _MARKER.encode() not in _db_bytes(path)
    finally:
        failed.close()

    stopped = JobQueue(tmp_path / "stopped.sqlite")
    stopped.close()
    stopped_path = tmp_path / "stopped.sqlite"
    with sqlite3.connect(stopped_path) as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, user_id, request_key, status, created_at, error_json, body_hash
            )
            VALUES ('stuck', ?, 'stuck', 'queued', ?, NULL, NULL)
            """,
            (_LOCAL, time.time()),
        )
        conn.execute(
            "INSERT INTO job_bodies (job_id, body_json) VALUES ('stuck', ?)",
            (json.dumps({"volume": _MARKER}),),
        )
    reopened = JobQueue(stopped_path)
    try:
        assert reopened.get(_LOCAL, "stuck").status == "failed"
        assert _MARKER.encode() not in _db_bytes(stopped_path)
    finally:
        reopened.close()

    done = JobQueue(tmp_path / "done.sqlite")
    done_client = _client(done)
    try:
        created = _post_marked(done_client, "ok")
        job_id = created.json()["job_id"]
        assert _wait(done_client, job_id)["status"] == "succeeded"
        results = done_client.get(f"/backtests/{job_id}/results")
        assert results.status_code == 200
        assert Decimal(results.json()["fills"][0]["price"]) == Decimal("100")
        # The request body is gone and the result JSON does not grow by the
        # raw high or volume. The chart window keeps those candles until the
        # TTL. test_chart_bars_leave_the_database_and_the_wal covers that pass.
        with sqlite3.connect(tmp_path / "done.sqlite") as conn:
            assert conn.execute("SELECT COUNT(*) FROM job_bodies").fetchone() == (0,)
            stored = conn.execute("SELECT result_json FROM job_results").fetchone()
            assert stored is not None
            assert _MARKER.encode() not in stored[0].encode()
            assert b"31337.25" not in stored[0].encode()
            chart = conn.execute("SELECT bars_json FROM job_charts").fetchone()
            assert chart is not None
            assert _MARKER in chart[0]
            assert "31337.25" in chart[0]
    finally:
        done.close()


def test_the_rate_window_excludes_a_job_created_exactly_60_seconds_ago(tmp_path: Path) -> None:
    now = {"t": 1_700_000_000.0}
    queue = JobQueue(
        tmp_path / "jobs.sqlite",
        max_concurrent=4,
        per_minute=1,
        clock=lambda: now["t"],
    )
    client = _client(queue)
    try:
        assert client.post("/backtests", json=_payload("one")).status_code == 202
        now["t"] += 59
        blocked = client.post("/backtests", json=_payload("two"))
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "rate_limit"
        now["t"] += 1
        allowed = client.post("/backtests", json=_payload("three"))
        assert allowed.status_code == 202
    finally:
        queue.close()


def test_a_queued_job_counts_toward_the_concurrency_limit(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite", max_concurrent=1, per_minute=10)
    queue._executor.submit = lambda *_args, **_kwargs: None
    client = _client(queue)
    try:
        first = client.post("/backtests", json=_payload("one"))
        assert first.status_code == 202
        job_id = first.json()["job_id"]
        assert client.get(f"/backtests/{job_id}").json()["status"] == "queued"
        second = client.post("/backtests", json=_payload("two"))
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "concurrency_limit"
    finally:
        queue.close()


def test_sixty_four_threads_with_one_key_create_one_job(tmp_path: Path) -> None:
    queue = JobQueue(
        tmp_path / "jobs.sqlite",
        max_concurrent=64,
        per_minute=100,
        runner=lambda _body: {"ok": True},
    )
    errors: list[BaseException] = []
    created: list[bool] = []
    ids: list[str] = []
    barrier = threading.Barrier(64)

    def once() -> None:
        try:
            barrier.wait(5)
            job = queue.start(user_id=_LOCAL, request_key="race", body={"n": 1})
        except Exception as exc:
            errors.append(exc)
            return
        ids.append(job.job_id)
        created.append(job.created)

    threads = [threading.Thread(target=once) for _ in range(64)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert errors == []
        assert sum(created) == 1
        assert len(set(ids)) == 1
        with sqlite3.connect(tmp_path / "jobs.sqlite") as conn:
            count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
        assert count == (1,)
    finally:
        queue.close()


def test_a_request_key_of_200_characters_is_accepted_and_201_is_not(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite", runner=lambda _body: {"ok": True})
    client = _client(queue)
    try:
        accepted = client.post("/backtests", json=_payload("k" * 200))
        assert accepted.status_code == 202
        rejected = client.post("/backtests", json=_payload("k" * 201))
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "invalid_request_key"
    finally:
        queue.close()


def test_a_surrogate_in_the_request_key_is_422(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    raw = '{"request_key":"bad\\ud800","ir":{"strategy":{"name":"x"}},"bars":{}}'
    try:
        response = client.post("/backtests", content=_raw(raw), headers=_JSON)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_string"
        assert response.json()["error"]["path"] == "$.request_key"
    finally:
        queue.close()


def test_a_low_surrogate_and_a_surrogate_key_are_422(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    low = '{"request_key":"low","ir":{"strategy":{"name":"bad\\udc00"}},"bars":{}}'
    key = '{"request_key":"key","ir":{"\\ud800":1},"bars":{}}'
    try:
        for raw in (low, key):
            response = client.post("/backtests", content=_raw(raw), headers=_JSON)
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_string"
    finally:
        queue.close()


def test_exactly_200_000_slots_are_accepted(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite", runner=lambda _body: {"ok": True})
    client = _client(queue)
    span = (MAX_GRID_BARS - 1) * 60
    over = MAX_GRID_BARS * 60
    try:
        accepted = client.post(
            "/backtests",
            json=_payload("cap", bars=[_row(0, 1), _row(span, 1)]),
        )
        assert accepted.status_code == 202
        rejected = client.post(
            "/backtests",
            json=_payload("over", bars=[_row(0, 1), _row(over, 1)]),
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "grid_too_large"
    finally:
        queue.close()


def test_a_second_process_cannot_open_the_same_database(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    queue = JobQueue(path, max_concurrent=2, per_minute=10)
    queue._executor.submit = lambda *_args, **_kwargs: None
    client = _client(queue)
    try:
        created = client.post("/backtests", json=_payload("live"))
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        with pytest.raises(QueueBusy):
            JobQueue(path)
        status = client.get(f"/backtests/{job_id}")
        assert status.json()["status"] == "queued"
    finally:
        queue.close()


def test_a_held_write_lock_is_503(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    queue = JobQueue(path, busy_timeout_ms=50)
    client = _client(queue)
    other = sqlite3.connect(path)
    try:
        other.execute("BEGIN EXCLUSIVE")
        response = client.post("/backtests", json=_payload("locked"))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "busy"
    finally:
        other.execute("ROLLBACK")
        other.close()
        queue.close()


class _ExecuteProxy:
    """Stand-in for the queue connection. Result inserts can be forced to fail."""

    def __init__(self, conn: sqlite3.Connection, fail_until: int) -> None:
        self._conn = conn
        self._fail_until = fail_until
        self.inserts = 0

    def execute(self, sql: str, *args, **kwargs):
        if isinstance(sql, str) and "INSERT INTO job_results" in sql:
            self.inserts += 1
            if self.inserts <= self._fail_until:
                raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args, **kwargs)

    def close(self) -> None:
        self._conn.close()


def test_a_result_write_that_keeps_failing_does_not_leave_the_job_running(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    queue = JobQueue(path, max_concurrent=1, per_minute=10, runner=lambda _body: {"ok": True})
    queue._conn = _ExecuteProxy(queue._conn, fail_until=10)
    client = _client(queue)
    try:
        created = client.post("/backtests", json=_payload("write"))
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "failed"
        follow = client.post("/backtests", json=_payload("next"))
        assert follow.status_code == 202
    finally:
        queue.close()


def test_a_result_write_retries_a_held_lock(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite", runner=lambda _body: {"ok": True})
    proxy = _ExecuteProxy(queue._conn, fail_until=2)
    queue._conn = proxy
    client = _client(queue)
    try:
        created = client.post("/backtests", json=_payload("retry"))
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        assert proxy.inserts >= 3
    finally:
        queue.close()


_DAY = 24 * 60 * 60


def test_a_result_is_readable_at_23h59m_and_gone_at_24h01m(tmp_path: Path) -> None:
    """23h59m is still inside 24h. 24h01m has passed it, so the read deletes the job."""
    now = {"t": 1_700_000_000.0}
    path = tmp_path / "jobs.sqlite"
    queue = JobQueue(
        path,
        clock=lambda: now["t"],
        runner=lambda _body: {"marker": "919191.25"},
    )
    client = _client(queue)
    try:
        created = client.post("/backtests", json=_payload("kept"))
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        now["t"] += 23 * 60 * 60 + 59 * 60
        status = client.get(f"/backtests/{job_id}")
        assert status.status_code == 200
        results = client.get(f"/backtests/{job_id}/results")
        assert results.status_code == 200
        assert results.json()["marker"] == "919191.25"
        now["t"] += 2 * 60
        expired = client.get(f"/backtests/{job_id}")
        assert expired.status_code == 410
        assert expired.json()["error"]["code"] == "expired"
        missing = client.get(f"/backtests/{job_id}/results")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM job_results").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM job_bodies").fetchone() == (0,)
        assert b"919191.25" not in _db_bytes(path)
    finally:
        queue.close()


def test_startup_purges_an_expired_result(tmp_path: Path) -> None:
    now = {"t": 1_700_000_000.0}
    path = tmp_path / "jobs.sqlite"
    queue = JobQueue(
        path,
        clock=lambda: now["t"],
        runner=lambda _body: {"marker": "919191.25"},
    )
    client = _client(queue)
    created = client.post("/backtests", json=_payload("old"))
    job_id = created.json()["job_id"]
    assert _wait(client, job_id)["status"] == "succeeded"
    queue.close()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO job_bodies (job_id, body_json) VALUES (?, ?)",
            (job_id, '{"volume":"919191.25"}'),
        )
    now["t"] += _DAY + 60
    reopened = JobQueue(path, clock=lambda: now["t"])
    again = _client(reopened)
    try:
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM job_results").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM job_bodies").fetchone() == (0,)
        assert b"919191.25" not in _db_bytes(path)
        gone = again.get(f"/backtests/{job_id}")
        assert gone.status_code == 404
        assert gone.json()["error"]["code"] == "not_found"
    finally:
        reopened.close()


def test_a_late_worker_does_not_leave_an_orphan_result(tmp_path: Path) -> None:
    """A purge from another start runs before the worker finishes.

    The result insert must see that the job is gone. A row planted after
    that, with no job, is deleted on the next open past the TTL.
    """
    now = {"t": 1_700_000_000.0}
    started = threading.Event()
    release = threading.Event()

    def slow(_body: dict) -> dict:
        started.set()
        if not release.wait(5):
            raise TimeoutError("runner was not released")
        return {
            "marker": "ORPHAN-RESULT-999",
            "fills": [{"price": "123.456", "qty": "1"}],
        }

    path = tmp_path / "jobs.sqlite"
    queue = JobQueue(path, clock=lambda: now["t"], runner=slow, per_minute=100)
    client = _client(queue)
    finished = False
    try:
        created = client.post("/backtests", json=_payload("late"))
        assert created.status_code == 202
        late_id = created.json()["job_id"]
        assert started.wait(5)
        now["t"] += _DAY + 10
        other = client.post("/backtests", json=_payload("other"))
        assert other.status_code == 202
        gone = client.get(f"/backtests/{late_id}")
        assert gone.status_code == 404
        assert gone.json()["error"]["code"] == "not_found"
        release.set()
        finished = True
    finally:
        release.set()
        queue.close()
    assert finished
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM job_results WHERE job_id = ?", (late_id,)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM job_bodies WHERE job_id = ?", (late_id,)
        ).fetchone() == (0,)
        conn.execute(
            """
            INSERT INTO job_results (job_id, user_id, result_json)
            VALUES (?, ?, ?)
            """,
            (
                late_id,
                _LOCAL,
                '{"marker":"ORPHAN-RESULT-999","fills":[{"price":"123.456"}]}',
            ),
        )
        conn.execute(
            "INSERT INTO job_bodies (job_id, body_json) VALUES (?, ?)",
            (late_id, '{"price":"123.456"}'),
        )
    now["t"] += _DAY
    reopened = JobQueue(path, clock=lambda: now["t"])
    try:
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM job_results").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM job_bodies").fetchone() == (0,)
            assert conn.execute(
                """
                SELECT COUNT(*) FROM job_results
                WHERE job_id NOT IN (SELECT job_id FROM jobs)
                """
            ).fetchone() == (0,)
        blob = _db_bytes(path)
        assert b"ORPHAN-RESULT-999" not in blob
        assert b"123.456" not in blob
    finally:
        reopened.close()


def test_a_result_ttl_above_24_hours_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="24 hours"):
        JobQueue(tmp_path / "jobs.sqlite", result_ttl_seconds=_DAY + 1)
    with pytest.raises(ValueError, match="whole number"):
        JobQueue(tmp_path / "also.sqlite", result_ttl_seconds=True)  # type: ignore[arg-type]


def test_parse_strategy_rejects_a_lone_surrogate_with_a_position() -> None:
    with pytest.raises(StrategyError) as caught:
        parse_strategy('strategy hand:\n  name: "bad\ud800"\n')
    error = caught.value
    assert error.code == "invalid_string"
    assert error.line == 2
    assert error.column >= 1
    assert "surrogate" in error.message


def _result_schema() -> dict:
    variants = OUTPUTS["backtest_get"]["properties"]["result"]["anyOf"]
    return next(item for item in variants if item.get("type") == "object")


def test_the_tool_result_schema_has_no_equity_curve() -> None:
    schema = _result_schema()
    assert "equity_curve" not in schema["properties"]
    assert "equity_curve" not in schema["required"]
    assert schema["additionalProperties"] is False


def test_results_include_the_engine_curve_and_exports_match(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    bars = [_row(0, 100), _row(60, 110), _row(120, 130)]
    posted = _payload("curve", bars=bars)
    try:
        created = client.post("/backtests", json=posted)
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        results = client.get(f"/backtests/{job_id}/results")
        assert results.status_code == 200
        payload = results.json()
        prepared = _prepare(posted)
        engine = run_backtest(
            prepared["ir"],
            {
                asset_id: _bars(asset_id, rows, prepared["ir"]["engine"]["bar_size"])
                for asset_id, rows in prepared["bars"].items()
            },
            long_no_trade_minutes=prepared["long_no_trade_minutes"],
        )
        assert payload["equity_curve"] == [
            {
                "open_time": point.open_time,
                "equity": format(point.equity, "f"),
                "cash": format(point.cash, "f"),
            }
            for point in engine.equity_curve
        ]
        trades_csv = client.get(f"/backtests/{job_id}/exports/trades.csv")
        trades_json = client.get(f"/backtests/{job_id}/exports/trades.json")
        metrics_csv_body = client.get(f"/backtests/{job_id}/exports/metrics.csv")
        metrics_json_body = client.get(f"/backtests/{job_id}/exports/metrics.json")
        assert trades_csv.status_code == 200
        # Clean engine cells match the decimal grammar or are plain text, so
        # the CSV bytes still match. A formula that does not match is escaped:
        # test_csv_formula_escape_is_fail_closed.
        assert trades_csv.text == trade_log_csv(engine.trades)
        assert trades_csv.headers["content-type"].startswith("text/csv")
        assert trades_csv.headers["cache-control"] == "no-store"
        assert trades_csv.headers["content-disposition"] == 'attachment; filename="trades.csv"'
        assert trades_json.text == trade_log_json(engine.trades)
        assert trades_json.headers["content-type"].startswith("application/json")
        assert metrics_csv_body.text == metrics_csv(engine.metrics)
        assert metrics_json_body.text == metrics_json(engine.metrics)
        stored = payload["metrics"]["values"]["net_return"]
        assert stored in metrics_json_body.text
        assert stored in metrics_csv_body.text
        unknown = client.get(f"/backtests/{job_id}/exports/trades.txt")
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "not_found"
    finally:
        queue.close()


def test_export_copies_stored_strings_and_rejects_a_computed_cell() -> None:
    values = {column: "0.0000000000" for column in METRIC_COLUMNS}
    values["net_return"] = "9.9999999999"
    values["sharpe"] = None
    trade = {column: "stored" for column in TRADE_COLUMNS}
    trade["exit_time"] = None
    trade["exit_price"] = None
    result = {
        "metrics": {
            "values": values,
            "undefined": {"sharpe": "too_few_returns"},
        },
        "trades": [trade],
    }
    metrics_text = render_export("metrics.json", result)
    assert "9.9999999999" in metrics_text
    assert "too_few_returns" in metrics_text
    assert "0.5" not in metrics_text
    parsed = json.loads(metrics_text)
    assert parsed["values"]["net_return"] == "9.9999999999"
    assert parsed["values"]["sharpe"] is None
    assert render_export("metrics.csv", result).splitlines()[1].split(",")[0] == "9.9999999999"
    assert render_export("trades.csv", result).splitlines()[1].startswith("stored,stored,stored,")
    trades = json.loads(render_export("trades.json", result))
    assert trades["schema"] == "tradoortest.trade_log.v1"
    assert trades["trades"][0]["exit_time"] is None
    assert trades["trades"][0]["realized_pnl"] == "stored"
    broken = {
        "metrics": {"values": {**values, "net_return": 0.5}, "undefined": {}},
        "trades": [trade],
    }
    with pytest.raises(ExportError):
        render_export("metrics.json", broken)


def test_csv_formula_escape_is_fail_closed() -> None:
    """Raw only for the decimal grammar, or a validated integer or timestamp.

    Replacing ``_strict_decimal`` with a function that always returns true
    lets ``=1+1`` out unchanged, and the assertions on ``realized_pnl`` and
    ``sharpe`` fail.
    """
    trade = {column: "1" for column in TRADE_COLUMNS}
    trade["asset_id"] = '=HYPERLINK("http://evil")'
    trade["status"] = "+cmd"
    trade["trade_id"] = "-cmd"
    trade["limit_below_low"] = "@sum"
    trade["overlaps_suspect_outage"] = "\t=1"
    trade["realized_pnl"] = "=1+1"
    trade["entry_price"] = "-12.34"
    trade["qty"] = "-0.5"
    trade["exit_price"] = "+12.34"
    trade["entry_notional"] = "\r=1"
    trade["entry_time"] = "1767225600"
    trade["exit_time"] = None
    trade["synthetic_bars"] = "-01"
    values = {column: "0.1000000000" for column in METRIC_COLUMNS}
    values["net_return"] = "-12.34"
    values["sharpe"] = "=1+1"
    result = {
        "metrics": {"values": values, "undefined": {"sortino": "too_few_returns"}},
        "trades": [trade],
    }
    parsed = list(csv.DictReader(io.StringIO(render_export("trades.csv", result))))
    assert len(parsed) == 1
    row = parsed[0]
    assert row["asset_id"] == "'=HYPERLINK(\"http://evil\")"
    assert row["status"] == "'+cmd"
    assert row["trade_id"] == "'-cmd"
    assert row["limit_below_low"] == "'@sum"
    assert row["overlaps_suspect_outage"] == "'\t=1"
    assert row["realized_pnl"] == "'=1+1"
    assert row["entry_price"] == "-12.34"
    assert row["qty"] == "-0.5"
    assert row["exit_price"] == "'+12.34"
    assert row["entry_notional"] == "'\r=1"
    assert row["entry_time"] == "1767225600"
    assert row["exit_time"] == ""
    assert row["synthetic_bars"] == "'-01"
    assert csv_cell("realized_pnl", "-12.34") == "-12.34"
    assert csv_cell("sharpe", "=1+1") == "'=1+1"
    metrics_row = list(csv.DictReader(io.StringIO(render_export("metrics.csv", result))))[0]
    assert metrics_row["net_return"] == "-12.34"
    assert metrics_row["sharpe"] == "'=1+1"
    assert metrics_row["max_drawdown"] == "0.1000000000"
    trades_json = json.loads(render_export("trades.json", result))
    assert trades_json["trades"][0]["asset_id"] == '=HYPERLINK("http://evil")'
    assert trades_json["trades"][0]["realized_pnl"] == "=1+1"
    assert trades_json["trades"][0]["entry_price"] == "-12.34"
    assert "'" not in trades_json["trades"][0]["entry_price"]
    metrics_json_body = json.loads(render_export("metrics.json", result))
    assert metrics_json_body["values"]["net_return"] == "-12.34"
    assert metrics_json_body["values"]["sharpe"] == "=1+1"
    assert metrics_json_body["undefined"]["sortino"] == "too_few_returns"


def test_csv_escapes_padded_and_lookalike_formula_starts() -> None:
    """A leading pad or a fullwidth formula mark is escaped. Decimals stay raw.

    Dropping the pad and lookalike check lets `` =1+1`` and ``＝1+1`` out raw.
    """
    samples = (
        " =1+1",
        "\n=1+1",
        "\u00a0=1+1",
        "\ufeff=1+1",
        "＝1+1",
        "＋1",
        "－12",
        "＠SUM(A1)",
        " ＝1",
        "\ufeff＋1",
        "\u00a0＝1",
        " -12.34",
    )
    for sample in samples:
        trade = {column: "1" for column in TRADE_COLUMNS}
        trade["exit_time"] = None
        trade["asset_id"] = sample
        trade["entry_price"] = "-12.34"
        trade["qty"] = "-0.5"
        values = {column: "0.1000000000" for column in METRIC_COLUMNS}
        values["sharpe"] = sample
        values["net_return"] = "-12.34"
        result = {
            "metrics": {"values": values, "undefined": {}},
            "trades": [trade],
        }
        trades = render_export("trades.csv", result)
        metrics = render_export("metrics.csv", result)
        row = list(csv.DictReader(io.StringIO(trades)))[0]
        metric = list(csv.DictReader(io.StringIO(metrics)))[0]
        assert row["asset_id"] == "'" + sample
        assert metric["sharpe"] == "'" + sample
        assert row["entry_price"] == "-12.34"
        assert row["qty"] == "-0.5"
        assert metric["net_return"] == "-12.34"
        assert csv_cell("entry_price", sample) == "'" + sample


def test_an_unknown_csv_column_defaults_to_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A column that is not an integer or timestamp column cannot stay raw."""
    columns = (*TRADE_COLUMNS, "note", "noted_at")
    monkeypatch.setattr("app.jobs.export.TRADE_COLUMNS", columns)
    monkeypatch.setattr("app.jobs.export.TIMESTAMP_COLUMNS", frozenset({"noted_at"}))
    trade = {column: "1" for column in TRADE_COLUMNS}
    trade["exit_time"] = None
    trade["note"] = "=1+1"
    trade["noted_at"] = "-2026-01-01T00:00:00Z"
    values = {column: "0.1000000000" for column in METRIC_COLUMNS}
    result = {"metrics": {"values": values, "undefined": {}}, "trades": [trade]}
    row = list(csv.DictReader(io.StringIO(render_export("trades.csv", result))))[0]
    assert row["note"] == "'=1+1"
    assert row["noted_at"] == "-2026-01-01T00:00:00Z"
    trade["noted_at"] = "=1+1"
    row = list(csv.DictReader(io.StringIO(render_export("trades.csv", result))))[0]
    assert row["noted_at"] == "'=1+1"
    trade["note"] = "-2026-01-01T00:00:00Z"
    trade["noted_at"] = "-2026-01-01T00:00:00Z"
    row = list(csv.DictReader(io.StringIO(render_export("trades.csv", result))))[0]
    assert row["note"] == "'-2026-01-01T00:00:00Z"
    assert row["noted_at"] == "-2026-01-01T00:00:00Z"


def test_a_stored_numeric_formula_is_escaped_on_export(tmp_path: Path) -> None:
    """A formula written into the stored result is escaped on both CSV reads."""
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    try:
        created = client.post("/backtests", json=_payload("formula"))
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        with sqlite3.connect(tmp_path / "jobs.sqlite") as conn:
            stored = conn.execute("SELECT result_json FROM job_results").fetchone()
            assert stored is not None
            payload = json.loads(stored[0])
            assert payload["trades"]
            payload["trades"][0]["realized_pnl"] = "=1+1"
            payload["trades"][0]["qty"] = "-0.5"
            payload["metrics"]["values"]["sharpe"] = "=1+1"
            payload["metrics"]["values"]["net_return"] = "-12.34"
            conn.execute(
                "UPDATE job_results SET result_json = ? WHERE job_id = ?",
                (json.dumps(payload), job_id),
            )
        trades = list(
            csv.DictReader(
                io.StringIO(client.get(f"/backtests/{job_id}/exports/trades.csv").text)
            )
        )[0]
        metrics = list(
            csv.DictReader(
                io.StringIO(client.get(f"/backtests/{job_id}/exports/metrics.csv").text)
            )
        )[0]
        assert trades["realized_pnl"] == "'=1+1"
        assert trades["qty"] == "-0.5"
        assert metrics["sharpe"] == "'=1+1"
        assert metrics["net_return"] == "-12.34"
        trades_json = client.get(f"/backtests/{job_id}/exports/trades.json").json()
        metrics_json_body = client.get(f"/backtests/{job_id}/exports/metrics.json").json()
        assert trades_json["trades"][0]["realized_pnl"] == "=1+1"
        assert trades_json["trades"][0]["qty"] == "-0.5"
        assert metrics_json_body["values"]["sharpe"] == "=1+1"
        assert metrics_json_body["values"]["net_return"] == "-12.34"
    finally:
        queue.close()


def test_export_for_another_user_or_a_running_job_is_not_the_result(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow(body: dict) -> dict:
        started.set()
        if not release.wait(5):
            raise TimeoutError("runner was not released")
        return run_backtest_job(body)

    queue = JobQueue(tmp_path / "jobs.sqlite", runner=slow)
    client = _client(queue)
    try:
        other = queue.start(
            user_id="someone-else",
            request_key="theirs",
            body={"ir": _strategy(), "bars": {"sol": [_row(0, 1)]}},
        )
        hidden = client.get(f"/backtests/{other.job_id}/exports/trades.csv")
        assert hidden.status_code == 404
        assert hidden.json()["error"]["code"] == "not_found"
        created = client.post("/backtests", json=_payload("running"))
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        assert started.wait(5)
        waiting = client.get(f"/backtests/{job_id}/exports/metrics.json")
        assert waiting.status_code == 409
        assert waiting.json()["error"]["code"] == "not_ready"
    finally:
        release.set()
        queue.close()


def test_export_of_a_failed_job_is_the_stored_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RecursionError("nested")

    monkeypatch.setattr("app.jobs.run.run_backtest", boom)
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    try:
        created = client.post("/backtests", json=_payload("export-failed"))
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "failed"
        exported = client.get(f"/backtests/{job_id}/exports/trades.json")
        assert exported.status_code == 422
        assert exported.json()["error"]["code"] == "expr_too_deep"
    finally:
        queue.close()


def test_a_corrupt_result_is_422_not_500(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite", runner=lambda _body: {"marker": "1"})
    client = _client(queue)
    try:
        created = client.post("/backtests", json=_payload("corrupt"))
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        with sqlite3.connect(tmp_path / "jobs.sqlite") as conn:
            conn.execute("UPDATE job_results SET result_json = ?", ("{",))
        results = client.get(f"/backtests/{job_id}/results")
        assert results.status_code == 422
        assert results.json()["error"]["code"] == "invalid_result"
        exported = client.get(f"/backtests/{job_id}/exports/trades.csv")
        assert exported.status_code == 422
        assert exported.json()["error"]["code"] == "invalid_result"
    finally:
        queue.close()


def test_a_legacy_result_without_a_curve_still_exports(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    bars = [_row(0, 100), _row(60, 110), _row(120, 130)]
    posted = _payload("legacy", bars=bars)
    try:
        created = client.post("/backtests", json=posted)
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        prepared = _prepare(posted)
        engine = run_backtest(
            prepared["ir"],
            {
                asset_id: _bars(asset_id, rows, prepared["ir"]["engine"]["bar_size"])
                for asset_id, rows in prepared["bars"].items()
            },
            long_no_trade_minutes=prepared["long_no_trade_minutes"],
        )
        with sqlite3.connect(tmp_path / "jobs.sqlite") as conn:
            row = conn.execute(
                "SELECT result_json FROM job_results WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            assert row is not None
            stored = json.loads(row[0])
            assert "equity_curve" in stored
            del stored["equity_curve"]
            conn.execute(
                "UPDATE job_results SET result_json = ? WHERE job_id = ?",
                (json.dumps(stored), job_id),
            )
        results = client.get(f"/backtests/{job_id}/results")
        assert results.status_code == 200
        assert "equity_curve" not in results.json()
        expected = {
            "trades.csv": trade_log_csv(engine.trades),
            "trades.json": trade_log_json(engine.trades),
            "metrics.csv": metrics_csv(engine.metrics),
            "metrics.json": metrics_json(engine.metrics),
        }
        for name, text in expected.items():
            exported = client.get(f"/backtests/{job_id}/exports/{name}")
            assert exported.status_code == 200
            assert exported.text == text
    finally:
        queue.close()


def test_render_export_refuses_the_event_loop() -> None:
    values = {column: "1" for column in METRIC_COLUMNS}
    result = {"metrics": {"values": values, "undefined": {}}, "trades": []}

    async def on_the_loop() -> None:
        render_export("metrics.json", result)

    with pytest.raises(RuntimeError, match="event loop"):
        asyncio.run(on_the_loop())
    assert "1" in render_export("metrics.json", result)


def test_health_answers_while_a_max_size_export_renders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trade = {column: "1" for column in TRADE_COLUMNS}
    values = {column: "0.1000000000" for column in METRIC_COLUMNS}
    huge = {
        "trades": [trade] * 50_000,
        "metrics": {"values": values, "undefined": {}},
        "equity_curve": [
            {"open_time": index, "equity": "1.00", "cash": "1.00"} for index in range(200_000)
        ],
    }
    queue = JobQueue(tmp_path / "jobs.sqlite", runner=lambda _body: huge)
    client = _client(queue)
    started = threading.Event()
    release = threading.Event()
    rendered_on: list[threading.Thread] = []
    real_render = render_export

    def wrapped(name: str, result: dict) -> str:
        rendered_on.append(threading.current_thread())
        started.set()
        text = real_render(name, result)
        assert release.wait(5)
        return text

    monkeypatch.setattr("app.jobs.routes.render_export", wrapped)
    try:
        created = client.post("/backtests", json=_payload("huge"))
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        deadline = time.monotonic() + 30
        status = "queued"
        while time.monotonic() < deadline and status not in {"succeeded", "failed"}:
            status = client.get(f"/backtests/{job_id}").json()["status"]
            time.sleep(0.02)
        assert status == "succeeded"
        application = create_app(jobs=queue)

        async def probe() -> None:
            loop_thread = threading.current_thread()
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                timeout=30,
            ) as http:
                warm = await http.get("/health")
                assert warm.status_code == 200
                export_task = asyncio.create_task(
                    http.get(f"/backtests/{job_id}/exports/trades.csv")
                )
                seen = time.monotonic() + 15
                while not started.is_set() and time.monotonic() < seen:
                    await asyncio.sleep(0.01)
                assert started.is_set()
                health_started = time.monotonic()
                health = await http.get("/health")
                elapsed = time.monotonic() - health_started
                release.set()
                exported = await export_task
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            assert elapsed < 0.1
            assert exported.status_code == 200
            assert exported.text.startswith("trade_id,")
            assert len(exported.content) > 1_000_000
            assert rendered_on
            assert rendered_on[0] is not loop_thread

        asyncio.run(probe())
    finally:
        release.set()
        queue.close()
