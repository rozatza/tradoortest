"""Birdeye fallback. The key is a test placeholder and never leaves the process."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.auth import LOCAL_USER_ID
from app.ohlcv.adapters.any_mint import AnyMintSource
from app.ohlcv.adapters.birdeye import (
    DEFAULT_CU_BUDGET,
    DEFAULT_MIN_INTERVAL,
    BirdeyeSource,
    cu_for_candles,
)
from app.ohlcv.errors import ComputeBudgetExceeded, RateLimitExhausted
from app.ohlcv.registry import Asset
from app.ohlcv.store import SqliteBarStore

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
_KEY = "test-birdeye-key"
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "ohlcv" / "birdeye_ohlcv_synthetic.json").read_text(
        encoding="utf-8"
    )
)
POOLS = json.loads(
    (Path(__file__).parent / "fixtures" / "ohlcv" / "geckoterminal_pools_synthetic.json").read_text(
        encoding="utf-8"
    )
)


def test_birdeye_is_a_clean_noop_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIRDEYE_API_KEY", raising=False)
    source = BirdeyeSource(api_key=None)
    assert source.enabled is False
    assert "test-birdeye-key" not in repr(source)
    assert list(source.fetch_pages(MINT, "1m", 0, 60)) == []
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"message": "missing"})

    wrapped = AnyMintSource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        api_key=None,
        min_interval=0,
        sleep=lambda _seconds: None,
    )
    try:
        with pytest.raises(Exception, match="no pool") as caught:
            list(wrapped.fetch_pages(MINT, "1m", 1_699_999_980, 1_700_000_040))
    finally:
        wrapped.close()
    assert "BIRDEYE" not in str(caught.value)
    assert "key" not in str(caught.value).lower()
    assert all("birdeye" not in path for path in calls)


def test_birdeye_serves_when_geckoterminal_has_no_data(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if "geckoterminal" in request.url.host or request.url.path.endswith("/pools"):
            return httpx.Response(200, json={"data": []})
        if "/tokens/" in request.url.path and not request.url.path.endswith("/pools"):
            return httpx.Response(404, json={"status": "missing"})
        assert request.url.path == "/defi/v3/ohlcv"
        assert request.url.params["padding"] == "false"
        assert request.url.params["outlier"] == "false"
        assert "X-API-KEY" not in str(request.url)
        assert request.headers["X-API-KEY"] == _KEY
        return httpx.Response(200, json=FIXTURE)

    source = _wrapped(handler)
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    start = 1_699_999_980
    end = 1_700_000_220
    try:
        from app.ohlcv.ingest import run_mint_ingest

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
        source.close()
    real = [bar for bar in stored if not bar.synthetic]
    assert [bar.open_time for bar in real] == [1_699_999_980, 1_700_000_100]
    assert 1_700_000_040 not in {bar.open_time for bar in real}
    assert real[0].volume == 4.0
    assert real[1].close == 1.3
    series = report["series"][0]
    assert series["provider"] == "birdeye"
    assert series["pool"] is None
    assert report["attribution"] == "Data by Birdeye"
    dumped = json.dumps(report)
    assert _KEY not in dumped
    assert "public-api.birdeye.so" in seen


def test_an_empty_geckoterminal_page_falls_back_and_keeps_the_pool(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "birdeye" in request.url.host:
            assert request.url.params["padding"] == "false"
            stamp = int(request.url.params["time_from"])
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "items": [
                            {
                                "o": 2,
                                "h": 2,
                                "l": 2,
                                "c": 2,
                                "v": 1,
                                "v_usd": 8,
                                "unix_time": stamp,
                                "address": "synthetic",
                                "type": "1d",
                                "currency": "usd",
                            }
                        ]
                    },
                },
            )
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json=POOLS)
        if "ohlcv" in request.url.path:
            return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": []}}})
        raise AssertionError(request.url.path)

    source = _wrapped(handler)
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    start = 1_699_920_000
    end = start + 86_400
    try:
        from app.ohlcv.ingest import run_mint_ingest

        report = run_mint_ingest(
            source=source,
            store=store,
            assets=(Asset(MINT, MINT),),
            start=start,
            end=end,
            timeframes=("1d",),
            user_id=LOCAL_USER_ID,
            generated_at=end,
        )
    finally:
        store.close()
        source.close()
    series = report["series"][0]
    assert series["provider"] == "birdeye"
    assert series["pool"]["address"] == "PoolHigh"
    assert report["pools"][0]["address"] == "PoolHigh"
    assert _KEY not in json.dumps(report)


def test_birdeye_does_not_interpolate_a_hole() -> None:
    source = BirdeyeSource(
        api_key=_KEY,
        client=_client(_fixture_only),
        min_interval=0,
        sleep=lambda _seconds: None,
    )
    try:
        result = source.fetch_bars(MINT, "1m", 1_699_999_980, 1_700_000_220)
    finally:
        source.close()
    assert [bar.open_time for bar in result.bars] == [1_699_999_980, 1_700_000_100]
    assert all(bar.open_time != 1_700_000_040 for bar in result.bars)


def test_birdeye_key_is_absent_from_errors_repr_and_output() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=_KEY)

    source = BirdeyeSource(
        api_key=_KEY,
        client=_client(handler),
        min_interval=0,
        sleep=lambda _seconds: None,
        max_calls=10,
    )
    try:
        assert repr(source) == "BirdeyeSource(enabled=True)"
        assert _KEY not in repr(source)
        with pytest.raises(Exception) as caught:
            source.fetch_bars(MINT, "1m", 1_699_999_980, 1_700_000_040)
    finally:
        source.close()
    assert _KEY not in str(caught.value)
    assert _KEY not in repr(caught.value)
    assert "HTTP 500" in str(caught.value)


def test_birdeye_cu_budget_fails_before_the_request() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise AssertionError("budget must fail before the request")

    source = BirdeyeSource(
        api_key=_KEY,
        client=_client(handler),
        budget=20,
        min_interval=0,
        sleep=lambda _seconds: None,
    )
    try:
        with pytest.raises(ComputeBudgetExceeded, match="compute-unit budget exceeded") as caught:
            source.fetch_bars(MINT, "1m", 1_699_999_980, 1_700_000_040)
    finally:
        source.close()
    assert calls["n"] == 0
    assert _KEY not in str(caught.value)
    assert cu_for_candles(100) == 25
    assert cu_for_candles(101) == 30
    assert cu_for_candles(1000) == 40
    assert cu_for_candles(1001) == 75
    assert cu_for_candles(5000) == 100
    assert DEFAULT_CU_BUDGET == 30_000
    assert DEFAULT_MIN_INTERVAL == 1.0


def test_birdeye_paces_at_one_request_per_second() -> None:
    clock = {"now": 0.0}
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"items": []}})

    source = BirdeyeSource(
        api_key=_KEY,
        client=_client(handler),
        min_interval=1,
        sleep=sleep,
        monotonic=lambda: clock["now"],
        budget=300,
    )
    try:
        # Two chunks: the span is longer than 5000 one-minute candles.
        pages = list(source.fetch_pages(MINT, "1m", 0, 5001 * 60))
    finally:
        source.close()
    assert len(pages) == 2
    assert sleeps == [1.0]
    assert source.min_interval >= 1


def test_birdeye_rate_limit_exhaustion_hides_the_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text=_KEY)

    source = BirdeyeSource(
        api_key=_KEY,
        client=_client(handler),
        min_interval=0,
        sleep=lambda _seconds: None,
    )
    try:
        with pytest.raises(RateLimitExhausted, match="Birdeye rate limit exhausted") as caught:
            source.fetch_bars(MINT, "1m", 1_699_999_980, 1_700_000_040)
    finally:
        source.close()
    assert _KEY not in str(caught.value)


def test_birdeye_bars_are_purged_with_the_job_result(tmp_path: Path) -> None:
    clock = {"now": 1_800_000_000}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pools") or (
            "/tokens/" in request.url.path and "ohlcv" not in request.url.path
        ):
            if request.url.path.endswith("/pools"):
                return httpx.Response(200, json={"data": []})
            return httpx.Response(404, json={"status": "missing"})
        stamp = int(request.url.params["time_from"])
        return httpx.Response(
            200,
            json={
                "synthetic": True,
                "success": True,
                "data": {
                    "items": [
                        {
                            "o": 1,
                            "h": 1,
                            "l": 1,
                            "c": 1,
                            "v": 1,
                            "v_usd": 2,
                            "unix_time": stamp,
                            "address": "synthetic",
                            "type": "1d",
                            "currency": "usd",
                        }
                    ]
                },
            },
        )

    source = _wrapped(handler, now=lambda: clock["now"])
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite", ttl_seconds=60, now=lambda: clock["now"])
    start = clock["now"] - (clock["now"] % 86400) - 86400
    end = start + 86400
    try:
        from app.ohlcv.ingest import run_mint_ingest

        report = run_mint_ingest(
            source=source,
            store=store,
            assets=(Asset(MINT, MINT),),
            start=start,
            end=end,
            timeframes=("1d",),
            user_id=LOCAL_USER_ID,
            generated_at=clock["now"],
        )
        assert report["series"][0]["provider"] == "birdeye"
        assert store.load(user_id=LOCAL_USER_ID, mint=MINT, timeframe="1d", start=start, end=end)
        job_id = str(report["job_id"])
        clock["now"] += 60
        with pytest.raises(Exception, match="expired"):
            store.read_ingest_result(user_id=LOCAL_USER_ID, job_id=job_id)
        assert (
            store.load(user_id=LOCAL_USER_ID, mint=MINT, timeframe="1d", start=start, end=end) == []
        )
    finally:
        store.close()
        source.close()


def _fixture_only(request: httpx.Request) -> httpx.Response:
    assert request.url.params["padding"] == "false"
    return httpx.Response(200, json=FIXTURE)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _wrapped(handler, *, now=lambda: 1_700_000_000) -> AnyMintSource:
    transport = httpx.MockTransport(handler)
    birdeye = BirdeyeSource(
        api_key=_KEY,
        client=httpx.Client(transport=transport),
        min_interval=0,
        sleep=lambda _seconds: None,
        now=now,
    )
    return AnyMintSource(
        httpx.Client(transport=transport),
        birdeye=birdeye,
        api_key=_KEY,
        min_interval=0,
        sleep=lambda _seconds: None,
        now=now,
    )
