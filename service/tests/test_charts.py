"""Chart window, indicator series, UTC trade buckets, and the 24h purge.

Bars in these tests are synthetic. Nothing here calls the network.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request

from app.auth import CurrentUser
from app.indicators import sma
from app.jobs.analysis import WEEKDAYS, trade_timing
from app.jobs.chart import CHART_SLOT_CAP, _window
from app.jobs.queue import JobQueue
from app.jobs.routes import _analysis_document, _bars_document, _indicators_document
from app.main import create_app
from app.tools.backtests import backtest_get
from app.tools.runtime import ToolRuntime
from test_backtest import _START
from test_jobs import _DAY, _MARKER, _client, _db_bytes, _payload, _row, _strategy, _wait


def test_window_prefers_the_earliest_slice_that_covers_the_most_trades() -> None:
    assert _window(5, [], 5) == (0, 5)
    assert _window(10, [], 4) == (6, 10)
    assert _window(10, [0, 1], 4) == (0, 4)
    assert _window(10, [7, 9], 4) == (6, 10)
    assert _window(10, [0, 9], 4) == (0, 4)
    assert CHART_SLOT_CAP == 2000


def test_bars_match_trade_log_times_and_indicators_use_the_library(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    closes = [10, 20, 30, 40]
    bars = [_row(index * 60, close) for index, close in enumerate(closes)]
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
    try:
        created = client.post("/backtests", json=_payload("sma", document, bars))
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        results = client.get(f"/backtests/{job_id}/results").json()
        chart = client.get(f"/backtests/{job_id}/bars")
        series = client.get(f"/backtests/{job_id}/indicators")
        assert chart.status_code == 200
        assert chart.headers["cache-control"] == "no-store"
        assert series.headers["cache-control"] == "no-store"
        body = chart.json()
        assert body["available"] is True
        assert body["bar_size"] == "1m"
        assert body["slots_total"] == 4
        assert body["slots_shown"] == 4
        asset = body["assets"][0]
        assert asset["asset_id"] == "sol"
        assert asset["shown"] == 4
        assert asset["total"] == 4
        assert asset["rejected"] == 0
        assert [bar["open_time"] for bar in asset["bars"]] == [
            _START + index * 60 for index in range(4)
        ]
        assert [bar["close"] for bar in asset["bars"]] == ["10", "20", "30", "40"]
        entry_times = {trade["entry_time"] for trade in results["trades"]}
        bar_times = {str(bar["open_time"]) for bar in asset["bars"]}
        assert entry_times
        assert entry_times <= bar_times
        for trade in results["trades"]:
            if trade["exit_time"] is not None:
                assert trade["exit_time"] in bar_times
        stored = sqlite3.connect(tmp_path / "jobs.sqlite").execute(
            "SELECT result_json FROM job_results WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        assert stored is not None
        assert "sma_2" not in stored[0]
        assert '"bars"' not in stored[0]
        indicator = series.json()
        assert indicator["available"] is True
        assert indicator["series"][0]["id"] == "sma_2"
        assert indicator["series"][0]["indicator"] == "sma"
        points = indicator["series"][0]["points"]
        expected = sma([float(close) for close in closes], 2)
        assert len(points) == len(expected)
        valid = 0
        for point, value in zip(points, expected, strict=True):
            if math.isfinite(value):
                assert point["value"] == json.dumps(value)
                valid += 1
            else:
                assert point["value"] is None
        assert indicator["series"][0]["valid"] == valid
        assert indicator["series"][0]["shown"] == 4
        assert valid < 4
    finally:
        queue.close()


def test_a_hole_is_not_filled_and_the_indicator_sees_the_gap(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    bars = [_row(0, 100), _row(120, 130)]
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
    try:
        created = client.post("/backtests", json=_payload("hole", document, bars))
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        chart = client.get(f"/backtests/{job_id}/bars").json()
        assert [bar["open_time"] for bar in chart["assets"][0]["bars"]] == [_START, _START + 120]
        assert _START + 60 not in {bar["open_time"] for bar in chart["assets"][0]["bars"]}
        points = client.get(f"/backtests/{job_id}/indicators").json()["series"][0]["points"]
        expected = sma([100.0, math.nan, 130.0], 2)
        assert [point["open_time"] for point in points] == [_START, _START + 60, _START + 120]
        for point, value in zip(points, expected, strict=True):
            if math.isfinite(value):
                assert float(point["value"]) == value
            else:
                assert point["value"] is None
    finally:
        queue.close()


def test_the_chart_window_is_capped_and_reports_shown_of_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.jobs.chart.CHART_SLOT_CAP", 2)
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    bars = [_row(index * 60, 100 + index) for index in range(5)]
    try:
        created = client.post("/backtests", json=_payload("cap", bars=bars))
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        body = client.get(f"/backtests/{job_id}/bars").json()
        assert body["slots_total"] == 5
        assert body["slots_shown"] == 2
        asset = body["assets"][0]
        assert asset["shown"] == 2
        assert asset["total"] == 5
        assert asset["shown"] < asset["total"]
        results = client.get(f"/backtests/{job_id}/results").json()
        shown = {bar["open_time"] for bar in asset["bars"]}
        for trade in results["trades"]:
            assert int(trade["entry_time"]) in shown
    finally:
        queue.close()


def test_chart_reads_do_not_parse_the_result_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    try:
        created = client.post("/backtests", json=_payload("parse"))
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        with sqlite3.connect(tmp_path / "jobs.sqlite") as conn:
            row = conn.execute(
                "SELECT result_json FROM job_results WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            assert row is not None
            stored = json.loads(row[0])
            stored["equity_curve"] = [{"open_time": 1, "equity": "EQUITY-ONLY-MARKER", "cash": "1"}]
            conn.execute(
                "UPDATE job_results SET result_json = ? WHERE job_id = ?",
                (json.dumps(stored), job_id),
            )
        real = json.loads

        def guarded(text: str, *args: object, **kwargs: object) -> object:
            if isinstance(text, str) and "EQUITY-ONLY-MARKER" in text:
                raise AssertionError("parsed the result row")
            return real(text, *args, **kwargs)

        monkeypatch.setattr("app.jobs.routes.json.loads", guarded)
        calls: list[str] = []
        real_result = queue.result_json

        def spy(user_id: str, ident: str) -> str | None:
            calls.append(ident)
            return real_result(user_id, ident)

        monkeypatch.setattr(queue, "result_json", spy)
        bars = client.get(f"/backtests/{job_id}/bars")
        indicators = client.get(f"/backtests/{job_id}/indicators")
        analysis = client.get(f"/backtests/{job_id}/analysis")
        assert bars.status_code == 200
        assert indicators.status_code == 200
        assert analysis.status_code == 200
        assert "EQUITY-ONLY-MARKER" not in bars.text
        assert "EQUITY-ONLY-MARKER" not in indicators.text
        assert "EQUITY-ONLY-MARKER" not in analysis.text
        assert calls == []
    finally:
        queue.close()


def test_analysis_uses_utc_entry_time_and_is_unavailable_when_empty(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    quiet = _strategy(signals=[{"id": "go", "expr": {"op": "lit", "value": False}}])
    try:
        created = client.post("/backtests", json=_payload("timed"))
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        body = client.get(f"/backtests/{job_id}/analysis").json()
        assert body["timezone"] == "UTC"
        assert body["available"] is True
        assert body["trades_used"] == body["trades_total"]
        assert body["trades_used"] >= 1
        assert "hours" in body
        assert len(body["hours"]) < 24
        assert len(body["weekdays"]) < 7
        assert all(item["count"] > 0 for item in body["hours"])
        assert all(item["count"] > 0 for item in body["weekdays"])
        instant = datetime.fromtimestamp(_START, UTC)
        assert instant == datetime(2026, 1, 1, tzinfo=UTC)
        assert body["hours"] == [{"hour_utc": instant.hour, "count": body["trades_used"]}]
        assert body["weekdays"] == [
            {
                "weekday_utc": instant.weekday(),
                "weekday": WEEKDAYS[instant.weekday()],
                "count": body["trades_used"],
            }
        ]
        assert body["weekdays"][0]["weekday"] == "Thursday"
        assert body["hours"][0]["hour_utc"] == 0

        empty = client.post("/backtests", json=_payload("empty", quiet))
        empty_id = empty.json()["job_id"]
        assert _wait(client, empty_id)["status"] == "succeeded"
        quiet_body = client.get(f"/backtests/{empty_id}/analysis").json()
        assert quiet_body["available"] is False
        assert quiet_body["trades_used"] == 0
        assert "hours" not in quiet_body
        assert "weekdays" not in quiet_body

        hostile = trade_timing(
            [
                {"entry_time": "=cmd"},
                {"entry_time": "-12"},
                {"entry_time": str(_START)},
                {"entry_time": None},
            ]
        )
        assert hostile["available"] is True
        assert hostile["trades_total"] == 4
        assert hostile["trades_used"] == 1
        assert hostile["hours"] == [{"hour_utc": 0, "count": 1}]
    finally:
        queue.close()


def test_a_chart_build_failure_is_visible_and_the_job_still_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("/tmp/secret-path")

    monkeypatch.setattr("app.jobs.chart._build", boom)
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    try:
        created = client.post("/backtests", json=_payload("chart-fail"))
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        bars = client.get(f"/backtests/{job_id}/bars")
        indicators = client.get(f"/backtests/{job_id}/indicators")
        assert bars.status_code == 200
        assert bars.json()["available"] is False
        assert bars.json()["reason"] == "the price chart could not be built"
        assert "secret-path" not in bars.text
        assert indicators.json()["available"] is False
        assert indicators.json()["reason"] == "indicators unavailable"
        assert client.get(f"/backtests/{job_id}/results").status_code == 200
    finally:
        queue.close()


def test_chart_bars_leave_the_database_and_the_wal(tmp_path: Path) -> None:
    now = {"t": 1_700_000_000.0}
    path = tmp_path / "jobs.sqlite"
    queue = JobQueue(path, clock=lambda: now["t"])
    client = _client(queue)
    try:
        created = client.post(
            "/backtests",
            json=_payload("ttl", bars=[_row(0, 100, high=31337.25, volume=424242.5)]),
        )
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        assert _MARKER.encode() in _db_bytes(path)
        assert b"31337.25" in _db_bytes(path)
        now["t"] += _DAY + 60
        expired = client.get(f"/backtests/{job_id}/bars")
        assert expired.status_code == 410
        assert expired.json()["error"]["code"] == "expired"
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM job_results").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM job_charts").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM job_bodies").fetchone() == (0,)
        assert _MARKER.encode() not in _db_bytes(path)
        assert b"31337.25" not in _db_bytes(path)
        wal = Path(str(path) + "-wal")
        if wal.exists():
            assert _MARKER.encode() not in wal.read_bytes()
            assert b"31337.25" not in wal.read_bytes()
        missing = client.get(f"/backtests/{job_id}/indicators")
        assert missing.status_code == 404
    finally:
        queue.close()


def test_an_orphan_chart_row_is_purged(tmp_path: Path) -> None:
    now = {"t": 1_700_000_000.0}
    path = tmp_path / "jobs.sqlite"
    queue = JobQueue(path, clock=lambda: now["t"], runner=lambda _body: {"ok": True})
    client = _client(queue)
    try:
        created = client.post("/backtests", json=_payload("orphan-chart"))
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        queue.close()
        with sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            conn.execute(
                """
                INSERT INTO job_charts (job_id, user_id, bars_json, indicators_json)
                VALUES (?, 'local-user', ?, ?)
                """,
                (job_id, '{"close":"31337.25"}', '{"value":"424242.5"}'),
            )
        now["t"] += _DAY + 60
        reopened = JobQueue(path, clock=lambda: now["t"], runner=lambda _body: {"ok": True})
        try:
            with sqlite3.connect(path) as conn:
                assert conn.execute("SELECT COUNT(*) FROM job_charts").fetchone() == (0,)
            assert b"31337.25" not in _db_bytes(path)
            assert _MARKER.encode() not in _db_bytes(path)
        finally:
            reopened.close()
    finally:
        if queue._lock_file is not None:
            queue.close()


def test_the_tool_result_does_not_carry_bars_or_indicator_series(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    try:
        created = client.post(
            "/backtests",
            json=_payload("tool", bars=[_row(0, 100, high=31337.25, volume=424242.5)]),
        )
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
        payload = backtest_get(
            {"job_id": job_id},
            CurrentUser(user_id="local-user"),
            ToolRuntime(jobs=queue),
        )
        text = json.dumps(payload)
        assert "31337.25" not in text
        assert _MARKER not in text
        assert "equity_curve" not in payload["result"]
        assert "bars" not in payload["result"]
        assert "series" not in payload["result"]
        bars = client.get(f"/backtests/{job_id}/bars")
        assert "31337.25" in bars.text
        assert _MARKER in bars.text
    finally:
        queue.close()


def test_chart_reads_refuse_the_event_loop() -> None:
    scope = {"type": "http", "headers": [], "app": create_app(jobs=None)}
    request = Request(scope)

    async def on_the_loop() -> None:
        _bars_document(request, "local-user", "job")

    with pytest.raises(RuntimeError, match="event loop"):
        asyncio.run(on_the_loop())

    async def indicators() -> None:
        _indicators_document(request, "local-user", "job")

    with pytest.raises(RuntimeError, match="event loop"):
        asyncio.run(indicators())

    async def analysis() -> None:
        _analysis_document(request, "local-user", "job")

    with pytest.raises(RuntimeError, match="event loop"):
        asyncio.run(analysis())


def test_health_answers_while_bars_are_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    started = threading.Event()
    release = threading.Event()
    seen: list[threading.Thread] = []
    real = _bars_document

    def wrapped(request: object, user_id: str, job_id: str) -> object:
        seen.append(threading.current_thread())
        started.set()
        assert release.wait(5)
        return real(request, user_id, job_id)

    monkeypatch.setattr("app.jobs.routes._bars_document", wrapped)
    try:
        created = client.post("/backtests", json=_payload("health-bars"))
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "succeeded"
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
                task = asyncio.create_task(http.get(f"/backtests/{job_id}/bars"))
                deadline = time.monotonic() + 5
                while not started.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert started.is_set()
                began = time.monotonic()
                health = await http.get("/health")
                elapsed = time.monotonic() - began
                release.set()
                body = await task
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            assert elapsed < 0.1
            assert body.status_code == 200
            assert body.json()["available"] is True
            assert seen
            assert seen[0] is not loop_thread

        asyncio.run(probe())
    finally:
        release.set()
        queue.close()
