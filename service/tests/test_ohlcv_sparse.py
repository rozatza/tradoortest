"""A thin pool: flagged synthetic 1m bars, and a native 5m candle."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from app.auth import LOCAL_USER_ID
from app.ohlcv.adapters.geckoterminal import GeckoTerminalSource
from app.ohlcv.backfill import run_backfill
from app.ohlcv.registry import JUP_MINT, MAJORS
from app.ohlcv.store import SqliteBarStore

_FIXTURE = Path(__file__).parent / "fixtures" / "ohlcv" / "geckoterminal_sparse_synthetic.json"
_START = 1_700_000_100
_END = _START + 600
_FORBIDDEN = (
    "authorization",
    "api-key",
    "api_key",
    "x-cg-",
    "bearer",
    "set-cookie",
    "account_id",
    "x-api-key",
)


def test_sparse_fixture_is_labeled_synthetic_and_has_no_secrets() -> None:
    text = _FIXTURE.read_text(encoding="utf-8")
    lowered = text.lower()
    payload = json.loads(text)
    assert payload["synthetic"] is True
    assert "not a live" in payload["note"].lower()
    for forbidden in _FORBIDDEN:
        assert forbidden not in lowered


def test_sparse_pool_flags_synthetic_minutes_and_keeps_native_5m(tmp_path: Path) -> None:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert "include_empty_intervals" not in request.url.params
        aggregate = request.url.params["aggregate"]
        seen.append((request.url.path, aggregate))
        if aggregate == "1":
            return httpx.Response(200, json=payload["minute_aggregate_1"])
        if aggregate == "5":
            return httpx.Response(200, json=payload["minute_aggregate_5"])
        raise AssertionError(aggregate)

    source = GeckoTerminalSource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        min_interval=0,
        sleep=lambda _seconds: None,
    )
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite")
    try:
        report = run_backfill(
            source=source,
            store=store,
            assets=(MAJORS[1],),
            start=_START,
            end=_END,
            timeframes=("1m", "5m"),
            user_id=LOCAL_USER_ID,
            generated_at=_END,
        )
        minute = store.load(
            user_id=LOCAL_USER_ID, mint=JUP_MINT, timeframe="1m", start=_START, end=_END
        )
        synthetic = [bar for bar in minute if bar.synthetic]
        real = [bar for bar in minute if not bar.synthetic]
        assert [bar.open_time for bar in real] == [_START + 60, _START + 180]
        assert _START not in {bar.open_time for bar in minute}
        assert synthetic
        assert all(bar.synthetic and bar.volume == 0.0 for bar in synthetic)
        assert all(bar.open == bar.high == bar.low == bar.close for bar in synthetic)
        assert next(bar for bar in synthetic if bar.open_time == _START + 120).close == 1.2
        assert next(bar for bar in synthetic if bar.open_time == _START + 240).close == 2.0
        five = store.load(
            user_id=LOCAL_USER_ID, mint=JUP_MINT, timeframe="5m", start=_START, end=_END
        )
        assert five
        assert five[0].synthetic is False
        assert five[0].open_time == _START
        sections = {section["timeframe"]: section for section in report["series"]}
        assert sections["1m"]["real"] == 2
        assert sections["1m"]["synthetic"] == 7
        assert sections["1m"]["coverage_pct"] == 20.0
        assert sections["1m"]["gaps"] == []
        assert sections["1m"]["unfetched"] == 0
        assert sections["5m"]["real"] == 1
        assert sections["5m"]["synthetic"] == 0
        assert sections["5m"]["gaps"] == []
        assert sections["5m"]["present"] == 1
        assert seen == [
            (f"/api/v2/networks/solana/pools/{seen[0][0].split('/')[6]}/ohlcv/minute", "1"),
            (f"/api/v2/networks/solana/pools/{seen[1][0].split('/')[6]}/ohlcv/minute", "5"),
        ]
    finally:
        store.close()
