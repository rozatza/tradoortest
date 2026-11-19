"""GeckoTerminal adapter against a canned transport. No live socket."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.ohlcv.adapters.geckoterminal import GeckoTerminalSource, _parse_candle
from app.ohlcv.errors import SourceError, UnknownMint
from app.ohlcv.registry import SOL_MINT

FIXTURE = Path(__file__).parent / "fixtures" / "ohlcv" / "geckoterminal_minute.json"
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


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_committed_fixture_is_synthetic_and_has_no_secrets() -> None:
    text = FIXTURE.read_text(encoding="utf-8").lower()
    for forbidden in _FORBIDDEN:
        assert forbidden not in text
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "meta" not in payload
    assert set(payload) == {"data"}


def test_fixture_page_parses_without_keeping_extra_fields() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["meta"] = {"note": "do-not-store-this-token", "headers": {"x-api-key": "nope"}}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert "include_empty_intervals" not in request.url.params
        assert request.url.params["currency"] == "usd"
        assert request.url.params["token"] == SOL_MINT
        assert request.url.params["aggregate"] == "1"
        banned = {"authorization", "x-api-key", "x-cg-demo-api-key", "x-cg-pro-api-key"}
        for name in request.headers:
            assert name.lower() not in banned
        return httpx.Response(200, json=payload)

    source = GeckoTerminalSource(_client(handler), min_interval=0, sleep=lambda _seconds: None)
    result = source.fetch_bars(SOL_MINT, "1m", 1_700_000_040, 1_700_000_160)
    assert seen
    assert [bar.open_time for bar in result.bars] == [1_700_000_040, 1_700_000_100]
    assert result.bars[0].open == 1.0
    assert result.bars[0].volume == 10.0
    assert result.rejected == []
    blob = repr(result)
    assert "do-not-store-this-token" not in blob
    assert "x-api-key" not in blob


def test_pagination_walks_backward_and_paces_calls() -> None:
    start = 1_700_000_040
    stamps = [start, start + 60, start + 120]
    sleeps: list[float] = []

    def candle(stamp: int) -> list[float]:
        return [stamp, 1.0, 2.0, 0.5, 1.5, 4.0]

    def handler(request: httpx.Request) -> httpx.Response:
        before = int(request.url.params["before_timestamp"])
        page = [candle(stamp) for stamp in reversed(stamps) if stamp <= before][:2]
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": page}}})

    source = GeckoTerminalSource(
        _client(handler),
        page_limit=2,
        min_interval=12,
        sleep=sleeps.append,
        monotonic=lambda: 0.0,
    )
    result = source.fetch_bars(SOL_MINT, "1m", start, start + 180)
    assert [bar.open_time for bar in result.bars] == stamps
    assert 12 in sleeps


def test_http_429_backs_off_without_reading_the_body_into_the_error() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"message": "secret-token-should-not-leak"})
        return httpx.Response(
            200,
            json={"data": {"attributes": {"ohlcv_list": [[1_700_000_040, 1, 1, 1, 1, 1]]}}},
        )

    source = GeckoTerminalSource(_client(handler), min_interval=0, sleep=lambda _seconds: None)
    result = source.fetch_bars(SOL_MINT, "1m", 1_700_000_040, 1_700_000_100)
    assert len(result.bars) == 1
    assert calls["n"] == 2


def test_repeated_429_error_does_not_include_the_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="secret-token-should-not-leak")

    source = GeckoTerminalSource(_client(handler), min_interval=0, sleep=lambda _seconds: None)
    with pytest.raises(SourceError, match="HTTP 429") as caught:
        source.fetch_bars(SOL_MINT, "1m", 1_700_000_040, 1_700_000_100)
    assert "secret-token-should-not-leak" not in str(caught.value)


def test_unknown_mint_is_refused_without_a_request() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unknown mint must not call the network")

    source = GeckoTerminalSource(_client(handler), min_interval=0, sleep=lambda _seconds: None)
    with pytest.raises(UnknownMint, match="fetch_bars"):
        source.fetch_bars("not-a-configured-mint", "1m", 0, 60)


def test_higher_timeframe_is_resampled_and_a_bad_minute_is_not_repaired() -> None:
    start = 1_700_000_100
    good = [[start + offset, 1.0, 2.0, 0.5, 1.5, 1.0] for offset in (0, 60, 120, 180, 240)]

    def handler(_request: httpx.Request) -> httpx.Response:
        body = {"data": {"attributes": {"ohlcv_list": list(reversed(good))}}}
        return httpx.Response(200, json=body)

    source = GeckoTerminalSource(_client(handler), min_interval=0, sleep=lambda _seconds: None)
    result = source.fetch_bars(SOL_MINT, "5m", start, start + 300)
    assert len(result.bars) == 1
    assert result.bars[0].timeframe == "5m"
    assert result.bars[0].open_time == start
    assert result.bars[0].volume == 5.0

    broken = [row[:] for row in good]
    broken[2] = [start + 120, 1.0, 1.0, 0.5, 4.0, 1.0]

    def broken_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"attributes": {"ohlcv_list": list(reversed(broken))}}}
        )

    broken_source = GeckoTerminalSource(
        _client(broken_handler), min_interval=0, sleep=lambda _seconds: None
    )
    failed = broken_source.fetch_bars(SOL_MINT, "5m", start, start + 300)
    assert failed.bars == []
    assert failed.rejected
    assert "high is below max(open, close)" in failed.rejected[0].reasons


def test_unparseable_candle_is_flagged() -> None:
    bar, rejection, _stamp = _parse_candle(["nope", True, "x", None, [], {}], SOL_MINT, 3)
    assert bar is None
    assert rejection is not None
    assert rejection.open_time is None
    assert any("not an integer" in reason for reason in rejection.reasons)
    assert any("not a number" in reason for reason in rejection.reasons)


def test_non_json_body_does_not_echo_the_payload() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="secret-token-should-not-leak")

    source = GeckoTerminalSource(_client(handler), min_interval=0, sleep=lambda _seconds: None)
    with pytest.raises(SourceError, match="not JSON") as caught:
        source.fetch_bars(SOL_MINT, "1m", 1_700_000_040, 1_700_000_100)
    assert "secret-token-should-not-leak" not in str(caught.value)
