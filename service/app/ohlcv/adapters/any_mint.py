"""Any-mint OHLCV: GeckoTerminal pool resolution, optional Birdeye fallback.

``fetch_bars(mint, timeframe, start, end)`` is the ROZ-69 seam. The mint
is checked before a request. The pool is chosen by liquidity (see
``app.ohlcv.pools``). Candles come from that pool. When GeckoTerminal has
no pool, or a pool but no candles, and ``BIRDEYE_API_KEY`` is set, Birdeye
``/defi/v3/ohlcv`` may serve the series. With no key that step is skipped
and is not an error.

Each series records which provider served it. The chosen pool, when there
is one, is available from ``pool_for``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx

from app.ohlcv.adapters.birdeye import DEFAULT_CU_BUDGET, BirdeyeSource
from app.ohlcv.adapters.geckoterminal import BASE_URL, GeckoTerminalSource, _Pace
from app.ohlcv.errors import NoDexPoolYet, NoPool, RateLimitExhausted, SourceError
from app.ohlcv.mint import require_mint
from app.ohlcv.pools import POOL_PAGE_SIZE, ChosenPool, choose_pool, token_is_ungraduated
from app.ohlcv.source import FetchPage, FetchResult

_UNSET = object()

DEFAULT_GT_MAX_CALLS = 180
_ATTEMPTS = 5
_NO_RETRY = (httpx.UnsupportedProtocol, httpx.LocalProtocolError, httpx.ProxyError)


class CallBudget:
    """Hard cap on GeckoTerminal HTTP calls for one source, including retries."""

    def __init__(self, limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("GeckoTerminal call cap must be a positive whole number")
        self.limit = limit
        self.used = 0

    def take(self, _request: object = None) -> None:
        if self.used >= self.limit:
            raise RateLimitExhausted("GeckoTerminal request cap exhausted")
        self.used += 1


class AnyMintSource:
    """Resolve a mint to a pool, then read OHLCV through the existing client."""

    name = "any-mint"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        birdeye: BirdeyeSource | None = None,
        api_key: str | None | object = _UNSET,
        min_interval: float = 12.0,
        max_calls: int = DEFAULT_GT_MAX_CALLS,
        max_pool_pages: int = 1,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], int] | None = None,
        page_limit: int = 1000,
        birdeye_budget: int | None = None,
        birdeye_ledger: Path | None = None,
    ) -> None:
        if isinstance(max_pool_pages, bool) or not isinstance(max_pool_pages, int):
            raise ValueError("pool page cap must be a positive whole number")
        if max_pool_pages < 1:
            raise ValueError("pool page cap must be a positive whole number")
        self._budget = CallBudget(max_calls)
        self._owns_client = client is None
        self._client = client if client is not None else _new_client(self._budget)
        if client is not None:
            self._client.event_hooks.setdefault("request", []).append(self._budget.take)
        self._sleep = sleep
        self._pace = _Pace(min_interval, sleep, monotonic)
        self._now = (lambda: int(time.time())) if now is None else now
        self._max_pool_pages = max_pool_pages
        self._pool_map: dict[str, str] = {}
        self._chosen: dict[str, ChosenPool] = {}
        self._missing: dict[str, NoPool | NoDexPoolYet] = {}
        self._served: dict[tuple[str, str], str] = {}
        self._gt = GeckoTerminalSource(
            self._client,
            pools=self._pool_map,
            min_interval=min_interval,
            sleep=sleep,
            monotonic=monotonic,
            pace=self._pace,
            page_limit=page_limit,
        )
        if birdeye is not None:
            self._birdeye = birdeye
        else:
            kwargs: dict[str, Any] = {
                "sleep": sleep,
                "monotonic": monotonic,
                "now": self._now,
                "budget": DEFAULT_CU_BUDGET if birdeye_budget is None else birdeye_budget,
            }
            if api_key is not _UNSET:
                kwargs["api_key"] = api_key
            if birdeye_ledger is not None:
                kwargs["ledger_path"] = birdeye_ledger
            self._birdeye = BirdeyeSource(**kwargs)

    def __repr__(self) -> str:
        return f"AnyMintSource(birdeye={self._birdeye.enabled})"

    def close(self) -> None:
        self._gt.close()
        if self._owns_client:
            self._client.close()
        self._birdeye.close()

    def pool_for(self, mint: str) -> ChosenPool | None:
        return self._chosen.get(mint)

    def served_provider(self, mint: str, timeframe: str) -> str:
        return self._served.get((mint, timeframe), "geckoterminal")

    def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult:
        bars = []
        rejected = []
        for page in self.fetch_pages(mint, timeframe, start, end):
            bars.extend(page.bars)
            rejected.extend(page.rejected)
        bars.sort(key=lambda bar: bar.open_time)
        return FetchResult(bars, rejected)

    def fetch_pages(self, mint: str, timeframe: str, start: int, end: int) -> Iterator[FetchPage]:
        require_mint(mint)
        failure = self._resolve(mint)
        if failure is not None:
            yield from self._fallback_or_raise(mint, timeframe, start, end, failure)
            return
        self._pool_map[mint] = self._chosen[mint].address
        try:
            pending = self._gt.fetch_pages(mint, timeframe, start, end)
            first = next(pending, None)
        except SourceError as exc:
            _reraise_rate_limit(exc)
            raise
        if first is None:
            return
        if first.bars or first.rejected or not first.provider_empty:
            self._served[(mint, timeframe)] = "geckoterminal"
            yield first
            try:
                yield from pending
            except SourceError as exc:
                _reraise_rate_limit(exc)
                raise
            return
        if self._birdeye.enabled:
            pages = self._birdeye_pages(mint, timeframe, start, end)
            if any(page.bars for page in pages):
                self._served[(mint, timeframe)] = "birdeye"
                yield from pages
                return
        self._served[(mint, timeframe)] = "geckoterminal"
        yield first

    def _fallback_or_raise(
        self,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
        failure: NoPool | NoDexPoolYet,
    ) -> Iterator[FetchPage]:
        if not self._birdeye.enabled:
            raise failure
        pages = self._birdeye_pages(mint, timeframe, start, end)
        if any(page.bars for page in pages):
            self._served[(mint, timeframe)] = "birdeye"
            yield from pages
            return
        raise failure

    def _birdeye_pages(self, mint: str, timeframe: str, start: int, end: int) -> list[FetchPage]:
        return list(self._birdeye.fetch_pages(mint, timeframe, start, end))

    def _resolve(self, mint: str) -> NoPool | NoDexPoolYet | None:
        if mint in self._chosen:
            return None
        if mint in self._missing:
            return self._missing[mint]
        try:
            payload = self._pool_pages(mint)
            chosen = choose_pool(payload, mint, resolved_at=int(self._now()))
        except NoDexPoolYet as exc:
            self._missing[mint] = exc
            return exc
        except NoPool as exc:
            if self._token_ungraduated(mint):
                upgraded = NoDexPoolYet("no DEX pool yet")
                self._missing[mint] = upgraded
                return upgraded
            self._missing[mint] = exc
            return exc
        self._chosen[mint] = chosen
        return None

    def _pool_pages(self, mint: str) -> dict[str, Any]:
        combined: list[Any] = []
        included: list[Any] = []
        for page in range(1, self._max_pool_pages + 1):
            payload = self._get_json(
                f"{BASE_URL}/networks/solana/tokens/{mint}/pools",
                {"include": "base_token,quote_token,dex", "page": str(page)},
            )
            rows = payload.get("data")
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list):
                break
            combined.extend(rows)
            extra = payload.get("included")
            if isinstance(extra, list):
                included.extend(extra)
            if len(rows) < POOL_PAGE_SIZE:
                break
        return {"data": combined, "included": included}

    def _token_ungraduated(self, mint: str) -> bool:
        try:
            payload = self._get_json(f"{BASE_URL}/networks/solana/tokens/{mint}", {})
        except SourceError as exc:
            if str(exc) == "GeckoTerminal returned HTTP 404":
                return False
            raise
        return token_is_ungraduated(payload)

    def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        backoff = max(self._pace.interval, 1.0)
        last_status = 0
        for attempt in range(_ATTEMPTS):
            self._pace.wait()
            try:
                response = self._client.get(url, params=params)
            except RateLimitExhausted:
                raise
            except httpx.TimeoutException:
                if attempt == _ATTEMPTS - 1:
                    raise SourceError("GeckoTerminal request timed out") from None
                self._sleep(backoff)
                backoff = min(backoff * 2, 120.0)
                continue
            except httpx.TransportError as exc:
                if isinstance(exc, _NO_RETRY) or attempt == _ATTEMPTS - 1:
                    raise SourceError("GeckoTerminal request failed") from None
                self._sleep(backoff)
                backoff = min(backoff * 2, 120.0)
                continue
            except httpx.HTTPError:
                raise SourceError("GeckoTerminal request failed") from None
            last_status = response.status_code
            if response.status_code == 404:
                return {"data": []}
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == _ATTEMPTS - 1:
                    if response.status_code == 429:
                        raise RateLimitExhausted("GeckoTerminal rate limit exhausted")
                    raise SourceError(f"GeckoTerminal returned HTTP {response.status_code}")
                self._sleep(backoff)
                backoff = min(backoff * 2, 120.0)
                continue
            if response.status_code != 200:
                raise SourceError(f"GeckoTerminal returned HTTP {response.status_code}")
            try:
                payload = response.json()
            except ValueError:
                raise SourceError("GeckoTerminal returned a body that is not JSON") from None
            if not isinstance(payload, dict):
                raise SourceError("GeckoTerminal returned a body that is not a JSON object")
            return payload
        raise SourceError(f"GeckoTerminal returned HTTP {last_status}")


def _new_client(budget: CallBudget) -> httpx.Client:
    return httpx.Client(
        timeout=30.0,
        headers={"Accept": "application/json", "User-Agent": "tradoortest-ohlcv"},
        event_hooks={"request": [budget.take]},
    )


def _reraise_rate_limit(exc: SourceError) -> None:
    if isinstance(exc, RateLimitExhausted):
        raise exc
    if "HTTP 429" in str(exc):
        raise RateLimitExhausted("GeckoTerminal rate limit exhausted") from None
