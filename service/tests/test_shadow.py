"""Quote-only shadow mode. Sockets stay blocked. The mock venue is in memory."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.test_paper_broker import _GRACE, _STEP
from tests.test_paper_deploy import _T, _bar, _deploy, _document, _fills, _save, _world

from app.auth import LOCAL_USER_ID
from app.live.scheduler import live_feed_enabled
from app.ohlcv.registry import SOL_MINT
from app.ohlcv.store import MAX_CACHE_TTL_SECONDS
from app.paper.broker import PaperBroker
from app.paper.shadow import (
    DEFAULT_SHADOW_SLIPPAGE_BPS,
    shadow_build_enabled,
    shadow_quotes_enabled,
    shadow_slippage_bps,
)
from app.venue.errors import VenueExecuteDisabled, VenueRejected
from app.venue.flags import live_trading_enabled
from app.venue.pace import DEFAULT_DAILY_CAP, DEFAULT_MIN_INTERVAL_SECONDS
from app.venue.types import QuoteRequest, VenueExpiry, VenueQuote

_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_TAKER = "29d2S7vB453rNYFdR5Ycwt7y9haRT5fwVwL9zTmBhfV2"


class _Calls:
    """A venue that quotes whatever it is asked and never sends."""

    def __init__(self, *, out_amount: int = 1_000_000, fail_build: bool = False) -> None:
        self.out_amount = out_amount
        self.fail_build = fail_build
        self.quotes: list[QuoteRequest] = []
        self.builds: list[object] = []
        self.executed = 0

    def quote(self, request: QuoteRequest, *, now: int) -> VenueQuote:
        self.quotes.append(request)
        return VenueQuote(
            input_mint=request.input_mint,
            output_mint=request.output_mint,
            in_amount=request.amount,
            out_amount=self.out_amount,
            minimum_out_amount=max(1, self.out_amount - 1),
            price_impact="-0.1",
            route_label="Synthetic",
            router="metis",
            expiry=VenueExpiry(200_000_000, None),
            request_id="shadow-test",
            quoted_at=now,
            side=request.side,
            slippage_bps=request.slippage_bps,
            fee_bps=None,
            fee_mint=None,
            fee_amount=None,
            swap_mode="ExactIn",
            network_fee_lamports=5000,
            priority_fee_lamports=1000,
            rent_fee_lamports=2039280,
        )

    def build(
        self,
        quote: VenueQuote,
        *,
        taker: str,
        now: int,
        block_height: int | None = None,
        slippage_bps: int | None = None,
    ) -> object:
        self.builds.append((quote, taker, now, block_height, slippage_bps))
        if self.fail_build:
            raise VenueRejected("amount_mismatch")
        return object()

    def execute(self, transaction: object = None) -> None:
        self.executed += 1
        raise VenueExecuteDisabled("execute_disabled")


def test_shadow_flag_is_exact_and_the_live_flags_stay_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHADOW_QUOTES_ENABLED", raising=False)
    monkeypatch.delenv("SHADOW_BUILD_ENABLED", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("LIVE_FEED_ENABLED", raising=False)
    monkeypatch.delenv("SHADOW_SLIPPAGE_BPS", raising=False)
    assert shadow_quotes_enabled() is False
    assert live_feed_enabled() is False
    assert shadow_build_enabled() is False
    assert live_trading_enabled() is False
    for value in ("", "0", "true", "yes", "on", " 1", "1 ", "2"):
        assert shadow_quotes_enabled(value) is False
        assert shadow_build_enabled(value) is False
        assert live_trading_enabled(value) is False
    assert shadow_quotes_enabled("1") is True
    assert shadow_build_enabled("1") is True
    assert live_trading_enabled("1") is True
    for value in ("1", "true", "yes", "on"):
        assert live_feed_enabled(value) is True
    assert live_feed_enabled("0") is False
    assert shadow_slippage_bps(None) == DEFAULT_SHADOW_SLIPPAGE_BPS == 50
    assert shadow_slippage_bps("  ") == 50
    assert shadow_slippage_bps("0") == 0
    assert shadow_slippage_bps("10000") == 10000
    with pytest.raises(ValueError):
        shadow_slippage_bps("10001")
    with pytest.raises(ValueError):
        shadow_slippage_bps("01")
    assert DEFAULT_MIN_INTERVAL_SECONDS == 1.0
    assert DEFAULT_DAILY_CAP == 1000


def test_a_fill_records_the_quote_and_does_not_change_the_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SHADOW_QUOTES_ENABLED", raising=False)
    monkeypatch.delenv("SHADOW_BUILD_ENABLED", raising=False)
    monkeypatch.delenv("SHADOW_SLIPPAGE_BPS", raising=False)
    quiet_venue = _Calls()
    quiet = _run(tmp_path / "off", venue=quiet_venue)
    assert quiet_venue.quotes == []
    monkeypatch.setenv("SHADOW_QUOTES_ENABLED", "1")
    loud_path = tmp_path / "on"
    venue = _Calls(out_amount=2_500_000_000)
    loud = _run(loud_path, venue=venue)
    assert quiet["fills"] == loud["fills"]
    assert quiet["fills"]
    assert quiet["wallet"] == loud["wallet"]
    assert quiet["position"] == loud["position"]
    assert venue.executed == 0
    assert venue.builds == []
    assert len(venue.quotes) == 1
    request = venue.quotes[0]
    assert request.input_mint == _USDC
    assert request.output_mint == SOL_MINT
    assert request.side == "buy"
    assert request.slippage_bps == 50
    body = loud["shadow"]
    assert body["enabled"] is True
    assert body["build_enabled"] is False
    assert body["amount_mismatch_rate"] is None
    assert body["build_attempts"] == 0
    quote = body["quotes"][0]
    assert quote["quote_status"] == "ok"
    assert quote["build_status"] == "not_requested"
    assert quote["route_label"] == "Synthetic"
    assert quote["router"] == "metis"
    assert quote["price_impact"] == "-0.1"
    assert quote["paper_out"]
    assert quote["quoted_out"] == "2.5"
    assert quote["network_fee_lamports"] == 5000
    assert quote["priority_fee_lamports"] == 1000
    assert quote["rent_lamports"] == 2039280
    assert isinstance(quote["latency_ms"], int)
    realized = loud["position"][5]
    assert "5000" not in str(realized)
    assert "2039280" not in str(realized)
    with pytest.raises(VenueExecuteDisabled):
        venue.execute()
    names = _columns(loud_path / "ohlcv.sqlite", "paper_shadow")
    for banned in ("payload", "body", "transaction", "api_key", "signature"):
        assert banned not in names


def test_build_mismatch_is_counted_and_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHADOW_QUOTES_ENABLED", "1")
    monkeypatch.setenv("SHADOW_BUILD_ENABLED", "1")
    monkeypatch.setenv("SHADOW_TAKER", _TAKER)
    monkeypatch.setenv("SHADOW_SLIPPAGE_BPS", "75")
    venue = _Calls(fail_build=True)
    ran = _run(tmp_path, venue=venue)
    assert venue.executed == 0
    assert len(venue.builds) == 1
    assert venue.builds[0][1] == _TAKER
    assert venue.builds[0][4] == 75
    shadow = ran["page"]
    assert shadow["mode"] == "shadow"
    assert shadow["simulated"] is True
    assert shadow["build_attempts"] == 1
    assert shadow["amount_mismatches"] == 1
    assert shadow["amount_mismatch_rate"] == "1.0000"
    assert shadow["quotes"][0]["build_status"] == "amount_mismatch"
    assert shadow["quotes"][0]["slippage_bps"] == 75
    assert "transaction" not in shadow["quotes"][0]


def test_shadow_rows_purge_at_24_hours_and_the_book_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHADOW_QUOTES_ENABLED", "1")
    clock = {"now": _T}
    _client, _strategies, bars, venue = _world_broker(tmp_path, clock, _Calls())
    try:
        _tick(bars, clock)
        assert bars.list_paper_shadow(user_id=LOCAL_USER_ID)
        wallet = bars.paper_wallet(user_id=LOCAL_USER_ID, strategy_id=_strategy_id(bars), version=1)
        clock["now"] = _T + _STEP + _GRACE + MAX_CACHE_TTL_SECONDS
        assert bars.list_paper_shadow(user_id=LOCAL_USER_ID) == []
        assert (
            bars.paper_wallet(user_id=LOCAL_USER_ID, strategy_id=_strategy_id(bars), version=1)
            == wallet
        )
        assert venue.executed == 0
    finally:
        bars.close()


def test_the_daily_cap_stops_the_venue_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHADOW_QUOTES_ENABLED", "1")
    monkeypatch.setenv("JUPITER_DAILY_CAP", "1")
    from app.paper.shadow import observe_shadow

    venue = _Calls()
    clock = {"now": _T}
    client, strategies, bars, _venue = _world_broker(tmp_path, clock, venue)
    try:
        _tick(bars, clock, venue)
        assert len(venue.quotes) == 1
        strategy_id = client.get("/paper").json()["strategies"][0]["strategy_id"]
        document = _document()
        connection = sqlite3.connect(bars.path)
        try:
            connection.execute("DELETE FROM paper_shadow")
            connection.commit()
        finally:
            connection.close()
        observe_shadow(
            bars,
            user_id=LOCAL_USER_ID,
            strategy_id=strategy_id,
            version=1,
            document=document,
            now=clock["now"],
            venue=venue,
        )
        assert len(venue.quotes) == 1
        assert client.get("/shadow").json()["daily_cap"] == 1
    finally:
        bars.close()
        strategies.close()


def test_a_backward_clock_does_not_reset_the_cap(tmp_path: Path) -> None:
    from app.ohlcv.store import SqliteBarStore

    clock = {"now": 2 * 86400}
    store = SqliteBarStore(tmp_path / "ohlcv.sqlite", now=lambda: clock["now"])
    try:
        assert store.take_shadow_call(now=clock["now"], cap=1) is True
        assert store.take_shadow_call(now=clock["now"], cap=1) is False
        assert store.take_shadow_call(now=clock["now"] - 86400, cap=1) is False
        assert store.take_shadow_call(now=clock["now"] + 86400, cap=1) is True
    finally:
        store.close()


def _run(path: Path, venue: _Calls | None = None) -> dict[str, object]:
    clock = {"now": _T}
    client, strategies, bars, broker_venue = _world_broker(path, clock, venue)
    try:
        _tick(bars, clock, broker_venue)
        strategy_id = client.get("/paper").json()["strategies"][0]["strategy_id"]
        wallet = bars.paper_wallet(user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1)
        position = bars.paper_positions(
            user_id=LOCAL_USER_ID, strategy_id=strategy_id, version=1
        )[0]
        return {
            "fills": _fills(bars, strategy_id),
            "wallet": wallet,
            "position": position,
            "shadow": client.get("/paper").json()["shadow"],
            "page": client.get("/shadow").json(),
        }
    finally:
        bars.close()
        strategies.close()


def _world_broker(path: Path, clock: dict[str, int], venue: _Calls | None):
    client, strategies, bars, _application = _world(path, clock)
    strategy_id, _version, _hash = _save(strategies, _document())
    assert _deploy(client, strategy_id).status_code == 201
    return client, strategies, bars, venue


def _tick(bars, clock: dict[str, int], venue: _Calls | None = None) -> None:
    broker = PaperBroker(bars, now=lambda: clock["now"], shadow_venue=venue)
    bars.upsert([_bar(_T, 100, high=101, low=99)], user_id=LOCAL_USER_ID)
    clock["now"] = _T + _STEP + _GRACE
    broker.tick()


def _strategy_id(bars) -> str:
    rows = bars.live_registrations(user_id=LOCAL_USER_ID)
    return str(rows[0][0])


def _columns(path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()
