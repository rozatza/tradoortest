"""CR kill-tests for the chart routes.

``test_cr17_small_float_spelling_matches_decimal_mark`` is the M1 check:
``_spell`` must emit text the chart grammar accepts.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from pathlib import Path

import pytest

from app.jobs.analysis import trade_timing
from app.jobs.chart import _spell, _trade_indexes, chart_documents
from app.jobs.export import csv_cell
from app.jobs.queue import JobExpired, JobQueue
from test_jobs import _client, _payload, _row, _strategy, _wait

_DECIMAL_MARK = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


def test_cr17_small_float_spelling_matches_decimal_mark() -> None:
    """A float under 1e-4 is a plain decimal, not an exponent."""
    for value in (2.2e-05, 9.99e-05, 1.234e-07, 0.00001):
        spelled = _spell(value)
        assert _DECIMAL_MARK.fullmatch(spelled), spelled


def test_cr17_csv_lone_cr_lead_and_exponent_and_pad_only() -> None:
    assert csv_cell("asset_id", "\rabc") == "'\rabc"
    assert csv_cell("asset_id", "-1e5") == "'-1e5"
    assert csv_cell("asset_id", "+1E3") == "'+1E3"
    assert csv_cell("asset_id", "   ") == "   "
    assert csv_cell("asset_id", "\u00a0") == "\u00a0"


def _doc(rows: list[dict]) -> dict:
    return {"ir": _strategy(), "bars": {"sol": rows}}


def test_cr17_bad_candle_rows_are_counted_rejected_not_drawn() -> None:
    rows = [_row(0, 100), _row(60, 101, synthetic="yes"), _row(120, 102)]
    rows[2]["close"] = float("nan")
    bars, _indicators = chart_documents(_doc(rows), {"trades": []})
    asset = bars["assets"][0]
    assert [candle["open_time"] for candle in asset["bars"]] == [rows[0]["open_time"]]
    assert asset["rejected"] == 2


def test_cr17_missing_volume_matches_engine_default() -> None:
    row = _row(0, 100)
    del row["volume"]
    bars, _indicators = chart_documents(_doc([row, _row(60, 101)]), {"trades": []})
    assert bars["assets"][0]["bars"][0]["volume"] == "1"


def test_cr17_trade_index_needs_ascii_digits() -> None:
    grid = [1_700_000_000]
    eastern = "\u0661\u0667" + "0" * 8
    assert _trade_indexes(grid, [{"entry_time": eastern}]) == []
    assert trade_timing([{"entry_time": True}])["trades_used"] == 0


def test_cr17_analysis_buckets_entry_not_exit() -> None:
    entry = 1_767_225_600 + 3 * 3600
    exit_time = entry + 26 * 3600
    out = trade_timing([{"entry_time": str(entry), "exit_time": str(exit_time)}])
    assert out["hours"] == [{"hour_utc": 3, "count": 1}]
    assert out["weekdays"] == [{"weekday_utc": 3, "weekday": "Thursday", "count": 1}]


def test_cr17_analysis_is_utc_even_if_process_tz_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin a non-UTC zone so a local-time bucket fails on a UTC CI runner."""
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    try:
        out = trade_timing([{"entry_time": "1767225600"}])
        assert out["hours"] == [{"hour_utc": 0, "count": 1}]
    finally:
        monkeypatch.undo()
        time.tzset()


def _succeeded(tmp_path: Path, now: dict | None = None):
    clock = (lambda: now["t"]) if now is not None else None
    queue = JobQueue(tmp_path / "jobs.sqlite", **({"clock": clock} if clock else {}))
    client = _client(queue)
    job_id = client.post("/backtests", json=_payload("cr17")).json()["job_id"]
    assert _wait(client, job_id)["status"] == "succeeded"
    return queue, client, job_id


def test_cr17_queue_chart_read_purges_on_its_own(tmp_path: Path) -> None:
    now = {"t": 1_800_000_000.0}
    queue, client, job_id = _succeeded(tmp_path, now)
    try:
        now["t"] += 86400 + 1
        with pytest.raises(JobExpired):
            queue.bars_json("local-user", job_id)
        wal = tmp_path / "jobs.sqlite-wal"
        assert not wal.exists() or wal.stat().st_size == 0
    finally:
        client.close()
        queue.close()


def test_cr17_queue_trades_json_is_owner_scoped(tmp_path: Path) -> None:
    queue, client, job_id = _succeeded(tmp_path)
    try:
        assert queue.trades_json("someone-else", job_id) == (None, None)
    finally:
        client.close()
        queue.close()


def test_cr17_indicators_read_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, client, job_id = _succeeded(tmp_path)
    seen: list[bool] = []
    original = JobQueue.indicators_json

    def spy(self, user_id, job_id_arg):
        try:
            asyncio.get_running_loop()
            seen.append(True)
        except RuntimeError:
            seen.append(False)
        return original(self, user_id, job_id_arg)

    monkeypatch.setattr(JobQueue, "indicators_json", spy)
    try:
        assert client.get(f"/backtests/{job_id}/indicators").status_code == 200
        assert seen == [False]
    finally:
        client.close()
        queue.close()


def test_cr17_failed_job_chart_reads_are_422(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite")
    client = _client(queue)
    try:
        created = client.post(
            "/backtests",
            json=_payload("bad", bars=[_row(0, -5), _row(60, 1)]),
        )
        job_id = created.json()["job_id"]
        assert _wait(client, job_id)["status"] == "failed"
        for path in ("bars", "indicators", "analysis"):
            assert client.get(f"/backtests/{job_id}/{path}").status_code == 422, path
    finally:
        client.close()
        queue.close()


def test_cr17_missing_trades_member_is_unavailable_not_422(tmp_path: Path) -> None:
    queue, client, job_id = _succeeded(tmp_path)
    try:
        connection = sqlite3.connect(tmp_path / "jobs.sqlite")
        connection.execute(
            "UPDATE job_results SET result_json = json_remove(result_json, '$.trades')"
        )
        connection.commit()
        connection.close()
        response = client.get(f"/backtests/{job_id}/analysis")
        assert response.status_code == 200
        assert response.json()["available"] is False
    finally:
        client.close()
        queue.close()
