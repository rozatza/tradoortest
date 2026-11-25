"""ExecutionVenue contract, Jupiter fixtures, and the hostile-response rejects.

Sockets stay blocked by the autouse fixture in conftest. These tests use
recorded bodies and an in-memory transport.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.venue.errors import (
    VenueError,
    VenueExecuteDisabled,
    VenueExpired,
    VenueRejected,
    VenueSchemaError,
    VenueTransportError,
)
from app.venue.flags import live_trading_enabled
from app.venue.jupiter import JupiterVenue
from app.venue.mock import MockVenue
from app.venue.pin import (
    JUPITER_API_VERSION,
    JUPITER_BASE_URL,
    JUPITER_BUILD_PATH,
    JUPITER_EXECUTE_PATH,
    JUPITER_ORDER_PATH,
    QUOTE_TTL_SECONDS,
)
from app.venue.types import (
    AccountRole,
    DecodedInstruction,
    ExecutionVenue,
    QuoteRequest,
    UnsignedTransaction,
    purge_quote_records,
)

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "venue"
_SOL = "So11111111111111111111111111111111111111112"
_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_TAKER = "29d2S7vB453rNYFdR5Ycwt7y9haRT5fwVwL9zTmBhfV2"
_PROGRAM = "3JF3sEqM796hk5WFqA6EtmEwJQ9quALszsfJyvXNQKy3"
_POOL = "4Ss5JMkXAD9Z7cktFEdrqeMuT6jGMF1pVozTyPHZ6zT4"
_TABLE = "5bV6jUfhDHCQVA1WfKBUnXUsboJgoKgkzkKcxr3joew5"
_OTHER = "6k78AbasGMFFrhG95Pj6jQbqkVt7FQMhVgemxJovWKR6"
_AMOUNT = 100_000_000
_OUT = 11_680_143
_MINIMUM = 11_600_000
_HEIGHT = 200_000_000
_NOW = 1_700_000_000
_LEAK = "provider-body-must-not-leak"
_KEY = "jupiter-test-key"


@pytest.fixture(autouse=True)
def _clear_venue_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)


def _body(name: str) -> dict[str, object]:
    wrapper = json.loads((_FIXTURES / name).read_text())
    assert wrapper["synthetic"] is True
    body = wrapper["body"]
    assert isinstance(body, dict)
    return body


def _request(side: str = "buy") -> QuoteRequest:
    return QuoteRequest(_SOL, _USDC, _AMOUNT, side)


def _instructions() -> tuple[DecodedInstruction, ...]:
    return (
        DecodedInstruction(
            _PROGRAM,
            (
                AccountRole(_TAKER, True, True),
                AccountRole(_SOL, False, False),
                AccountRole(_USDC, False, False),
                AccountRole(_POOL, False, True),
                AccountRole(_OTHER, False, False),
            ),
            b"\x01",
        ),
    )


def _mock() -> MockVenue:
    return MockVenue(
        input_mint=_SOL,
        output_mint=_USDC,
        in_amount=_AMOUNT,
        out_amount=_OUT,
        minimum_out_amount=_MINIMUM,
        price_impact="-0.1",
        route_label="Synthetic",
        router="metis",
        last_valid_block_height=_HEIGHT,
        instructions=_instructions(),
        lookup_tables=((_TABLE, (_OTHER, _POOL)),),
        recent_blockhash=bytes(range(32)),
    )


class _Script:
    def __init__(self, order: dict[str, object], build: dict[str, object]) -> None:
        self.order = order
        self.build = build
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url).endswith(JUPITER_EXECUTE_PATH) or request.url.path.endswith(
            JUPITER_EXECUTE_PATH
        ):
            return httpx.Response(500, text=_LEAK)
        if request.url.path.endswith(JUPITER_ORDER_PATH):
            return httpx.Response(200, json=self.order)
        if request.url.path.endswith(JUPITER_BUILD_PATH):
            return httpx.Response(200, json=self.build)
        return httpx.Response(404, text=_LEAK)

    def venue(self, *, api_key: str | None = None) -> JupiterVenue:
        client = httpx.Client(transport=httpx.MockTransport(self.handler))
        return JupiterVenue(client, api_key=api_key, sleep=self.sleeps.append)


def _assert_guard(built: UnsignedTransaction) -> None:
    assert built.transaction_version == 0
    assert built.fee_payer == _TAKER
    assert built.taker == _TAKER
    assert built.account_keys[0] == _TAKER
    assert built.transaction[0] == 1
    assert built.transaction[1:65] == b"\x00" * 64
    assert built.transaction[65] == 0x80
    assert built.static_account_count == 4
    assert built.account_keys[1] == _PROGRAM
    assert built.account_keys[2] == _SOL
    assert built.account_keys[3] == _USDC
    assert built.account_keys[4] == _POOL
    assert built.account_keys[5] == _OTHER
    assert _POOL not in built.account_keys[: built.static_account_count]
    assert _OTHER not in built.account_keys[: built.static_account_count]
    assert len(built.instructions) == 1
    instruction = built.instructions[0]
    assert instruction.program_id == _PROGRAM
    assert instruction.data == b"\x01"
    roles = {account.pubkey: account for account in instruction.accounts}
    assert roles[_TAKER].signer and roles[_TAKER].writable
    assert roles[_POOL].writable and not roles[_POOL].signer
    assert not roles[_OTHER].signer and not roles[_OTHER].writable
    assert not roles[_SOL].signer and not roles[_USDC].writable
    for account in instruction.accounts:
        assert account.pubkey in built.account_keys
    assert built.lookup_tables[0].address == _TABLE
    assert built.lookup_tables[0].writable_indexes == (1,)
    assert built.lookup_tables[0].readonly_indexes == (0,)
    assert built.in_amount == _AMOUNT
    assert built.out_amount == _OUT
    assert built.minimum_out_amount == _MINIMUM
    assert built.input_mint == _SOL
    assert built.output_mint == _USDC
    assert built.last_valid_block_height == _HEIGHT


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_mock_and_jupiter_satisfy_the_venue_contract(side: str) -> None:
    request = _request(side)
    mock = _mock()
    script = _Script(_body("order.json"), _body("build.json"))
    jupiter = script.venue()
    for venue in (mock, jupiter):
        assert isinstance(venue, ExecutionVenue)
        quote = venue.quote(request, now=_NOW)
        assert quote.input_mint == _SOL
        assert quote.output_mint == _USDC
        assert quote.in_amount == _AMOUNT
        assert quote.out_amount == _OUT
        assert quote.minimum_out_amount == _MINIMUM
        assert quote.route_label == "Synthetic"
        assert quote.router == "metis"
        assert quote.swap_mode == "ExactIn"
        assert quote.side == side
        assert quote.quoted_at == _NOW
        assert quote.price_impact == "-0.1"
        assert quote.expiry.last_valid_block_height == _HEIGHT
        record = quote.as_record()
        assert "payload" not in record
        assert "transaction" not in record
        assert "raw" not in record
        built = venue.build(quote, taker=_TAKER, now=_NOW, block_height=_HEIGHT)
        _assert_guard(built)
        with pytest.raises(VenueExecuteDisabled, match="execute_disabled"):
            venue.execute(built)
        with pytest.raises(VenueExpired, match="quote_expired"):
            venue.build(quote, taker=_TAKER, now=_NOW + QUOTE_TTL_SECONDS, block_height=_HEIGHT)
    paths = [item.url.path for item in script.requests]
    assert paths == ["/swap/v2/order", "/swap/v2/build"]
    assert all(not item.url.path.endswith(JUPITER_EXECUTE_PATH) for item in script.requests)


def test_jupiter_quote_sends_no_taker_and_no_slippage() -> None:
    script = _Script(_body("order.json"), _body("build.json"))
    script.venue().quote(_request(), now=_NOW)
    request = script.requests[0]
    assert request.url.params["inputMint"] == _SOL
    assert request.url.params["outputMint"] == _USDC
    assert request.url.params["amount"] == str(_AMOUNT)
    assert request.url.params["swapMode"] == "ExactIn"
    assert set(request.url.params) == {"inputMint", "outputMint", "amount", "swapMode"}
    assert "x-api-key" not in request.headers
    assert str(request.url).startswith(JUPITER_BASE_URL + JUPITER_ORDER_PATH)


def test_jupiter_build_pins_version_zero_and_the_taker() -> None:
    script = _Script(_body("order.json"), _body("build.json"))
    venue = script.venue()
    quote = venue.quote(_request(), now=_NOW)
    venue.build(quote, taker=_TAKER, now=_NOW, block_height=_HEIGHT)
    request = script.requests[1]
    assert set(request.url.params) == {
        "inputMint",
        "outputMint",
        "amount",
        "taker",
        "transactionVersion",
    }
    assert request.url.params["taker"] == _TAKER
    assert request.url.params["transactionVersion"] == "0"
    assert "slippageBps" not in request.url.params
    assert "payer" not in request.url.params
    assert "tipAmount" not in request.url.params


def test_api_key_is_header_only_and_stays_out_of_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUPITER_API_KEY", _KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == _KEY
        assert _KEY not in str(request.url)
        return httpx.Response(500, text=_KEY)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    venue = JupiterVenue(client, sleep=lambda _delay: None)
    assert _KEY not in repr(venue)
    with pytest.raises(VenueTransportError, match="HTTP 500") as caught:
        venue.quote(_request(), now=_NOW)
    assert _KEY not in str(caught.value)
    assert _LEAK not in str(caught.value)


def test_a_provider_body_is_not_part_of_a_transport_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=_LEAK)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    venue = JupiterVenue(client, api_key=None, sleep=lambda _delay: None)
    with pytest.raises(VenueTransportError, match="HTTP 400") as caught:
        venue.quote(_request(), now=_NOW)
    assert _LEAK not in str(caught.value)


def test_retry_uses_retry_after_then_accepts_the_fixture() -> None:
    script = _Script(_body("order.json"), _body("build.json"))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text=_LEAK, headers={"Retry-After": "3"})
        return script.handler(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    venue = JupiterVenue(client, api_key=None, sleep=script.sleeps.append)
    quote = venue.quote(_request(), now=_NOW)
    assert quote.out_amount == _OUT
    assert script.sleeps == [3.0]
    assert _LEAK not in repr(quote)


def test_five_failures_stop_and_do_not_keep_the_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=_LEAK)

    sleeps: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    venue = JupiterVenue(client, api_key=None, sleep=sleeps.append)
    with pytest.raises(VenueTransportError, match="HTTP 503") as caught:
        venue.quote(_request(), now=_NOW)
    assert _LEAK not in str(caught.value)
    assert len(sleeps) == 4


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda body: body.__setitem__("note", "extra"), "unknown_field"),
        (lambda body: body.pop("routePlan"), "missing_route"),
        (lambda body: body.__setitem__("routePlan", []), "missing_route"),
        (lambda body: body["routePlan"][0]["swapInfo"].pop("label"), "missing_route"),
        (lambda body: body["routePlan"][0]["swapInfo"].__setitem__("extra", 1), "unknown_field"),
        (lambda body: body.__setitem__("outputMint", _POOL), "mint_mismatch"),
        (lambda body: body.__setitem__("inAmount", "100000001"), "amount_mismatch"),
        (lambda body: body.__setitem__("transaction", "AQ=="), "unexpected_transaction"),
    ],
)
def test_hostile_order_responses_are_rejected(mutate, code: str) -> None:
    order = deepcopy(_body("order.json"))
    mutate(order)
    script = _Script(order, _body("build.json"))
    with pytest.raises((VenueSchemaError, VenueRejected), match=code):
        script.venue().quote(_request(), now=_NOW)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda body: body.__setitem__("note", "extra"), "unknown_field"),
        (lambda body: body.pop("routePlan"), "missing_route"),
        (lambda body: body.__setitem__("routePlan", []), "missing_route"),
        (lambda body: body.__setitem__("inputMint", _POOL), "mint_mismatch"),
        (lambda body: body.__setitem__("outAmount", "1"), "amount_mismatch"),
        (lambda body: body.__setitem__("otherAmountThreshold", "1"), "amount_mismatch"),
        (
            lambda body: body["swapInstruction"]["accounts"][4].__setitem__("isSigner", True),
            "extra_signer",
        ),
        (lambda body: body.__setitem__("transactionVersion", 1), "unsupported_version"),
        (
            lambda body: body.__setitem__("tipInstruction", deepcopy(body["swapInstruction"])),
            "unexpected_tip",
        ),
    ],
)
def test_hostile_build_responses_are_rejected(mutate, code: str) -> None:
    build = deepcopy(_body("build.json"))
    mutate(build)
    script = _Script(_body("order.json"), build)
    venue = script.venue()
    quote = venue.quote(_request(), now=_NOW)
    with pytest.raises((VenueSchemaError, VenueRejected), match=code):
        venue.build(quote, taker=_TAKER, now=_NOW, block_height=_HEIGHT)


def test_duplicate_json_keys_are_rejected() -> None:
    raw = b'{"inputMint":"a","inputMint":"b"}'

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    venue = JupiterVenue(client, api_key=None, sleep=lambda _delay: None)
    with pytest.raises(VenueSchemaError, match="duplicate_field"):
        venue.quote(_request(), now=_NOW)


def test_an_rfq_timestamp_is_an_expiry_and_a_past_one_is_not_a_quote() -> None:
    order = deepcopy(_body("order.json"))
    order.pop("lastValidBlockHeight")
    order["router"] = "jupiterz"
    order["expireAt"] = "2023-11-15T00:00:00Z"
    script = _Script(order, _body("build.json"))
    quote = script.venue().quote(_request(), now=_NOW)
    assert quote.expiry.last_valid_block_height is None
    assert quote.expiry.expire_at_unix is not None
    assert quote.expiry.expire_at_unix > _NOW
    order["expireAt"] = "1699999000"
    script = _Script(order, _body("build.json"))
    with pytest.raises(VenueExpired, match="quote_expired"):
        script.venue().quote(_request(), now=_NOW)


def test_a_quote_without_either_expiry_is_rejected() -> None:
    order = deepcopy(_body("order.json"))
    order.pop("lastValidBlockHeight")
    script = _Script(order, _body("build.json"))
    with pytest.raises(VenueSchemaError, match="missing_expiry"):
        script.venue().quote(_request(), now=_NOW)


def test_build_rejects_a_block_height_past_the_quote_without_a_second_call() -> None:
    script = _Script(_body("order.json"), _body("build.json"))
    venue = script.venue()
    quote = venue.quote(_request(), now=_NOW)
    with pytest.raises(VenueExpired, match="quote_expired"):
        venue.build(quote, taker=_TAKER, now=_NOW, block_height=_HEIGHT + 1)
    assert [item.url.path for item in script.requests] == ["/swap/v2/order"]


def test_mock_rejects_a_mismatched_mint_and_amount() -> None:
    venue = _mock()
    quote = venue.quote(_request(), now=_NOW)
    with pytest.raises(VenueRejected, match="mint_mismatch"):
        venue.build(replace(quote, input_mint=_POOL), taker=_TAKER, now=_NOW, block_height=_HEIGHT)
    with pytest.raises(VenueRejected, match="amount_mismatch"):
        venue.build(
            replace(quote, out_amount=_OUT - 1),
            taker=_TAKER,
            now=_NOW,
            block_height=_HEIGHT,
        )
    with pytest.raises(VenueRejected, match="amount_mismatch"):
        venue.quote(replace(_request(), amount=_AMOUNT + 1), now=_NOW)
    with pytest.raises(VenueRejected, match="mint_mismatch"):
        venue.quote(replace(_request(), output_mint=_POOL), now=_NOW)
    with pytest.raises(VenueSchemaError, match="missing_route"):
        venue.build(
            replace(quote, route_label="Other"),
            taker=_TAKER,
            now=_NOW,
            block_height=_HEIGHT,
        )


def test_a_short_side_and_a_mint_cycled_to_itself_are_rejected() -> None:
    venue = _mock()
    with pytest.raises(VenueRejected, match="bad_side"):
        venue.quote(QuoteRequest(_SOL, _USDC, _AMOUNT, "short"), now=_NOW)
    with pytest.raises(VenueRejected, match="mint_mismatch"):
        venue.quote(QuoteRequest(_SOL, _SOL, _AMOUNT, "buy"), now=_NOW)


def test_quote_records_purge_at_24_hours_and_drop_a_provider_body() -> None:
    quote = _mock().quote(_request(), now=_NOW)
    record = quote.as_record()
    assert purge_quote_records([record], _NOW + QUOTE_TTL_SECONDS - 1) == (record,)
    assert purge_quote_records([record], _NOW + QUOTE_TTL_SECONDS) == ()
    leaked = dict(record)
    leaked["payload"] = {"transaction": "AQ=="}
    assert purge_quote_records([leaked], _NOW) == ()


def test_live_trading_defaults_off_and_execute_still_raises() -> None:
    assert live_trading_enabled() is False
    assert live_trading_enabled(None) is False
    for value in ("", "0", "true", "yes", "on", " 1", "1 ", "2"):
        assert live_trading_enabled(value) is False
    assert live_trading_enabled("1") is True
    script = _Script(_body("order.json"), _body("build.json"))
    venue = script.venue()
    with pytest.raises(VenueExecuteDisabled, match="execute_disabled"):
        venue.execute()
    assert script.requests == []


def test_execute_helper_refuses_the_path_before_http() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    venue = JupiterVenue(client, api_key=None)
    with pytest.raises(VenueExecuteDisabled, match="execute_disabled"):
        venue._get_json(JUPITER_EXECUTE_PATH, {}, _NOW)


def test_pin_names_the_verified_swap_api() -> None:
    assert JUPITER_API_VERSION == "2.0.0"
    assert JUPITER_BASE_URL == "https://api.jup.ag/swap/v2"
    assert JUPITER_ORDER_PATH == "/order"
    assert JUPITER_BUILD_PATH == "/build"
    assert JUPITER_EXECUTE_PATH == "/execute"


def test_runner_core_and_the_legacy_engine_are_unchanged() -> None:
    runner = hashlib.sha256((_ROOT / "service/app/runner/core.py").read_bytes()).hexdigest()
    legacy = hashlib.sha256(
        (_ROOT / "service/tests/legacy/backtest_engine_e56685c.py").read_bytes()
    ).hexdigest()
    assert runner == "d2989f02b4b5ff99aaa5833e922145e9638fa3898200615dd6141cc006a0c126"
    assert legacy == "4bc6fcc2fce84df4dbb0844e10e4e86e82d30e4d895112d4a8482205d2264798"
    text = (_ROOT / "service/app/runner/core.py").read_text()
    assert "Jupiter" not in text
    assert "ExecutionVenue" not in text


def test_slippage_bps_is_sent_on_order_and_build() -> None:
    script = _Script(_body("order.json"), _body("build.json"))
    venue = script.venue()
    quote = venue.quote(QuoteRequest(_SOL, _USDC, _AMOUNT, "buy", 50), now=_NOW)
    venue.build(quote, taker=_TAKER, now=_NOW, block_height=_HEIGHT, slippage_bps=50)
    assert script.requests[0].url.params["slippageBps"] == "50"
    assert script.requests[1].url.params["slippageBps"] == "50"
    assert quote.network_fee_lamports == 5000
    assert quote.priority_fee_lamports is None
    assert quote.rent_fee_lamports is None


def test_the_daily_cap_and_the_interval_bound_jupiter_calls() -> None:
    script = _Script(_body("order.json"), _body("build.json"))
    client = httpx.Client(transport=httpx.MockTransport(script.handler))
    venue = JupiterVenue(
        client,
        api_key=None,
        sleep=script.sleeps.append,
        clock=lambda: 0.0,
        min_interval=1.0,
        daily_limit=1,
    )
    venue.quote(_request(), now=_NOW)
    with pytest.raises(VenueRejected, match="quote_cap"):
        venue.quote(_request(), now=_NOW)
    assert len(script.requests) == 1
    spaced = JupiterVenue(
        httpx.Client(transport=httpx.MockTransport(script.handler)),
        api_key=None,
        sleep=script.sleeps.append,
        clock=lambda: 0.0,
        min_interval=1.0,
        daily_limit=10,
    )
    quote = spaced.quote(_request(), now=_NOW)
    spaced.build(quote, taker=_TAKER, now=_NOW, block_height=_HEIGHT)
    assert script.sleeps == [1.0]


def test_a_bad_address_fails_before_a_request() -> None:
    script = _Script(_body("order.json"), _body("build.json"))
    with pytest.raises(VenueError, match="invalid_address"):
        script.venue().quote(QuoteRequest("not-a-mint", _USDC, _AMOUNT, "buy"), now=_NOW)
    assert script.requests == []
