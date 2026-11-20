"""Review fixes for the any-mint cache. No live socket."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import LOCAL_USER_ID
from app.main import create_app
from app.ohlcv.adapters.any_mint import MEMORY_TTL_SECONDS, AnyMintSource
from app.ohlcv.adapters.birdeye import BirdeyeSource, CuLedger
from app.ohlcv.backfill import main, plan_window
from app.ohlcv.bars import Bar
from app.ohlcv.errors import (
    ComputeBudgetExceeded,
    IngestExpired,
    NoDexPoolYet,
    SourceError,
)
from app.ohlcv.ingest import run_mint_ingest
from app.ohlcv.quality import annotate_series
from app.ohlcv.registry import assets_from_mints
from app.ohlcv.routes import OhlcvRuntime
from app.ohlcv.source import FetchPage
from app.ohlcv.store import SqliteBarStore

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
_KEY = "test-birdeye-key"
_NOW = 1_700_086_400
_FIXTURES = Path(__file__).parent / "fixtures" / "ohlcv"
_POOLS = json.loads((_FIXTURES / "geckoterminal_pools_synthetic.json").read_text(encoding="utf-8"))
_PUMP = json.loads((_FIXTURES / "geckoterminal_pump_synthetic.json").read_text(encoding="utf-8"))


def test_secure_delete_is_enabled(tmp_path: Path) -> None:
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    try:
        row = store._conn.execute("PRAGMA secure_delete").fetchone()
    finally:
        store.close()
    assert row is not None
    assert row[0] == 1


def test_expired_ingest_rows_are_deleted(tmp_path: Path) -> None:
    clock = {"now": 1_700_000_000}
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite", ttl_seconds=60, now=lambda: clock["now"])
    try:
        store.save_ingest_result(
            user_id=LOCAL_USER_ID, job_id="job-1", payload={"kept": True}
        )
        clock["now"] += 60
        with pytest.raises(IngestExpired):
            store.read_ingest_result(user_id=LOCAL_USER_ID, job_id="job-1")
        count = store._conn.execute("SELECT COUNT(*) FROM ingest_results").fetchone()
    finally:
        store.close()
    assert count is not None
    assert count[0] == 0


def test_locked_store_is_503_on_get_and_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    runtime = OhlcvRuntime(store, _quiet_source(), lambda: _NOW)
    client = TestClient(create_app(ohlcv=runtime), raise_server_exceptions=False)

    def locked(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "read_ingest_result", locked)
    got = client.get("/ohlcv/jobs/job-1")
    assert got.status_code == 503
    assert got.json()["error"] == {
        "code": "store_unavailable",
        "message": "the local store is unavailable",
        "path": None,
    }
    assert "locked" not in got.text
    assert "sqlite" not in got.text.lower()

    def explode(**_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("app.ohlcv.routes.run_mint_ingest", explode)
    posted = client.post("/ohlcv/jobs", json={"mints": [MINT], "timeframes": ["1d"], "days": 1})
    store.close()
    assert posted.status_code == 503
    assert posted.json()["error"]["code"] == "store_unavailable"
    assert "ingest_failed" not in posted.text
    assert "Internal Server Error" not in posted.text
    assert "locked" not in posted.text


def test_corrupt_and_unwritable_store_stay_in_the_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ohlcv.routes as routes

    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a database")
    monkeypatch.setattr(routes, "_DEFAULT_DB", corrupt)
    client = TestClient(create_app(), raise_server_exceptions=False)
    got = client.get("/ohlcv/jobs/abc")
    assert got.status_code == 503
    assert got.json()["error"]["code"] == "store_unavailable"
    assert str(corrupt) not in got.text
    assert "not a database" not in got.text
    assert "sqlite" not in got.text.lower()
    assert "Traceback" not in got.text

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(routes, "_DEFAULT_DB", blocker / "ohlcv.sqlite")
    again = TestClient(create_app(), raise_server_exceptions=False)
    blocked = again.get("/ohlcv/jobs/abc")
    assert blocked.status_code == 503
    assert blocked.json()["error"]["message"] == "the local store is unavailable"
    assert str(blocker) not in blocked.text
    assert "Traceback" not in blocked.text


def test_concurrent_post_and_get_have_no_bare_500(tmp_path: Path) -> None:
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    client = TestClient(
        create_app(ohlcv=OhlcvRuntime(store, _quiet_source(), lambda: _NOW)),
        raise_server_exceptions=False,
    )
    try:
        seeded = client.post("/ohlcv/jobs", json={"mints": [MINT], "timeframes": ["1d"], "days": 1})
        assert seeded.status_code == 200, seeded.text
        job_id = seeded.json()["job_id"]
        bodies: list[tuple[int, str]] = []
        lock = threading.Lock()

        def worker() -> None:
            found: list[tuple[int, str]] = []
            for _ in range(3):
                posted = client.post(
                    "/ohlcv/jobs", json={"mints": [MINT], "timeframes": ["1d"], "days": 1}
                )
                got = client.get(f"/ohlcv/jobs/{job_id}")
                found.append((posted.status_code, posted.text))
                found.append((got.status_code, got.text))
            with lock:
                bodies.extend(found)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert all(not thread.is_alive() for thread in threads)
    finally:
        store.close()
    assert bodies
    for status, text in bodies:
        assert status == 200, text
        assert "Internal Server Error" not in text
        assert "ingest_failed" not in text
        assert "Traceback" not in text


def test_post_does_not_block_the_event_loop(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    client = TestClient(
        create_app(ohlcv=OhlcvRuntime(store, _blocking_source(entered, release), lambda: _NOW)),
        raise_server_exceptions=False,
    )

    def post() -> None:
        client.post("/ohlcv/jobs", json={"mints": [MINT], "timeframes": ["1d"], "days": 1})

    try:
        worker = threading.Thread(target=post)
        worker.start()
        assert entered.wait(2)
        health: dict[str, object] = {}

        def get_health() -> None:
            started = time.perf_counter()
            health["response"] = client.get("/health")
            health["elapsed"] = time.perf_counter() - started

        checker = threading.Thread(target=get_health)
        checker.start()
        checker.join(0.3)
        blocked = checker.is_alive()
        release.set()
        checker.join(2)
        worker.join(2)
    finally:
        release.set()
        store.close()
    assert not blocked
    response = health["response"]
    assert isinstance(response, httpx.Response)
    assert response.status_code == 200
    assert float(health["elapsed"]) < 0.3


def test_request_caps_and_timeframe_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    client = TestClient(create_app(ohlcv=OhlcvRuntime(store, _quiet_source(), lambda: _NOW)))
    try:
        too_many = client.post(
            "/ohlcv/jobs",
            json={"mints": [MINT] * 9, "timeframes": ["1d"], "days": 1},
        )
        assert too_many.status_code == 422
        assert too_many.json()["error"]["code"] == "too_many_mints"
        assert too_many.json()["error"]["path"] == "$.mints"
        assert MINT not in too_many.text

        huge = b'{"mints":["' + b"A" * 20000 + b'"]}'
        oversized = client.post(
            "/ohlcv/jobs", content=huge, headers={"content-type": "application/json"}
        )
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "payload_too_large"
        assert b"AAAA" not in oversized.content

        bad_frame = client.post(
            "/ohlcv/jobs", json={"mints": [MINT], "timeframes": ["x180"], "days": 1}
        )
        assert bad_frame.status_code == 422
        assert bad_frame.json()["error"]["code"] == "invalid_request"
        assert bad_frame.json()["error"]["message"] == "unsupported timeframe"
        assert bad_frame.json()["error"]["path"] is None
        assert "history_limit" not in bad_frame.text
        assert "--days" not in bad_frame.text

        newline = client.post(
            "/ohlcv/jobs", json={"mints": [MINT], "timeframes": ["1\n180"], "days": 1}
        )
        assert newline.status_code == 422
        assert newline.json()["error"]["message"] == "unsupported timeframe"

        monkeypatch.setenv("OHLCV_MAX_BODY_BYTES", "200000")
        long_name = "T" * 4000
        reflected = client.post(
            "/ohlcv/jobs", json={"mints": [MINT], "timeframes": [long_name], "days": 1}
        )
        assert reflected.status_code == 422
        assert reflected.json()["error"]["message"] == "unsupported timeframe"
        assert long_name not in reflected.text
        assert len(reflected.text) < 400
    finally:
        store.close()


def test_api_reads_call_and_budget_env_and_caps_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ohlcv.routes as routes

    database = tmp_path / "ohlcv.sqlite"
    monkeypatch.setattr(routes, "_DEFAULT_DB", database)
    monkeypatch.setenv("OHLCV_GT_MAX_CALLS", "7")
    monkeypatch.setenv("BIRDEYE_CU_BUDGET", "40")
    app = create_app()

    class _Request:
        def __init__(self) -> None:
            self.app = app

    runtime = routes._runtime(_Request())
    try:
        assert runtime.source._call_limit == 7
        assert runtime.source._birdeye.ledger.budget == 40
        assert runtime.source._birdeye.ledger.path == database.parent / "birdeye-cu.json"
    finally:
        runtime.store.close()
        runtime.source.close()

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_POOLS)

    source = AnyMintSource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        api_key=None,
        min_interval=0,
        max_calls=1,
        sleep=lambda _seconds: None,
        now=lambda: _NOW,
    )
    store = SqliteBarStore(tmp_path / "cap.sqlite")
    client = TestClient(create_app(ohlcv=OhlcvRuntime(store, source, lambda: _NOW)))
    try:
        first = client.post("/ohlcv/jobs", json={"mints": [MINT], "timeframes": ["1d"], "days": 1})
        second = client.post("/ohlcv/jobs", json={"mints": [MINT], "timeframes": ["1d"], "days": 1})
    finally:
        store.close()
        source.close()
    # The pool stays in memory, so the second ingest spends its one call on
    # OHLCV. A cap that never reset would refuse that call before it was sent.
    assert first.status_code == 429
    assert calls["n"] == 2
    assert second.status_code != 500


def test_missing_pool_memory_expires_within_a_day() -> None:
    assert MEMORY_TTL_SECONDS <= 24 * 60 * 60
    clock = {"now": _NOW}
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path.endswith("/pools") and clock["now"] < _NOW + MEMORY_TTL_SECONDS:
            return httpx.Response(200, json=_PUMP)
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json=_POOLS)
        before = int(request.url.params.get("before_timestamp", "0"))
        candle = [before - 86400, 1, 1, 1, 1, 1]
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": [candle]}}})

    source = AnyMintSource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        api_key=None,
        min_interval=0,
        sleep=lambda _seconds: None,
        now=lambda: clock["now"],
    )
    start, end = plan_window(_NOW, 1, ("1d",))
    try:
        with pytest.raises(NoDexPoolYet, match="^no DEX pool yet$"):
            list(source.fetch_pages(MINT, "1d", start, end))
        seen = calls["n"]
        with pytest.raises(NoDexPoolYet):
            list(source.fetch_pages(MINT, "1d", start, end))
        assert calls["n"] == seen
        clock["now"] += MEMORY_TTL_SECONDS - 1
        with pytest.raises(NoDexPoolYet):
            list(source.fetch_pages(MINT, "1d", start, end))
        assert calls["n"] == seen
        clock["now"] += 1
        pages = list(source.fetch_pages(MINT, "1d", start, end))
    finally:
        source.close()
    assert calls["n"] > seen
    assert pages


def test_pre_migration_pump_is_not_served_by_birdeye_when_the_key_is_set() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        assert "birdeye" not in request.url.host
        assert request.url.path.endswith("/pools")
        return httpx.Response(200, json=_PUMP)

    source = _source(handler, api_key=_KEY)
    start, end = plan_window(_NOW, 1, ("1d",))
    try:
        with pytest.raises(NoDexPoolYet, match="^no DEX pool yet$") as caught:
            list(source.fetch_pages(MINT, "1d", start, end))
    finally:
        source.close()
    assert _KEY not in str(caught.value)
    assert hosts
    assert all("birdeye" not in host for host in hosts)


def test_token_endpoint_ungraduated_mint_is_no_dex_pool_yet() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200,
            json={"data": {"attributes": {"launchpad_details": {"completed": False}}}},
        )

    source = _source(handler, api_key=None)
    start, end = plan_window(_NOW, 1, ("1d",))
    try:
        with pytest.raises(NoDexPoolYet, match="^no DEX pool yet$"):
            list(source.fetch_pages(MINT, "1d", start, end))
    finally:
        source.close()
    assert any(path.endswith("/pools") for path in paths)
    assert any("/tokens/" in path and not path.endswith("/pools") for path in paths)


def test_fresh_source_keeps_pool_and_provider_for_gt_birdeye_and_mixed(tmp_path: Path) -> None:
    gt_store = SqliteBarStore(tmp_path / "gt.sqlite", now=lambda: _NOW)
    gt = _source(_gt_handler(_POOLS), api_key=None)
    try:
        first = _ingest(gt, gt_store, days=1, timeframe="1d")
        assert first["series"][0]["provider"] == "geckoterminal"
        assert first["pools"][0]["address"] == "PoolHigh"
        fresh = _source(_refuse, api_key=None)
        again = _ingest(fresh, gt_store, days=1, timeframe="1d")
    finally:
        gt.close()
        gt_store.close()
    assert again["series"][0]["provider"] == "geckoterminal"
    assert again["pools"][0]["address"] == "PoolHigh"
    assert again["series"][0]["pool"]["address"] == "PoolHigh"

    be_store = SqliteBarStore(tmp_path / "be.sqlite", now=lambda: _NOW)
    be = _source(_birdeye_only_handler(), api_key=_KEY)
    try:
        served = _ingest(be, be_store, days=1, timeframe="1d")
        assert served["series"][0]["provider"] == "birdeye"
        assert served["pools"] == []
        assert served["attribution"] == "Data by Birdeye"
        fresh_be = _source(_refuse, api_key=_KEY)
        kept = _ingest(fresh_be, be_store, days=1, timeframe="1d")
    finally:
        be.close()
        be_store.close()
    assert kept["series"][0]["provider"] == "birdeye"
    assert kept["pools"] == []
    assert kept["attribution"] == "Data by Birdeye"

    mixed_store = SqliteBarStore(tmp_path / "mixed.sqlite", now=lambda: _NOW)
    mixed = _source(_mixed_handler(), api_key=_KEY)
    try:
        _ingest(mixed, mixed_store, days=1, timeframe="1h")
        widened = _ingest(mixed, mixed_store, days=3, timeframe="1h")
        assert widened["series"][0]["provider"] == "mixed"
        assert "Birdeye" in str(widened["attribution"])
        assert "GeckoTerminal" in str(widened["attribution"])
        start, end = plan_window(_NOW, 3, ("1h",))
        closes = {
            bar.close
            for bar in mixed_store.load(
                user_id=LOCAL_USER_ID, mint=MINT, timeframe="1h", start=start, end=end
            )
            if not bar.synthetic
        }
        assert 1.1 in closes
        assert 9.5 in closes
        fresh_mixed = _source(_refuse, api_key=None)
        reread = _ingest(fresh_mixed, mixed_store, days=3, timeframe="1h")
    finally:
        mixed.close()
        mixed_store.close()
    assert reread["series"][0]["provider"] == "mixed"
    assert reread["pools"][0]["address"] == "PoolHigh"
    assert "Birdeye" in str(reread["attribution"])
    assert "GeckoTerminal" in str(reread["attribution"])


def test_cli_cache_hit_keeps_the_pool(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _source(_gt_handler(_POOLS), api_key=None)
    args = ["--mints", MINT, "--days", "1", "--timeframes", "1d", "--data-dir", str(tmp_path)]
    try:
        code = main(args, source=source, now=_NOW)
        assert code == 0
        first = json.loads(capsys.readouterr().out)
        fresh = _source(_refuse, api_key=None)
        again_code = main(args, source=fresh, now=_NOW)
    finally:
        source.close()
    assert again_code == 0
    second = json.loads(capsys.readouterr().out)
    assert first["pools"][0]["address"] == "PoolHigh"
    assert second["pools"][0]["address"] == "PoolHigh"
    assert second["series"][0]["provider"] == "geckoterminal"


def test_pace_and_cu_budget_hold_under_concurrency() -> None:
    arrivals: list[float] = []
    clock_lock = threading.Lock()
    clock = {"t": 0.0}

    def sleep(seconds: float) -> None:
        with clock_lock:
            clock["t"] += float(seconds)

    def monotonic() -> float:
        with clock_lock:
            return clock["t"]

    def handler(request: httpx.Request) -> httpx.Response:
        with clock_lock:
            arrivals.append(clock["t"])
        assert _KEY not in str(request.url)
        assert "api_key" not in str(request.url)
        return httpx.Response(200, json={"data": {"items": []}})

    paced = BirdeyeSource(
        api_key=_KEY,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_interval=1,
        sleep=sleep,
        monotonic=monotonic,
        max_calls=20,
        budget=30000,
        now=lambda: _NOW,
    )
    try:
        threads = [
            threading.Thread(
                target=lambda: list(paced.fetch_pages(MINT, "1h", _NOW - 3600, _NOW))
            )
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
    finally:
        paced.close()
    assert len(arrivals) == 4
    gaps = [b - a for a, b in zip(sorted(arrivals), sorted(arrivals)[1:])]
    assert min(gaps) >= 1

    ok: list[int] = []
    denied: list[int] = []
    barrier = threading.Barrier(6)
    budget_clock = {"t": 0.0}
    budget_lock = threading.Lock()

    def budget_sleep(seconds: float) -> None:
        with budget_lock:
            budget_clock["t"] += float(seconds)

    def budget_now() -> float:
        with budget_lock:
            return budget_clock["t"]

    limited = BirdeyeSource(
        api_key=_KEY,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"data": {"items": []}})
            )
        ),
        min_interval=1,
        sleep=budget_sleep,
        monotonic=budget_now,
        max_calls=20,
        budget=60,
        now=lambda: _NOW,
    )

    def once() -> None:
        barrier.wait(2)
        try:
            list(limited.fetch_pages(MINT, "1h", _NOW - 3600, _NOW))
        except ComputeBudgetExceeded:
            denied.append(1)
        else:
            ok.append(1)

    try:
        workers = [threading.Thread(target=once) for _ in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
    finally:
        limited.close()
    assert len(ok) + len(denied) == 6
    assert len(ok) <= 2
    assert limited.ledger.used <= 60
    assert limited.ledger.used == len(ok) * 25


def test_cu_budget_boundary_and_accumulation() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": {"items": []}})

    source = _birdeye(handler, budget=25)
    start = 1_700_000_000
    try:
        list(source.fetch_pages(MINT, "1m", start, start + 60))
        assert source.ledger.used == 25
        with pytest.raises(ComputeBudgetExceeded):
            list(source.fetch_pages(MINT, "1m", start, start + 60))
    finally:
        source.close()
    assert calls["n"] == 1

    second = _birdeye(handler, budget=100)
    try:
        list(second.fetch_pages(MINT, "1m", start, start + 60))
        list(second.fetch_pages(MINT, "1m", start + 60, start + 120))
    finally:
        second.close()
    assert second.ledger.used == 50


def test_birdeye_rejects_non_positive_pace_and_hides_the_key() -> None:
    for value in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            BirdeyeSource(api_key=_KEY, min_interval=value)

    def status(request: httpx.Request) -> httpx.Response:
        assert _KEY not in str(request.url)
        assert "api_key" not in str(request.url)
        return httpx.Response(418, text=_KEY)

    source = _birdeye(status, budget=100)
    try:
        with pytest.raises(SourceError) as caught:
            list(source.fetch_pages(MINT, "1m", 1_700_000_000, 1_700_000_060))
    finally:
        source.close()
    assert str(caught.value) == "Birdeye returned HTTP 418"
    assert _KEY not in str(caught.value)
    assert _KEY not in repr(caught.value)


def test_resaving_a_pool_does_not_extend_it_past_resolution(tmp_path: Path) -> None:
    """A repeat save of the same resolution cannot keep the pool for another day."""
    t0 = 1_700_000_000
    ttl = 24 * 60 * 60
    clock = {"now": t0}
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite", ttl_seconds=ttl, now=lambda: clock["now"])
    try:
        _save_pool_at(store, t0)
        clock["now"] = t0 + ttl - 5
        _save_pool_at(store, t0)
        stored = store._conn.execute(
            "SELECT stored_at, resolved_at FROM pool_resolutions"
        ).fetchone()
        assert stored == (t0, t0)
        assert store.load_pool(user_id=LOCAL_USER_ID, mint=MINT) is not None
        clock["now"] = t0 + ttl + 5
        assert store.load_pool(user_id=LOCAL_USER_ID, mint=MINT) is None
        count = store._conn.execute("SELECT COUNT(*) FROM pool_resolutions").fetchone()
    finally:
        store.close()
    assert count is not None
    assert count[0] == 0
    changed = SqliteBarStore(tmp_path / "changed.sqlite", ttl_seconds=ttl, now=lambda: clock["now"])
    try:
        clock["now"] = t0
        _save_pool_at(changed, t0)
        clock["now"] = t0 + 10
        _save_pool_at(changed, t0 + 10)
        moved = changed._conn.execute("SELECT stored_at FROM pool_resolutions").fetchone()
    finally:
        changed.close()
    assert moved == (t0 + 10,)


def test_unreadable_birdeye_ledger_is_503_even_when_keyless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ohlcv.routes as routes

    monkeypatch.delenv("BIRDEYE_API_KEY", raising=False)
    database = tmp_path / "ohlcv.sqlite"
    monkeypatch.setattr(routes, "_DEFAULT_DB", database)
    ledger = database.parent / "birdeye-cu.json"
    for content in ("{bad", "", "[]"):
        ledger.write_text(content, encoding="utf-8")
        client = TestClient(create_app(), raise_server_exceptions=False)
        responses = (
            client.get("/ohlcv/jobs/x"),
            client.post("/ohlcv/jobs", json={"mints": [MINT], "timeframes": ["1h"], "days": 1}),
        )
        for response in responses:
            assert response.status_code == 503, content
            assert response.json() == {
                "error": {
                    "code": "store_unavailable",
                    "message": "the local store is unavailable",
                    "path": None,
                }
            }
            assert str(ledger) not in response.text
            assert "Traceback" not in response.text
            assert "Internal Server Error" not in response.text


def test_cu_ledger_reloads_from_an_atomic_write(tmp_path: Path) -> None:
    path = tmp_path / "birdeye-cu.json"
    now = 1_700_000_000
    first = CuLedger(budget=100, path=path, now=lambda: now)
    cost = first.reserve(1)
    first.commit(cost)
    assert first.used == cost
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == {"period": first.period, "used": cost}
    second = CuLedger(budget=100, path=path, now=lambda: now)
    assert second.used == cost
    assert second.period == first.period
    assert [item.name for item in tmp_path.iterdir()] == ["birdeye-cu.json"]


def test_failed_ledger_replace_removes_the_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "birdeye-cu.json"
    ledger = CuLedger(budget=100, path=path, now=lambda: 1_700_000_000)

    def fail_replace(_src: str, _dst: str) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("app.ohlcv.adapters.birdeye.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        ledger.commit(25)
    assert list(tmp_path.iterdir()) == []


def test_short_history_and_sparse_use_strict_boundaries() -> None:
    start = 1_700_000_000
    bar = Bar(MINT, "1h", start, 1, 1, 1, 1, 1)
    later = Bar(MINT, "1h", start + 3600, 1, 1, 1, 1, 1)
    exact = annotate_series(
        {"expected": 4},
        [bar],
        pool=None,
        provider="geckoterminal",
        warmup_bars=0,
        window_start=start,
        window_end=start + 4 * 3600,
    )
    assert "short_history" not in exact["flags"]
    half = annotate_series(
        {"expected": 4},
        [bar, later],
        pool=None,
        provider="geckoterminal",
        warmup_bars=0,
        window_start=start,
        window_end=start + 4 * 3600,
    )
    assert "sparse" not in half["flags"]
    thin = annotate_series(
        {"expected": 4},
        [bar],
        pool=None,
        provider="geckoterminal",
        warmup_bars=0,
        window_start=start,
        window_end=start + 4 * 3600,
    )
    assert "sparse" in thin["flags"]


def _save_pool_at(store: SqliteBarStore, resolved_at: int) -> None:
    store.save_pool(
        user_id=LOCAL_USER_ID,
        mint=MINT,
        address="PoolHigh",
        dex="Raydium",
        dex_id="raydium",
        quote_token="So11111111111111111111111111111111111111112",
        quote_symbol="SOL",
        liquidity_usd="50.00",
        resolved_at=resolved_at,
        pool_created_at=1_600_000_000,
        provider="geckoterminal",
    )


def _quiet_source() -> AnyMintSource:
    class _Quiet:
        name = "any-mint"

        def fetch_pages(self, mint: str, timeframe: str, start: int, end: int):
            yield FetchPage(
                [Bar(mint, timeframe, start, 1.0, 1.0, 1.0, 1.0, 1.0)], [], start, end, False
            )

        def fetch_bars(self, *_args: object) -> None:
            raise AssertionError("pages")

        def pool_for(self, _mint: str) -> None:
            return None

        def served_provider(self, _mint: str, _timeframe: str) -> str:
            return "geckoterminal"

    return _Quiet()  # type: ignore[return-value]


def _blocking_source(entered: threading.Event, release: threading.Event) -> AnyMintSource:
    class _Slow:
        name = "any-mint"

        def fetch_pages(self, mint: str, timeframe: str, start: int, end: int):
            entered.set()
            release.wait(timeout=2)
            yield FetchPage(
                [Bar(mint, timeframe, start, 1.0, 1.0, 1.0, 1.0, 1.0)], [], start, end, False
            )

        def fetch_bars(self, *_args: object) -> None:
            raise AssertionError("pages")

        def pool_for(self, _mint: str) -> None:
            return None

        def served_provider(self, _mint: str, _timeframe: str) -> str:
            return "geckoterminal"

    return _Slow()  # type: ignore[return-value]


def _source(handler, *, api_key: str | None) -> AnyMintSource:
    transport = httpx.MockTransport(handler)
    sleep, monotonic = _instant_pace()
    birdeye = None
    if api_key is not None:
        birdeye = BirdeyeSource(
            api_key=api_key,
            client=httpx.Client(transport=transport),
            budget=30_000,
            min_interval=1,
            sleep=sleep,
            monotonic=monotonic,
            now=lambda: _NOW,
        )
    return AnyMintSource(
        httpx.Client(transport=transport),
        birdeye=birdeye,
        api_key=None if birdeye is None else api_key,
        min_interval=0,
        max_calls=40,
        sleep=lambda _seconds: None,
        now=lambda: _NOW,
    )


def _birdeye(handler, *, budget: int) -> BirdeyeSource:
    sleep, monotonic = _instant_pace()
    return BirdeyeSource(
        api_key=_KEY,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=budget,
        min_interval=1,
        sleep=sleep,
        monotonic=monotonic,
        now=lambda: _NOW,
    )


def _instant_pace():
    clock = {"t": 0.0}

    def sleep(seconds: float) -> None:
        clock["t"] += float(seconds)

    def monotonic() -> float:
        return clock["t"]

    return sleep, monotonic


def _ingest(source: AnyMintSource, store: SqliteBarStore, *, days: int, timeframe: str):
    start, end = plan_window(_NOW, days, (timeframe,))
    return run_mint_ingest(
        source=source,
        store=store,
        assets=assets_from_mints([MINT]),
        start=start,
        end=end,
        timeframes=(timeframe,),
        user_id=LOCAL_USER_ID,
        generated_at=_NOW,
    )


def _gt_handler(pools: dict):
    start, _end = plan_window(_NOW, 1, ("1d",))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json=pools)
        return httpx.Response(
            200, json={"data": {"attributes": {"ohlcv_list": [[start, 2, 2, 2, 2, 3]]}}}
        )

    return handler


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"cache hit called {request.url.path}")


def _birdeye_only_handler():
    start, _end = plan_window(_NOW, 1, ("1d",))

    def handler(request: httpx.Request) -> httpx.Response:
        if "birdeye" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {"unix_time": start, "o": 1, "h": 1, "l": 1, "c": 4, "v": 1, "v_usd": 2}
                        ]
                    }
                },
            )
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"data": []})

    return handler


def _mixed_handler():
    recent, _end = plan_window(_NOW, 1, ("1h",))

    def handler(request: httpx.Request) -> httpx.Response:
        if "birdeye" in request.url.host:
            start = int(request.url.params["time_from"])
            stop = int(request.url.params["time_to"])
            step = 3600
            stamp = start if start % step == 0 else start + (step - start % step)
            items = []
            while stamp <= stop and len(items) < 200:
                items.append(
                    {"unix_time": stamp, "o": 9, "h": 10, "l": 8, "c": 9.5, "v": 3, "v_usd": 4}
                )
                stamp += step
            return httpx.Response(200, json={"data": {"items": items}})
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json=_POOLS)
        before = int(request.url.params["before_timestamp"])
        if before <= recent:
            candles: list[list[float]] = []
        else:
            limit = int(request.url.params.get("limit", "100"))
            stamp = before - (before % 3600)
            candles = []
            while stamp >= recent and len(candles) < limit:
                candles.append([stamp, 1, 1.2, 0.9, 1.1, 2])
                stamp -= 3600
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": candles}}})

    return handler
