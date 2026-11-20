"""Any-mint OHLCV. Fixtures are synthetic. No live socket."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import LOCAL_USER_ID
from app.main import create_app
from app.ohlcv.adapters import open_source
from app.ohlcv.adapters.any_mint import AnyMintSource
from app.ohlcv.backfill import main, plan_window
from app.ohlcv.bars import Bar
from app.ohlcv.errors import NoDexPoolYet, NoPool
from app.ohlcv.ingest import run_mint_ingest
from app.ohlcv.mint import require_mint
from app.ohlcv.pools import choose_pool
from app.ohlcv.registry import Asset
from app.ohlcv.routes import OhlcvRuntime
from app.ohlcv.source import FetchPage
from app.ohlcv.store import SqliteBarStore

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
FIXTURES = Path(__file__).parent / "fixtures" / "ohlcv"
POOLS = json.loads((FIXTURES / "geckoterminal_pools_synthetic.json").read_text(encoding="utf-8"))
PUMP = json.loads((FIXTURES / "geckoterminal_pump_synthetic.json").read_text(encoding="utf-8"))
_FORBIDDEN = (
    "authorization",
    "api-key",
    "api_key",
    "x-api-key",
    "x-cg-",
    "bearer",
    "set-cookie",
    "secret",
    "birdeye_api_key",
)
_KEY = "test-birdeye-key"


def test_ohlcv_fixtures_are_synthetic_and_scrubbed() -> None:
    for path in FIXTURES.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
        assert payload["synthetic"] is True
        lowered = text.lower()
        for word in _FORBIDDEN:
            assert word not in lowered
        assert _KEY not in text


def test_open_source_any_mint_stays_off_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIRDEYE_API_KEY", raising=False)
    source = open_source("any-mint")
    try:
        assert source.name == "any-mint"
        assert repr(source) == "AnyMintSource(birdeye=False)"
    finally:
        source.close()


def test_tie_break_is_liquidity_then_older_pool_then_address() -> None:
    """Highest reserve wins. Equal reserves: earlier pool_created_at, then address."""
    chosen = choose_pool(POOLS, MINT, resolved_at=1_700_000_000)
    assert chosen.address == "PoolHigh"
    assert chosen.dex == "Raydium"
    assert chosen.dex_id == "raydium"
    assert chosen.quote_token == "So11111111111111111111111111111111111111112"
    assert chosen.quote_symbol == "SOL"
    assert chosen.liquidity_usd == "50.00"

    tied = json.loads(json.dumps(POOLS))
    tied["data"][0]["attributes"]["reserve_in_usd"] = "50.00"
    tied["data"][0]["attributes"]["pool_created_at"] = "2019-01-01T00:00:00Z"
    tied["data"][0]["attributes"]["address"] = "PoolOlder"
    older = choose_pool(tied, MINT, resolved_at=1_700_000_000)
    assert older.address == "PoolOlder"

    same_age = json.loads(json.dumps(tied))
    same_age["data"][0]["attributes"]["pool_created_at"] = "2021-06-01T00:00:00Z"
    same_age["data"][0]["attributes"]["address"] = "AAA"
    same_age["data"][1]["attributes"]["address"] = "BBB"
    assert choose_pool(same_age, MINT, resolved_at=1_700_000_000).address == "AAA"

    larger_first = json.loads(json.dumps(same_age))
    larger_first["data"][0]["attributes"]["address"] = "ZZZ"
    larger_first["data"][1]["attributes"]["address"] = "AAA"
    assert choose_pool(larger_first, MINT, resolved_at=1_700_000_000).address == "AAA"

    missing_time = json.loads(json.dumps(POOLS))
    missing_time["data"][0]["attributes"]["reserve_in_usd"] = "50.00"
    missing_time["data"][0]["attributes"]["address"] = "AAA"
    del missing_time["data"][0]["attributes"]["pool_created_at"]
    missing_time["data"][1]["attributes"]["address"] = "ZZZ"
    assert choose_pool(missing_time, MINT, resolved_at=1_700_000_000).address == "ZZZ"

    bonding = json.loads(json.dumps(POOLS))
    bonding["data"][1]["relationships"]["dex"]["data"]["id"] = "pump-fun"
    bonding["data"][1]["attributes"]["launchpad_details"] = {"completed": False}
    # The remaining eligible pool is the lower-liquidity Orca pool, not the curve.
    assert choose_pool(bonding, MINT, resolved_at=1_700_000_000).address == "PoolLow"
    with pytest.raises(NoDexPoolYet, match="no DEX pool yet"):
        choose_pool(PUMP, MINT, resolved_at=1_700_000_000)
    with pytest.raises(NoPool, match="no pool"):
        choose_pool({"data": []}, MINT, resolved_at=1_700_000_000)


def test_invalid_mint_is_rejected_before_any_request(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("invalid mint must not call the network")

    source = _source(handler)
    for bad in ("not-a-mint", "", "0OIl", "111", "1" * 50):
        with pytest.raises(Exception, match="invalid mint"):
            require_mint(bad)
        with pytest.raises(Exception, match="invalid mint"):
            list(source.fetch_pages(bad, "1m", 0, 60))
    assert calls == []
    code = main(
        ["--mints", "not-a-mint", "--days", "1", "--timeframes", "1m", "--data-dir", str(tmp_path)],
        source=source,
        now=1_700_086_400,
    )
    assert code == 1
    client, store = _http(tmp_path / "api", source)
    try:
        response = client.post("/ohlcv/jobs", json={"mints": ["not-a-mint"], "days": 1})
    finally:
        store.close()
        source.close()
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_mint"
    assert response.json()["error"]["message"] == "invalid mint"
    assert calls == []


def test_no_pool_is_a_404_and_a_nonzero_cli_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "ohlcv" not in request.url.path
        assert "secret-token-should-not-leak" not in request.url.path
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json={"data": [], "secret": "secret-token-should-not-leak"})
        return httpx.Response(404, json={"message": "secret-token-should-not-leak"})

    source = _source(handler)
    client, store = _http(tmp_path / "api", source)
    try:
        response = client.post(
            "/ohlcv/jobs",
            json={"mints": [MINT], "timeframes": ["1d"], "days": 1},
        )
        assert response.status_code == 404
        body = response.json()
        assert body["error"]["code"] == "no_pool"
        assert body["error"]["message"] == f"no pool for mint {MINT}"
        assert "secret-token-should-not-leak" not in response.text
        code = main(
            ["--mints", MINT, "--days", "1", "--timeframes", "1d", "--data-dir", str(tmp_path)],
            source=source,
            now=1_700_086_400,
        )
    finally:
        store.close()
        source.close()
    assert code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert err.strip().splitlines()[-1] == f"no pool for mint {MINT}"


def test_pumpfun_pre_migration_is_no_dex_pool_yet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/pools")
        assert "include_empty_intervals" not in request.url.params
        return httpx.Response(200, json=PUMP)

    source = _source(handler)
    client, store = _http(tmp_path / "api", source)
    try:
        response = client.post(
            "/ohlcv/jobs",
            json={"mints": [MINT], "timeframes": ["1d"], "days": 1},
        )
        assert response.status_code == 422
        assert response.json()["error"] == {
            "code": "no_dex_pool_yet",
            "message": "no DEX pool yet",
            "path": "$.mints",
        }
        code = main(
            ["--mints", MINT, "--days", "1", "--timeframes", "1d", "--data-dir", str(tmp_path)],
            source=source,
            now=1_700_086_400,
        )
    finally:
        store.close()
        source.close()
    assert code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert err.strip().splitlines()[-1] == "no DEX pool yet"


def test_history_beyond_180_days_is_a_clear_error_and_is_not_clamped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise AssertionError("180-day cap must fail before a request")

    source = _source(handler)
    now = 1_700_086_400
    code = main(
        ["--mints", MINT, "--days", "181", "--timeframes", "1d", "--data-dir", str(tmp_path)],
        source=source,
        now=now,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "180" in err
    assert "clamp" not in err.lower()
    assert calls == []
    with pytest.raises(ValueError, match="180"):
        plan_window(now, 181, ("1d",))
    client, store = _http(tmp_path / "api", source, now=now)
    try:
        response = client.post(
            "/ohlcv/jobs",
            json={"mints": [MINT], "timeframes": ["1d"], "days": 181},
        )
    finally:
        store.close()
        source.close()
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "history_limit"
    assert response.json()["error"]["path"] == "$.days"
    assert "180" in response.json()["error"]["message"]
    assert "--days" not in response.json()["error"]["message"]
    assert calls == []


def test_rate_limit_exhaustion_and_the_call_cap_are_clear_errors(tmp_path: Path) -> None:
    sleeps: list[float] = []

    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="secret-token-should-not-leak")

    source = _source(limited, sleep=sleeps.append, max_calls=20)
    client, store = _http(tmp_path / "rate", source)
    try:
        response = client.post(
            "/ohlcv/jobs",
            json={"mints": [MINT], "timeframes": ["1d"], "days": 1},
        )
    finally:
        store.close()
        source.close()
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit"
    assert response.json()["error"]["message"] == "GeckoTerminal rate limit exhausted"
    assert "secret-token-should-not-leak" not in response.text
    assert sleeps

    ohlcv_calls = {"n": 0}

    def capped(request: httpx.Request) -> httpx.Response:
        if "ohlcv" in request.url.path:
            ohlcv_calls["n"] += 1
        return httpx.Response(200, json=POOLS)

    capped_source = _source(capped, max_calls=1)
    capped_client, capped_store = _http(tmp_path / "cap", capped_source)
    try:
        capped_response = capped_client.post(
            "/ohlcv/jobs",
            json={"mints": [MINT], "timeframes": ["1d"], "days": 1},
        )
    finally:
        capped_store.close()
        capped_source.close()
    assert capped_response.status_code == 429
    assert capped_response.json()["error"]["message"] == "GeckoTerminal request cap exhausted"
    assert ohlcv_calls["n"] == 0


def test_short_history_is_a_lint_and_is_not_backfilled(tmp_path: Path) -> None:
    now = 1_700_086_400
    start, end = plan_window(now, 1, ("1m",))
    first = end - 60

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json=POOLS)
        assert "include_empty_intervals" not in request.url.params
        candle = [first, 2, 2, 2, 2, 5]
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": [candle]}}})

    source = _source(handler, now=now)
    client, store = _http(tmp_path, source, now=now)
    try:
        response = client.post(
            "/ohlcv/jobs",
            json={"mints": [MINT], "timeframes": ["1m"], "days": 1},
        )
        assert response.status_code == 200
        series = response.json()["series"][0]
        assert "short_history" in series["flags"]
        assert "nothing was back-filled" in series["lints"][0]
        stored = store.load(
            user_id=LOCAL_USER_ID, mint=MINT, timeframe="1m", start=start, end=end
        )
        assert stored
        assert min(bar.open_time for bar in stored) >= first
        assert all(bar.open_time >= first for bar in stored)
        assert series["gaps"]
    finally:
        store.close()
        source.close()


def test_sparse_zero_volume_and_young_pool_are_flagged(tmp_path: Path) -> None:
    now = 1_700_086_400
    start, _end = plan_window(now, 1, ("1m",))
    pools = json.loads(json.dumps(POOLS))
    created = start + 60
    pools["data"][1]["attributes"]["pool_created_at"] = _iso(created)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json=pools)
        candle = [start, 2, 2, 2, 2, 0]
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": [candle]}}})

    source = _source(handler, now=now)
    client, store = _http(tmp_path, source, now=now)
    try:
        response = client.post(
            "/ohlcv/jobs",
            json={"mints": [MINT], "timeframes": ["1m"], "days": 1, "warmup_bars": 50},
        )
        assert response.status_code == 200
        series = response.json()["series"][0]
        assert "sparse" in series["flags"]
        assert "zero_volume" in series["flags"]
        assert series["zero_volume_bars"] >= 1
        assert "young_pool" in series["flags"]
        assert "short_history" in series["flags"]
        assert any("indicator warmup" in lint for lint in series["lints"])
        assert series["pool_age_seconds"] is not None
    finally:
        store.close()
        source.close()


def test_a_mid_series_gap_is_reported_and_not_interpolated(tmp_path: Path) -> None:
    start = _aligned(60)
    end = start + 300
    mid = start + 120
    mid2 = start + 240
    left = Bar(MINT, "1m", start, 1, 1, 1, 1, 5, False)
    right = Bar(MINT, "1m", mid2, 3, 3, 3, 3, 7, False)
    source = _GapSource(left, right, start, mid, mid2, end)
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    try:
        report = run_mint_ingest(
            source=source,
            store=store,
            assets=(Asset(MINT, MINT),),
            start=start,
            end=end,
            timeframes=("1m",),
            user_id=LOCAL_USER_ID,
            generated_at=end,
        )
        stored = store.load(user_id=LOCAL_USER_ID, mint=MINT, timeframe="1m", start=start, end=end)
    finally:
        store.close()
    by_time = {bar.open_time: bar for bar in stored}
    assert mid not in by_time
    assert mid + 60 not in by_time
    assert by_time[start].close == 1
    assert by_time[mid2].close == 3
    assert all(bar.close != 2 for bar in stored)
    series = report["series"][0]
    assert series["gaps"]
    assert report["forward_filled"] is False
    assert "PoolHigh" == series["pool"]["address"]


def test_chosen_pool_is_in_the_ingest_result_cli_and_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = 1_700_086_400
    start, _end = plan_window(now, 1, ("1d",))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pools"):
            assert request.url.params["include"] == "base_token,quote_token,dex"
            return httpx.Response(200, json=POOLS)
        assert request.url.path.endswith("/pools/PoolHigh/ohlcv/day")
        assert "include_empty_intervals" not in request.url.params
        candle = [start, 1.5, 1.6, 1.4, 1.55, 9]
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": [candle]}}})

    source = _source(handler, now=now)
    client, store = _http(tmp_path / "api", source, now=now)
    try:
        response = client.post(
            "/ohlcv/jobs",
            json={
                "mints": [MINT],
                "timeframes": ["1d"],
                "days": 1,
                "user_id": "someone-else",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == LOCAL_USER_ID
        _assert_pool(body)
        job_id = body["job_id"]
        again = client.get(f"/ohlcv/jobs/{job_id}")
        assert again.status_code == 200
        assert again.json()["pools"] == body["pools"]
        assert again.json()["series"][0]["provider"] == "geckoterminal"
        code = main(
            ["--mints", MINT, "--days", "1", "--timeframes", "1d", "--data-dir", str(tmp_path)],
            source=source,
            now=now,
        )
    finally:
        store.close()
        source.close()
    assert code == 0
    cli = json.loads(capsys.readouterr().out)
    _assert_pool(cli)
    assert cli["forward_filled"] is False
    assert cli["series"][0]["provider"] == "geckoterminal"


def test_provider_rows_and_birdeye_results_are_purged_after_24h(tmp_path: Path) -> None:
    clock = {"now": 1_700_086_400}
    start, _end = plan_window(clock["now"], 1, ("1d",))

    def gt(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json=POOLS)
        candle = [start, 1, 1, 1, 1, 4]
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": [candle]}}})

    source = _source(gt, now=lambda: clock["now"])
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite", ttl_seconds=60, now=lambda: clock["now"])
    app = TestClient(create_app(ohlcv=OhlcvRuntime(store, source, lambda: clock["now"])))
    try:
        response = app.post("/ohlcv/jobs", json={"mints": [MINT], "timeframes": ["1d"], "days": 1})
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        assert store.load_pool(user_id=LOCAL_USER_ID, mint=MINT) is not None
        assert store.load(
            user_id=LOCAL_USER_ID, mint=MINT, timeframe="1d", start=start, end=start + 86400
        )
        clock["now"] += 60
        expired = app.get(f"/ohlcv/jobs/{job_id}")
        assert expired.status_code == 410
        assert expired.json()["error"]["code"] == "expired"
        assert store.load_pool(user_id=LOCAL_USER_ID, mint=MINT) is None
        assert (
            store.load(
                user_id=LOCAL_USER_ID, mint=MINT, timeframe="1d", start=start, end=start + 86400
            )
            == []
        )
    finally:
        store.close()
        source.close()


def _assert_pool(body: dict) -> None:
    pool = body["pools"][0]
    assert pool["mint"] == MINT
    assert pool["address"] == "PoolHigh"
    assert pool["dex"] == "Raydium"
    assert pool["quote_token"] == "So11111111111111111111111111111111111111112"
    assert pool["liquidity_usd"] == "50.00"
    assert pool["resolved_at"].endswith("Z")
    series_pool = body["series"][0]["pool"]
    assert series_pool["address"] == "PoolHigh"
    assert series_pool["dex"] == "Raydium"
    assert series_pool["quote_token"] == pool["quote_token"]
    assert series_pool["liquidity_usd"] == "50.00"
    assert series_pool["resolved_at"] == pool["resolved_at"]


class _GapSource:
    name = "any-mint"

    def __init__(
        self,
        left: Bar,
        right: Bar,
        start: int,
        mid: int,
        mid2: int,
        end: int,
    ) -> None:
        self._left = left
        self._right = right
        self._start = start
        self._mid = mid
        self._mid2 = mid2
        self._end = end

    def pool_for(self, _mint: str):
        return choose_pool(POOLS, MINT, resolved_at=self._end)

    def served_provider(self, _mint: str, _timeframe: str) -> str:
        return "geckoterminal"

    def fetch_pages(self, mint: str, timeframe: str, start: int, end: int):
        del mint, timeframe, start, end
        yield FetchPage([self._left], [], self._start, self._mid, False)
        yield FetchPage([], [], self._mid, self._mid2, True)
        yield FetchPage([self._right], [], self._mid2, self._end, False)

    def fetch_bars(self, mint: str, timeframe: str, start: int, end: int):
        raise AssertionError("pages are the checkpoint")


def _source(
    handler,
    *,
    now: int | object = 1_700_086_400,
    max_calls: int = 20,
    sleep=None,
    api_key: str | None = None,
    budget: int = 30_000,
) -> AnyMintSource:
    clock = now if callable(now) else (lambda: now)
    transport = httpx.MockTransport(handler)
    birdeye = None
    if api_key is not None:
        birdeye = _birdeye(api_key, transport, budget, sleep, clock)
    return AnyMintSource(
        httpx.Client(transport=transport),
        birdeye=birdeye,
        api_key=None if birdeye is None else api_key,
        min_interval=0,
        max_calls=max_calls,
        sleep=sleep or (lambda _seconds: None),
        now=clock,
    )


def _instant_pace():
    clock = {"t": 0.0}

    def sleep(seconds: float) -> None:
        clock["t"] += float(seconds)

    def monotonic() -> float:
        return clock["t"]

    return sleep, monotonic


def _birdeye(api_key: str, transport: httpx.MockTransport, budget: int, sleep, clock):
    from app.ohlcv.adapters.birdeye import BirdeyeSource

    pace_sleep, monotonic = _instant_pace()

    def _sleep(seconds: float) -> None:
        if sleep is not None:
            sleep(seconds)
        pace_sleep(seconds)

    return BirdeyeSource(
        api_key=api_key,
        client=httpx.Client(transport=transport),
        budget=budget,
        min_interval=1,
        sleep=_sleep,
        monotonic=monotonic,
        now=clock,
    )


def _http(
    tmp_path: Path, source: AnyMintSource, *, now: int | object = 1_700_086_400
) -> tuple[TestClient, SqliteBarStore]:
    clock = now if callable(now) else (lambda: now)
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite", now=clock)
    runtime = OhlcvRuntime(store, source, clock)
    return TestClient(create_app(ohlcv=runtime)), store


def _aligned(step: int) -> int:
    base = 1_700_000_000
    return base - (base % step)


def _iso(timestamp: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
