"""Strategy, backtest, and HTTP coverage for the agent tool layer."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import LOCAL_USER_ID, CurrentUser, get_current_user
from app.backtest.errors import BacktestError
from app.dsl.format import format_strategy
from app.ir.validate import REPO_ROOT
from app.jobs.queue import MAX_RESULT_TTL_SECONDS, JobQueue, QueueBusy
from app.jobs.routes import _prepare
from app.jobs.run import run_backtest_job
from app.main import create_app
from app.strategies.compile import compile_body
from app.strategies.store import StrategyStore
from app.tools.dispatch import Dispatcher
from app.tools.registry import BY_NAME
from app.tools.routes import MAX_BODY_BYTES
from app.tools.runtime import ToolRuntime
from test_backtest import _START, _strategy

_USER = CurrentUser(user_id=LOCAL_USER_ID)
_OTHER = "other-user"
_EMA = json.loads((REPO_ROOT / "schemas/examples/ema-cross.json").read_text(encoding="utf-8"))
_DSL = format_strategy(_EMA)
_LEAKS = ("traceback", "sqlite", "operationalerror", "/tmp/", "/home/", ".env", "secret")


def _bars() -> dict[str, list[dict[str, int]]]:
    return {
        "sol": [
            {"open_time": _START, "close": 100},
            {"open_time": _START + 60, "close": 110},
        ]
    }


def _changed_ema() -> str:
    return _DSL.replace("warmup_bars: 26", "warmup_bars: 30", 1)


def _runtime(
    tmp_path: Path,
    *,
    queue: JobQueue | None = None,
) -> tuple[Dispatcher, StrategyStore, JobQueue]:
    store = StrategyStore(tmp_path / "strategies.sqlite", clock=lambda: "2026-01-01T00:00:00Z")
    jobs = queue or JobQueue(tmp_path / "jobs.sqlite", clock=lambda: 1_700_000_000.0)
    dispatcher = Dispatcher(ToolRuntime(strategies=store, jobs=jobs))
    return dispatcher, store, jobs


def _invoke(dispatcher: Dispatcher, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return dispatcher.invoke(name, arguments, _USER)


def _counts(path: Path) -> tuple[int, int, str | None]:
    with sqlite3.connect(path) as conn:
        versions = conn.execute("SELECT COUNT(*) FROM strategy_versions").fetchone()
        strategies = conn.execute("SELECT COUNT(*) FROM strategies").fetchone()
        updated = conn.execute("SELECT updated_at FROM strategies").fetchone()
    assert versions is not None and strategies is not None
    stamp = None if updated is None else str(updated[0])
    return int(versions[0]), int(strategies[0]), stamp


def _no_leak(payload: object, *extra: str) -> None:
    text = json.dumps(payload).lower()
    for token in _LEAKS:
        assert token not in text
    for item in extra:
        assert item.lower() not in text


def _wait(dispatcher: Dispatcher, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _invoke(dispatcher, "backtest_status", {"job_id": job_id})
        if not last["ok"] or last["data"]["status"] in {"succeeded", "failed"}:
            return last
        time.sleep(0.01)
    return last


@pytest.fixture
def saved(tmp_path: Path) -> Iterator[tuple[Dispatcher, StrategyStore, JobQueue, str]]:
    dispatcher, store, jobs = _runtime(tmp_path)
    try:
        wrote = _invoke(dispatcher, "write_strategy", {"dsl": _DSL})
        assert wrote["ok"] is True
        yield dispatcher, store, jobs, wrote["data"]["strategy_id"]
    finally:
        store.close()
        jobs.close()


def test_write_strategy_stores_version_one_and_rejects_invalid_input(tmp_path: Path) -> None:
    dispatcher, store, jobs = _runtime(tmp_path)
    path = tmp_path / "strategies.sqlite"
    try:
        wrote = _invoke(dispatcher, "write_strategy", {"dsl": _DSL})
        assert wrote["ok"] is True
        assert wrote["data"]["version"] == 1
        assert wrote["data"]["status"] == "active"
        assert wrote["data"]["name"]
        record = store.find(LOCAL_USER_ID, wrote["data"]["strategy_id"])
        assert record is not None
        assert record.user_id == LOCAL_USER_ID
        before = _counts(path)
        rejected = _invoke(dispatcher, "write_strategy", {"dsl": "not a strategy"})
        assert rejected["ok"] is False
        assert rejected["error"]["details"]
        assert _counts(path) == before
        both = _invoke(dispatcher, "write_strategy", {"dsl": _DSL, "ir": _EMA})
        assert both["error"]["code"] == "validation_error"
        assert _counts(path) == before
    finally:
        store.close()
        jobs.close()


def test_preview_does_not_write_and_apply_requires_confirm_and_version(
    saved: tuple[Dispatcher, StrategyStore, JobQueue, str],
) -> None:
    dispatcher, store, _jobs, strategy_id = saved
    database = store.path
    before = _counts(database)
    same = _invoke(dispatcher, "update_strategy", {"strategy_id": strategy_id, "ir": _EMA})
    assert same["ok"] is True
    assert same["data"]["mode"] == "preview"
    assert same["data"]["mutated"] is False
    assert same["data"]["from_version"] == 1
    assert same["data"]["to_version"] == 2
    changed = _changed_ema()
    preview = _invoke(
        dispatcher,
        "update_strategy",
        {
            "strategy_id": strategy_id,
            "dsl": changed,
            "mode": "preview",
            "confirm": True,
            "expected_version": 1,
        },
    )
    assert preview["data"]["changed"] is True
    assert preview["data"]["mutated"] is False
    assert "warmup_bars" in preview["data"]["dsl_diff"]
    assert preview["data"]["ir_diff"]
    assert _counts(database) == before
    unconfirmed = _invoke(
        dispatcher,
        "update_strategy",
        {"strategy_id": strategy_id, "dsl": changed, "mode": "apply", "expected_version": 1},
    )
    assert unconfirmed["error"]["code"] == "confirmation_required"
    assert unconfirmed["error"]["path"] == "$.confirm"
    missing = _invoke(
        dispatcher,
        "update_strategy",
        {"strategy_id": strategy_id, "dsl": changed, "mode": "apply", "confirm": True},
    )
    assert missing["error"]["code"] == "validation_error"
    assert missing["error"]["path"] == "$.expected_version"
    assert _counts(database) == before
    applied = _invoke(
        dispatcher,
        "update_strategy",
        {
            "strategy_id": strategy_id,
            "dsl": changed,
            "mode": "apply",
            "confirm": True,
            "expected_version": 1,
        },
    )
    assert applied["ok"] is True
    assert applied["data"]["mutated"] is True
    assert applied["data"]["version"] == 2
    assert store.get(LOCAL_USER_ID, strategy_id)[0].latest_version == 2


def test_stale_version_and_hash_do_not_write(
    saved: tuple[Dispatcher, StrategyStore, JobQueue, str],
) -> None:
    dispatcher, store, _jobs, strategy_id = saved
    stale = _invoke(
        dispatcher,
        "update_strategy",
        {
            "strategy_id": strategy_id,
            "dsl": _changed_ema(),
            "mode": "apply",
            "confirm": True,
            "expected_version": 9,
        },
    )
    assert stale["error"]["code"] == "version_conflict"
    assert stale["error"]["details"]["current_version"] == 1
    assert store.get(LOCAL_USER_ID, strategy_id)[0].latest_version == 1
    current = store.get_version(LOCAL_USER_ID, strategy_id, 1)
    wrong_hash = _invoke(
        dispatcher,
        "update_strategy",
        {
            "strategy_id": strategy_id,
            "dsl": _changed_ema(),
            "mode": "apply",
            "confirm": True,
            "expected_version": 1,
            "expected_ir_hash": "ab" * 32,
        },
    )
    assert wrong_hash["error"]["code"] == "version_conflict"
    assert wrong_hash["error"]["details"]["current_ir_hash"] == current.ir_hash
    assert store.get(LOCAL_USER_ID, strategy_id)[0].latest_version == 1
    matched = _invoke(
        dispatcher,
        "update_strategy",
        {
            "strategy_id": strategy_id,
            "dsl": _changed_ema(),
            "mode": "apply",
            "confirm": True,
            "expected_version": 1,
            "expected_ir_hash": current.ir_hash,
        },
    )
    assert matched["ok"] is True
    assert matched["data"]["version"] == 2


def test_overlapping_applies_keep_one_version(
    saved: tuple[Dispatcher, StrategyStore, JobQueue, str],
) -> None:
    dispatcher, store, _jobs, strategy_id = saved
    barrier = threading.Barrier(2)
    results: list[dict[str, Any]] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait(2)
        body = _invoke(
            dispatcher,
            "update_strategy",
            {
                "strategy_id": strategy_id,
                "dsl": _changed_ema(),
                "mode": "apply",
                "confirm": True,
                "expected_version": 1,
            },
        )
        with lock:
            results.append(body)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(results) == 2
    codes = [item["error"]["code"] if not item["ok"] else "ok" for item in results]
    assert sorted(codes) == ["ok", "version_conflict"]
    assert store.get(LOCAL_USER_ID, strategy_id)[0].latest_version == 2


def test_identical_apply_does_not_append_and_put_still_does(
    saved: tuple[Dispatcher, StrategyStore, JobQueue, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher, store, _jobs, strategy_id = saved
    current = store.get_version(LOCAL_USER_ID, strategy_id, 1)
    same = _invoke(
        dispatcher,
        "update_strategy",
        {
            "strategy_id": strategy_id,
            "dsl": current.dsl,
            "mode": "apply",
            "confirm": True,
            "expected_version": 1,
            "expected_ir_hash": current.ir_hash,
        },
    )
    assert same["ok"] is False
    assert same["error"]["code"] == "no_changes"
    assert same["error"]["message"] == "strategy is unchanged"
    assert store.get(LOCAL_USER_ID, strategy_id)[0].latest_version == 1
    assert _counts(store.path)[0] == 1
    monkeypatch.chdir(tmp_path)
    application = create_app(strategies=store)
    client = TestClient(application)
    put = client.put(f"/strategies/{strategy_id}", json={"dsl": current.dsl})
    assert put.status_code == 200
    assert put.json()["latest_version"] == 2
    assert store.get(LOCAL_USER_ID, strategy_id)[0].latest_version == 2


def test_apply_rejects_a_stale_predecessor_before_add_version(
    saved: tuple[Dispatcher, StrategyStore, JobQueue, str],
) -> None:
    dispatcher, store, _jobs, strategy_id = saved
    first = _invoke(
        dispatcher,
        "update_strategy",
        {
            "strategy_id": strategy_id,
            "dsl": _changed_ema(),
            "mode": "apply",
            "confirm": True,
            "expected_version": 1,
        },
    )
    assert first["ok"] is True
    assert first["data"]["from_version"] == 1
    assert first["data"]["version"] == 2
    assert first["data"]["dsl_diff"].startswith("--- v1\n+++ v2\n")
    version_two = store.get_version(LOCAL_USER_ID, strategy_id, 2)
    further = version_two.dsl.replace("warmup_bars: 30", "warmup_bars: 40", 1)
    chained = _invoke(
        dispatcher,
        "update_strategy",
        {
            "strategy_id": strategy_id,
            "dsl": further,
            "mode": "apply",
            "confirm": True,
            "expected_version": 2,
            "expected_ir_hash": version_two.ir_hash,
        },
    )
    assert chained["ok"] is True
    assert chained["data"]["from_version"] == 2
    assert chained["data"]["version"] == 3
    assert chained["data"]["from_ir_hash"] == version_two.ir_hash
    assert chained["data"]["dsl_diff"].startswith("--- v2\n+++ v3\n")
    calls = {"add": 0}
    original_add = store.add_version
    original_get = store.get_version

    def guarded(*args: object, **kwargs: object):
        calls["add"] += 1
        return original_add(*args, **kwargs)

    def stuck(*_args: object, **_kwargs: object):
        return original_get(LOCAL_USER_ID, strategy_id, 1)

    store.add_version = guarded  # type: ignore[method-assign]
    store.get_version = stuck  # type: ignore[method-assign]
    raced = _invoke(
        dispatcher,
        "update_strategy",
        {
            "strategy_id": strategy_id,
            "dsl": further.replace("warmup_bars: 40", "warmup_bars: 50", 1),
            "mode": "apply",
            "confirm": True,
            "expected_version": 2,
        },
    )
    assert raced["error"]["code"] == "version_conflict"
    assert raced["error"]["details"]["current_version"] == 1
    assert calls["add"] == 0
    assert "dsl_diff" not in raced.get("data", {})
    assert store.get(LOCAL_USER_ID, strategy_id)[0].latest_version == 3


def test_add_version_expected_version_is_the_latest_row(tmp_path: Path) -> None:
    from app.strategies.errors import VersionConflict

    store = StrategyStore(tmp_path / "strategies.sqlite", clock=lambda: "2026-01-01T00:00:00Z")
    try:
        first = compile_body(json.dumps({"dsl": _DSL}).encode("utf-8"))
        record, version = store.create(LOCAL_USER_ID, first)
        second = compile_body(json.dumps({"dsl": _changed_ema()}).encode("utf-8"))
        store.add_version(LOCAL_USER_ID, record.strategy_id, second)
        with pytest.raises(VersionConflict) as exc:
            store.add_version(
                LOCAL_USER_ID,
                record.strategy_id,
                first,
                expected_version=1,
            )
        assert exc.value.version == 2
        assert store.get(LOCAL_USER_ID, record.strategy_id)[0].latest_version == 2
        assert version.version == 1
    finally:
        store.close()


def test_add_version_without_a_precondition_still_appends(tmp_path: Path) -> None:
    store = StrategyStore(tmp_path / "strategies.sqlite", clock=lambda: "2026-01-01T00:00:00Z")
    try:
        first = compile_body(json.dumps({"dsl": _DSL}).encode("utf-8"))
        record, version = store.create(LOCAL_USER_ID, first)
        assert version.version == 1
        second = compile_body(json.dumps({"dsl": _changed_ema()}).encode("utf-8"))
        _updated, saved = store.add_version(LOCAL_USER_ID, record.strategy_id, second)
        assert saved.version == 2
    finally:
        store.close()


def test_other_users_strategies_and_jobs_are_not_found(tmp_path: Path) -> None:
    dispatcher, store, jobs = _runtime(tmp_path)
    try:
        compiled = compile_body(json.dumps({"ir": _strategy()}).encode("utf-8"))
        record, version = store.create(_OTHER, compiled)
        hidden = record.strategy_id
        for name, arguments in (
            ("update_strategy", {"strategy_id": hidden, "ir": _strategy()}),
            ("backtest_start", {"strategy_id": hidden, "bars": _bars(), "request_key": "x"}),
        ):
            failed = _invoke(dispatcher, name, arguments)
            assert failed["error"]["code"] == "not_found"
            assert version.ir_hash not in json.dumps(failed)
        assert store.get(_OTHER, hidden)[0].latest_version == 1
    finally:
        store.close()
        jobs.close()


def test_archived_and_missing_strategies(tmp_path: Path) -> None:
    dispatcher, store, jobs = _runtime(tmp_path)
    try:
        wrote = _invoke(dispatcher, "write_strategy", {"ir": _strategy()})
        strategy_id = wrote["data"]["strategy_id"]
        store.archive(LOCAL_USER_ID, strategy_id)
        archived = _invoke(
            dispatcher,
            "update_strategy",
            {"strategy_id": strategy_id, "ir": _strategy(), "mode": "preview"},
        )
        assert archived["error"]["code"] == "strategy_archived"
        start = _invoke(
            dispatcher,
            "backtest_start",
            {"strategy_id": strategy_id, "bars": _bars(), "request_key": "gone"},
        )
        assert start["error"]["code"] == "strategy_archived"
        missing = _invoke(
            dispatcher,
            "update_strategy",
            {"strategy_id": "ab" * 16, "ir": _strategy()},
        )
        assert missing["error"]["code"] == "not_found"
        with sqlite3.connect(tmp_path / "jobs.sqlite") as conn:
            jobs_saved = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
        assert jobs_saved is not None
        assert int(jobs_saved[0]) == 0
    finally:
        store.close()
        jobs.close()


def test_owner_isolation_for_a_seeded_second_user(tmp_path: Path) -> None:
    dispatcher, store, jobs = _runtime(tmp_path)
    try:
        compiled = compile_body(json.dumps({"ir": _strategy()}).encode("utf-8"))
        record, version = store.create(_OTHER, compiled)
        body = _prepare({"ir": _strategy(), "bars": _bars()})
        other_job = jobs.start(user_id=_OTHER, request_key="other", body=body)
        status = _invoke(dispatcher, "backtest_status", {"job_id": other_job.job_id})
        result = _invoke(dispatcher, "backtest_get", {"job_id": other_job.job_id})
        assert status["error"]["code"] == "not_found"
        assert result["error"]["code"] == "not_found"
        assert version.ir_hash not in json.dumps(status)
        assert other_job.job_id not in json.dumps(status["error"])
        preview = _invoke(
            dispatcher,
            "update_strategy",
            {"strategy_id": record.strategy_id, "dsl": format_strategy(_strategy())},
        )
        assert preview["error"]["code"] == "not_found"
    finally:
        store.close()
        jobs.close()


def test_backtest_tools_wrap_the_queue(tmp_path: Path) -> None:
    dispatcher, store, jobs = _runtime(tmp_path)
    try:
        wrote = _invoke(dispatcher, "write_strategy", {"ir": _strategy()})
        strategy_id = wrote["data"]["strategy_id"]
        started = _invoke(
            dispatcher,
            "backtest_start",
            {"strategy_id": strategy_id, "bars": _bars(), "request_key": "one"},
        )
        assert started["ok"] is True
        assert started["data"]["created"] is True
        job_id = started["data"]["job_id"]
        status = _wait(dispatcher, job_id)
        assert status["data"]["status"] == "succeeded"
        assert status["data"]["started_at"] is None
        assert status["data"]["finished_at"] is None
        loaded = _invoke(dispatcher, "backtest_get", {"job_id": job_id})
        assert loaded["data"]["ready"] is True
        assert loaded["data"]["result"]["schema"] == "tradoortest.backtest_result.v1"
        assert loaded["data"]["result"]["final_equity"] == "10600.00"
        replay = _invoke(
            dispatcher,
            "backtest_start",
            {"strategy_id": strategy_id, "bars": _bars(), "request_key": "one"},
        )
        assert replay["data"]["created"] is False
        assert replay["data"]["job_id"] == job_id
        reused = _invoke(
            dispatcher,
            "backtest_start",
            {
                "strategy_id": strategy_id,
                "bars": {
                    "sol": [
                        {"open_time": _START, "close": 1},
                        {"open_time": _START + 60, "close": 2},
                    ]
                },
                "request_key": "one",
            },
        )
        assert reused["error"]["code"] == "conflict"
        inline = _invoke(
            dispatcher,
            "backtest_start",
            {"ir": _strategy(), "bars": _bars(), "request_key": "inline"},
        )
        assert _wait(dispatcher, inline["data"]["job_id"])["data"]["status"] == "succeeded"
        assert jobs.result_ttl_seconds == MAX_RESULT_TTL_SECONDS
        with sqlite3.connect(tmp_path / "jobs.sqlite") as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in rows}
            bodies = conn.execute("SELECT COUNT(*) FROM job_bodies").fetchone()
        assert "bars" not in tables
        assert bodies is not None
        assert int(bodies[0]) == 0
    finally:
        store.close()
        jobs.close()


def test_backtest_version_limit_expiry_and_failure(tmp_path: Path) -> None:
    clock = {"t": 1_700_000_000.0}
    seen: list[int] = []
    started = threading.Event()
    release = threading.Event()

    def runner(body: Mapping[str, Any]) -> dict[str, Any]:
        seen.append(int(body["ir"]["engine"]["warmup_bars"]))
        if not started.is_set():
            started.set()
            assert release.wait(5)
        return run_backtest_job(body)

    queue = JobQueue(
        tmp_path / "jobs.sqlite",
        clock=lambda: clock["t"],
        max_concurrent=1,
        per_minute=10,
        runner=runner,
    )
    dispatcher, store, jobs = _runtime(tmp_path, queue=queue)
    try:
        wrote = _invoke(dispatcher, "write_strategy", {"ir": _strategy()})
        strategy_id = wrote["data"]["strategy_id"]
        changed = json.loads(json.dumps(_strategy()))
        changed["engine"] = dict(_strategy()["engine"])
        changed["engine"]["warmup_bars"] = 1
        applied = _invoke(
            dispatcher,
            "update_strategy",
            {
                "strategy_id": strategy_id,
                "ir": changed,
                "mode": "apply",
                "confirm": True,
                "expected_version": 1,
            },
        )
        assert applied["ok"] is True
        first = _invoke(
            dispatcher,
            "backtest_start",
            {"strategy_id": strategy_id, "version": 1, "bars": _bars(), "request_key": "v1"},
        )
        assert started.wait(5)
        blocked = _invoke(
            dispatcher,
            "backtest_start",
            {"strategy_id": strategy_id, "version": 2, "bars": _bars(), "request_key": "v2"},
        )
        assert blocked["error"]["code"] == "limit_reached"
        assert blocked["error"]["retryable"] is True
        release.set()
        assert _wait(dispatcher, first["data"]["job_id"])["data"]["status"] == "succeeded"
        second = _invoke(
            dispatcher,
            "backtest_start",
            {"strategy_id": strategy_id, "version": 2, "bars": _bars(), "request_key": "v2"},
        )
        assert _wait(dispatcher, second["data"]["job_id"])["data"]["status"] == "succeeded"
        assert seen[:2] == [0, 1]

        def fail(_body: Mapping[str, Any]) -> dict[str, Any]:
            raise BacktestError("bars are not usable", code="invalid_bars")

        jobs._runner = fail
        failed = _invoke(
            dispatcher,
            "backtest_start",
            {"ir": _strategy(), "bars": _bars(), "request_key": "bad"},
        )
        failed_status = _wait(dispatcher, failed["data"]["job_id"])
        assert failed_status["data"]["status"] == "failed"
        loaded = _invoke(dispatcher, "backtest_get", {"job_id": failed["data"]["job_id"]})
        assert loaded["data"]["ready"] is False
        assert loaded["data"]["result"] is None
        assert loaded["data"]["error"]["code"] == "invalid_bars"
        clock["t"] += MAX_RESULT_TTL_SECONDS + 1
        expired = _invoke(dispatcher, "backtest_status", {"job_id": first["data"]["job_id"]})
        assert expired["error"]["code"] == "expired"
        missing = _invoke(dispatcher, "backtest_get", {"job_id": "cd" * 16})
        assert missing["error"]["code"] == "not_found"
    finally:
        release.set()
        store.close()
        jobs.close()


def test_queue_busy_is_service_unavailable(
    saved: tuple[Dispatcher, StrategyStore, JobQueue, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher, _store, jobs, strategy_id = saved

    def busy(*_args: object, **_kwargs: object) -> None:
        raise QueueBusy()

    monkeypatch.setattr(jobs, "start", busy)
    failed = _invoke(
        dispatcher,
        "backtest_start",
        {"strategy_id": strategy_id, "bars": _bars(), "request_key": "busy"},
    )
    assert failed["error"]["code"] == "service_unavailable"
    assert failed["error"]["retryable"] is True
    _no_leak(failed)


def test_rate_limit_and_generated_request_key(tmp_path: Path) -> None:
    clock = {"t": 1_700_000_000.0}
    queue = JobQueue(
        tmp_path / "jobs.sqlite",
        clock=lambda: clock["t"],
        max_concurrent=2,
        per_minute=1,
    )
    dispatcher, store, jobs = _runtime(tmp_path, queue=queue)
    try:
        first = _invoke(dispatcher, "backtest_start", {"ir": _strategy(), "bars": _bars()})
        second = _invoke(
            dispatcher,
            "backtest_start",
            {"ir": _strategy(), "bars": _bars(), "request_key": "second"},
        )
        assert first["ok"] is True
        assert len(first["data"]["request_key"]) == 32
        assert second["error"]["code"] == "rate_limited"
        assert second["error"]["retryable"] is True
        assert _wait(dispatcher, first["data"]["job_id"])["data"]["status"] == "succeeded"
    finally:
        store.close()
        jobs.close()


def _invoke_http(
    app: Any,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    query: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    receive: Any = None,
) -> tuple[int, bytes]:
    header_list = list(headers or [])
    if body is not None and not any(name == b"content-length" for name, _value in header_list):
        header_list.append((b"content-length", str(len(body)).encode("ascii")))
    sent: list[dict[str, Any]] = []

    async def default_receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body or b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query,
        "headers": header_list,
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
        "root_path": "",
    }
    asyncio.run(app(scope, receive or default_receive, send))
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    raw = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return status, raw


def test_http_surface_ignores_spoofed_identity_and_caps_the_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    store = StrategyStore(tmp_path / "strategies.sqlite", clock=lambda: "2026-01-01T00:00:00Z")
    application = create_app(strategies=store)
    client = TestClient(application)
    try:
        listed = client.get("/tools", params={"user_id": _OTHER})
        assert listed.status_code == 200
        assert [item["name"] for item in listed.json()["tools"]][0] == "search_asset"
        wrote = client.post(
            "/tools/write_strategy",
            json={"dsl": _DSL},
            headers={"X-User-Id": _OTHER},
            params={"owner_id": _OTHER},
        )
        assert wrote.status_code == 200
        strategy_id = wrote.json()["data"]["strategy_id"]
        assert store.find(LOCAL_USER_ID, strategy_id) is not None
        assert store.find(_OTHER, strategy_id) is None
        unknown = client.post("/tools/NotATool", json={})
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "unknown_tool"
        assert "NotATool" not in unknown.text
        nan = client.post(
            "/tools/add_chart",
            content=b'{"symbol":"SOL","timeframe":"1h","visible_bars":NaN}',
            headers={"content-type": "application/json"},
        )
        assert nan.status_code == 422
        assert nan.json()["error"]["code"] == "invalid_json"
        nul = client.post(
            "/tools/search_asset",
            content=b'{"query":"SOL\x00"}',
            headers={"content-type": "application/json"},
        )
        assert nul.status_code == 422
        assert nul.json()["error"]["code"] == "invalid_string"
        stale = client.post(
            "/tools/update_strategy",
            json={
                "strategy_id": strategy_id,
                "dsl": _changed_ema(),
                "mode": "apply",
                "confirm": True,
                "expected_version": 4,
            },
        )
        assert stale.status_code == 409
        assert stale.json()["ok"] is False
        assert stale.json()["error"]["code"] == "version_conflict"
    finally:
        store.close()

    monkeypatch.setattr("app.tools.routes.MAX_BODY_BYTES", 32)
    application = create_app()
    oversized = {"called": False}

    async def unread() -> dict[str, Any]:
        oversized["called"] = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    status, raw = _invoke_http(
        application,
        "POST",
        "/tools/validate",
        headers=[(b"content-type", b"application/json"), (b"content-length", b"999999")],
        receive=unread,
    )
    assert status == 413
    assert oversized["called"] is False
    assert json.loads(raw)["error"]["code"] == "payload_too_large"
    assert b"ok" not in raw
    state = {"calls": 0}

    async def receive() -> dict[str, Any]:
        state["calls"] += 1
        if state["calls"] == 1:
            return {"type": "http.request", "body": b"x" * 20, "more_body": True}
        if state["calls"] == 2:
            return {"type": "http.request", "body": b"y" * 20, "more_body": True}
        raise AssertionError("the tail of a capped body was read")

    status, raw = _invoke_http(
        application,
        "POST",
        "/tools/validate",
        headers=[(b"content-type", b"application/json")],
        receive=receive,
    )
    assert status == 413
    assert state["calls"] == 2
    assert json.loads(raw)["error"]["code"] == "payload_too_large"
    assert MAX_BODY_BYTES == 1_048_576


def test_locked_corrupt_and_unexpected_failures_do_not_leak_or_poison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "strategies.sqlite"
    store = StrategyStore(path, busy_timeout_ms=0, clock=lambda: "2026-01-01T00:00:00Z")
    application = create_app(strategies=store)
    client = TestClient(application, raise_server_exceptions=False)
    other = sqlite3.connect(path, isolation_level=None, timeout=0)
    other.execute("BEGIN EXCLUSIVE")
    try:
        locked = client.post("/tools/write_strategy", json={"dsl": _DSL})
        assert locked.status_code == 503
        assert locked.json()["error"]["code"] == "service_unavailable"
        assert locked.json()["error"]["retryable"] is True
        _no_leak(locked.json(), "locked", str(path))
        assert client.get("/health").json() == {"status": "ok"}
    finally:
        other.execute("ROLLBACK")
        other.close()
    recovered = client.post("/tools/write_strategy", json={"dsl": _DSL})
    assert recovered.status_code == 200
    store.close()

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("secret at /tmp/hidden.sqlite")

    live = StrategyStore(tmp_path / "live.sqlite", clock=lambda: "2026-01-01T00:00:00Z")
    monkeypatch.setattr(live, "create", explode)
    application = create_app(strategies=live)
    client = TestClient(application, raise_server_exceptions=False)
    blown = client.post("/tools/write_strategy", json={"dsl": _DSL})
    assert blown.status_code == 500
    assert blown.json()["error"]["code"] == "internal_error"
    _no_leak(blown.json(), "hidden", "RuntimeError")
    live.close()

    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a database")
    monkeypatch.setenv("STRATEGIES_DB_PATH", str(corrupt))
    application = create_app()
    client = TestClient(application, raise_server_exceptions=False)
    failed = client.post("/tools/write_strategy", json={"dsl": _DSL})
    assert failed.status_code == 503
    assert failed.json()["ok"] is False
    assert failed.json()["error"]["code"] == "service_unavailable"
    _no_leak(failed.json(), str(corrupt), "not a database")
    assert application.state.strategies is None
    monkeypatch.setenv("STRATEGIES_DB_PATH", str(tmp_path / "ok.sqlite"))
    opened = client.post("/tools/write_strategy", json={"dsl": _DSL})
    assert opened.status_code == 200, opened.text
    assert isinstance(application.state.strategies, StrategyStore)
    application.state.strategies.close()


_ASSERTION = "secret /tmp/strategies.sqlite redacted-test-token-value"


def _assert_internal(body: Mapping[str, Any], raw: str) -> None:
    assert body["ok"] is False
    assert body["tool"] == "validate"
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == "the tool could not finish"
    assert "AssertionError" not in raw
    assert "Internal Server Error" not in raw
    for token in ("secret", "/tmp/", "strategies.sqlite", "redacted-test-token-value"):
        assert token not in raw


def _boom(*_args: object, **_kwargs: object) -> dict[str, Any]:
    raise AssertionError(_ASSERTION)


def test_assertion_error_stays_inside_the_tool_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    spec = BY_NAME["validate"]
    monkeypatch.setitem(BY_NAME, "validate", replace(spec, handler=_boom))
    dispatcher = Dispatcher(ToolRuntime())
    failed = dispatcher.invoke("validate", {"dsl": "x"}, _USER)
    _assert_internal(failed, json.dumps(failed))
    application = create_app()
    client = TestClient(application, raise_server_exceptions=False)
    response = client.post("/tools/validate", json={"dsl": "x"})
    assert response.status_code == 500
    _assert_internal(response.json(), response.text)


def test_health_stays_responsive_while_a_tool_call_blocks(tmp_path: Path) -> None:
    store = StrategyStore(tmp_path / "strategies.sqlite", clock=lambda: "2026-01-01T00:00:00Z")
    application = create_app(strategies=store)
    entered = threading.Event()
    release = threading.Event()
    original = store.create

    def slow(user_id: str, compiled: Any) -> Any:
        entered.set()
        release.wait(3)
        return original(user_id, compiled)

    store.create = slow  # type: ignore[method-assign]

    async def probe() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.monotonic()
            write = asyncio.create_task(client.post("/tools/write_strategy", json={"dsl": _DSL}))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            saw = time.monotonic()
            health_started = time.monotonic()
            health = await client.get("/health")
            health_done = time.monotonic()
            release.set()
            saved = await write
        assert entered.is_set()
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert saved.status_code == 200
        assert saw - started < 1
        assert health_done - health_started < 0.3

    try:
        asyncio.run(probe())
    finally:
        release.set()
        store.close()


def test_non_local_user_is_forbidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    application = create_app()
    application.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id=_OTHER)
    client = TestClient(application)
    listed = client.get("/tools")
    called = client.post("/tools/validate", json={"dsl": "nope"})
    assert listed.status_code == 403
    assert called.status_code == 403
    assert listed.json()["error"]["code"] == "forbidden"
    assert application.state.strategies is None
