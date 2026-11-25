"""Jupiter Swap API V2, quote and unsigned build.

``GET /order`` without ``taker`` is the quote. The documented response
has ``transaction: null``, so this call does not yield a signable
transaction. ``GET /build`` is the build. Its
``addressesByLookupTableAddress`` map is the lookup-table resolution;
nothing here calls an RPC. The unsigned transaction is compiled from
those instructions.

``POST /execute`` is the pinned send path. ``execute`` raises and does
not request it.

An API key is optional. Quotes are documented to work without one.
When ``JUPITER_API_KEY`` is set it is sent only as the ``x-api-key``
header. It is not put on the query, in an error, or in ``repr``.

Slippage is not sent. ``/order`` then uses the documented real-time
estimate. ``/build`` applies its documented default. The build is
rejected when its mints or amounts differ from the accepted quote.

This path is Metis-only. The docs describe JupiterZ RFQ as a single
``/order`` transaction whose market-maker signature is added when
``/execute`` lands that same transaction. The input and output result
fields on ``/execute`` are a fee split. This client does not call
``/execute``, so it does not hand an RFQ order to Jupiter to settle.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.ohlcv.retry import retry_delay
from app.venue.address import require_address
from app.venue.checks import (
    require_block_height,
    require_build_matches,
    require_clock,
    require_fresh,
    require_request,
)
from app.venue.errors import (
    VenueExecuteDisabled,
    VenueExpired,
    VenueRejected,
    VenueSchemaError,
    VenueTransportError,
)
from app.venue.pin import (
    BUILD_QUERY_KEYS,
    JUPITER_API_KEY_ENV,
    JUPITER_API_KEY_HEADER,
    JUPITER_BASE_URL,
    JUPITER_BUILD_PATH,
    JUPITER_ORDER_PATH,
    MAX_RESPONSE_BYTES,
    ORDER_QUERY_KEYS,
)
from app.venue.schema import loads, parse_build, parse_order
from app.venue.tx import assemble_unsigned
from app.venue.types import QuoteRequest, UnsignedTransaction, VenueQuote

_UNSET = object()
_ATTEMPTS = 5
_NO_RETRY = (httpx.UnsupportedProtocol, httpx.LocalProtocolError, httpx.ProxyError)
_PATH_KEYS = {
    JUPITER_ORDER_PATH: ORDER_QUERY_KEYS,
    JUPITER_BUILD_PATH: BUILD_QUERY_KEYS,
}


class JupiterVenue:
    """``ExecutionVenue`` backed by Swap API V2 fixtures or the live host.

    Tests pass an ``httpx.MockTransport`` client. The default client is
    created on the first call and does not follow redirects.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        api_key: str | None | object = _UNSET,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._key = api_key
        self._sleep = sleep
        self._rng = rng

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    def __enter__(self) -> JupiterVenue:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "set" if self._api_key() else "unset"
        return f"JupiterVenue(api_key={state})"

    def quote(self, request: QuoteRequest, *, now: int) -> VenueQuote:
        now = require_clock(now)
        require_request(request)
        payload = self._get_json(
            JUPITER_ORDER_PATH,
            {
                "inputMint": request.input_mint,
                "outputMint": request.output_mint,
                "amount": str(request.amount),
                "swapMode": "ExactIn",
            },
        )
        parsed = parse_order(payload, now)
        if parsed.input_mint != request.input_mint or parsed.output_mint != request.output_mint:
            raise VenueRejected("mint_mismatch")
        if parsed.in_amount != request.amount:
            raise VenueRejected("amount_mismatch")
        return VenueQuote(
            input_mint=parsed.input_mint,
            output_mint=parsed.output_mint,
            in_amount=parsed.in_amount,
            out_amount=parsed.out_amount,
            minimum_out_amount=parsed.minimum_out_amount,
            price_impact=parsed.price_impact,
            route_label=parsed.route_label,
            router=parsed.router,
            expiry=parsed.expiry,
            request_id=parsed.request_id,
            quoted_at=now,
            side=request.side,
            slippage_bps=parsed.slippage_bps,
            fee_bps=parsed.fee_bps,
            fee_mint=parsed.fee_mint,
            fee_amount=parsed.fee_amount,
            swap_mode=parsed.swap_mode,
        )

    def build(
        self,
        quote: VenueQuote,
        *,
        taker: str,
        now: int,
        block_height: int | None = None,
    ) -> UnsignedTransaction:
        now = require_clock(now)
        taker = require_address(taker)
        # The built transaction expires by block height. Without the current
        # height the build cannot be judged, so it is not requested.
        if block_height is None:
            raise VenueExpired("missing_block_height")
        current = require_block_height(block_height)
        require_fresh(quote, now, block_height)
        payload = self._get_json(
            JUPITER_BUILD_PATH,
            {
                "inputMint": quote.input_mint,
                "outputMint": quote.output_mint,
                "amount": str(quote.in_amount),
                "taker": taker,
                "transactionVersion": "0",
            },
        )
        parsed = parse_build(payload)
        if current > parsed.last_valid_block_height:
            raise VenueExpired("quote_expired")
        require_build_matches(
            quote,
            input_mint=parsed.input_mint,
            output_mint=parsed.output_mint,
            in_amount=parsed.in_amount,
            out_amount=parsed.out_amount,
            minimum_out_amount=parsed.minimum_out_amount,
        )
        return assemble_unsigned(
            payer=taker,
            instructions=parsed.instructions,
            lookup_tables=parsed.lookup_tables,
            recent_blockhash=parsed.recent_blockhash,
            quote=quote,
            last_valid_block_height=parsed.last_valid_block_height,
        )

    def execute(self, transaction: object = None) -> None:
        raise VenueExecuteDisabled("execute_disabled")

    def _get_json(self, path: str, params: dict[str, str]) -> Any:
        if path not in _PATH_KEYS:
            raise VenueExecuteDisabled("execute_disabled")
        if set(params) != _PATH_KEYS[path]:
            raise VenueSchemaError("bad_request")
        url = JUPITER_BASE_URL + path
        backoff = 1.0
        last_status = 0
        for attempt in range(_ATTEMPTS):
            try:
                response = self._http().get(url, params=params, headers=self._headers())
            except httpx.TimeoutException:
                if attempt == _ATTEMPTS - 1:
                    raise VenueTransportError("Jupiter request timed out") from None
                self._sleep(self._delay(backoff, None, None))
                backoff = min(backoff * 2, 120.0)
                continue
            except httpx.TransportError as exc:
                if isinstance(exc, _NO_RETRY) or attempt == _ATTEMPTS - 1:
                    raise VenueTransportError("Jupiter request failed") from None
                self._sleep(self._delay(backoff, None, None))
                backoff = min(backoff * 2, 120.0)
                continue
            except httpx.HTTPError:
                raise VenueTransportError("Jupiter request failed") from None
            last_status = response.status_code
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == _ATTEMPTS - 1:
                    raise VenueTransportError(f"Jupiter returned HTTP {response.status_code}")
                self._sleep(self._delay(backoff, response, response.status_code))
                backoff = min(backoff * 2, 120.0)
                continue
            if response.status_code != 200:
                raise VenueTransportError(f"Jupiter returned HTTP {response.status_code}")
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise VenueSchemaError("response_too_large")
            payload = loads(response.content)
            if not isinstance(payload, dict):
                raise VenueSchemaError("bad_object")
            return payload
        raise VenueTransportError(f"Jupiter returned HTTP {last_status}")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        key = self._api_key()
        if key:
            headers[JUPITER_API_KEY_HEADER] = key
        return headers

    def _api_key(self) -> str:
        raw = self._key
        if raw is _UNSET:
            raw = os.environ.get(JUPITER_API_KEY_ENV)
        if not isinstance(raw, str):
            return ""
        text = raw.strip()
        if text == "" or any(char.isspace() for char in text):
            return ""
        return text

    def _delay(
        self, backoff: float, response: httpx.Response | None, status: int | None
    ) -> float:
        return retry_delay(backoff, rng=self._rng, response=response, status=status)

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=30.0,
                follow_redirects=False,
                headers={"Accept": "application/json", "User-Agent": "tradoortest-venue"},
            )
            self._owns_client = True
        return self._client
