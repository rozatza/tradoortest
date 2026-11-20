"""Any-mint OHLCV: GeckoTerminal pool resolution, optional Birdeye fallback.

``fetch_bars(mint, timeframe, start, end)`` is the ROZ-69 seam. The mint
is checked before a request. The pool is chosen by liquidity (see
``app.ohlcv.pools``). Candles come from that pool. When GeckoTerminal has
no pool, or a pool but no candles, and ``BIRDEYE_API_KEY`` is set, Birdeye
``/defi/v3/ohlcv`` may serve the series. A pre-migration pump.fun mint is
``no DEX pool yet`` even when the key is set; Birdeye is not called. With
no key the fallback is skipped and is not an error.

Each series records which provider served it. The chosen pool, when there
is one, is available from ``pool_for``. In-memory pool and missing-pool
notes expire within 24 hours. The GeckoTerminal call cap and the Birdeye
call cap reset at the start of each ingest. The monthly compute-unit
ledger does not.
"""

from __future__ import annotations

import contextvars
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
# Same ceiling as the bar cache. A migrated mint is resolved again without
# a process restart, and the note is not kept longer than the bars.
MEMORY_TTL_SECONDS = 24 * 60 * 60
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


_budget_var: contextvars.ContextVar[CallBudget | None] = contextvars.ContextVar(
    "gt_call_budget", default=None
)


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
        self._call_limit = max_calls
        self._budget = CallBudget(max_calls)
        self._owns_client = client is None
        self._client = client if client is not None else _new_client(self._take)
        if client is not None:
            self._client.event_hooks.setdefault("request", []).append(self._take)
        self._sleep = sleep
        self._pace = _Pace(min_interval, sleep, monotonic)
        self._now = (lambda: int(time.time())) if now is None else now
        self._max_pool_pages = max_pool_pages
        self._pool_map: dict[str, str] = {}
        self._chosen: dict[str, tuple[int, ChosenPool]] = {}
        self._missing: dict[str, tuple[int, NoPool | NoDexPoolYet]] = {}
        self._served: dict[tuple[str, str], tuple[int, str]] = {}
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

    def begin_run(self) -> tuple[contextvars.Token[CallBudget | None], object]:
        """Fresh GeckoTerminal and Birdeye call caps for one ingest."""
        budget_token = _budget_var.set(CallBudget(self._call_limit))
        return budget_token, self._birdeye.begin_run()

    def end_run(self, token: tuple[contextvars.Token[CallBudget | None], object]) -> None:
        budget_token, birdeye_token = token
        self._birdeye.end_run(birdeye_token)
        _budget_var.reset(budget_token)

    def _take(self, _request: object = None) -> None:
        budget = _budget_var.get()
        if budget is None:
            budget = self._budget
        budget.take()

    def pool_provider(self) -> str:
        """Name of the source that chooses the pool.

        Candle fallback does not change this. Callers that store the row
        should use it instead of assuming a provider string.
        """
        name = getattr(self._gt, "name", None)
        if isinstance(name, str) and name != "" and len(name) <= 32 and "\x00" not in name:
            return name
        return "geckoterminal"

    def pool_for(self, mint: str) -> ChosenPool | None:
        found = self._recall(self._chosen, mint)
        if isinstance(found, ChosenPool):
            return found
        return None

    def resolve_mint(self, mint: str) -> ChosenPool:
        """Resolve one mint to the pool ``fetch_pages`` would read.

        This is the ingest path: a fresh call budget, then ``_resolve``,
        then ``pool_for``. It does not fetch candles and it does not write
        the bar cache. Callers persist the pool with ``SqliteBarStore``
        when they want the 24-hour row.
        """
        checked = require_mint(mint)
        token = self.begin_run()
        try:
            failure = self._resolve(checked)
            if isinstance(failure, (NoPool, NoDexPoolYet)):
                raise failure
            chosen = self.pool_for(checked)
            if chosen is None:
                raise NoPool(f"no pool for mint {checked}")
            return chosen
        finally:
            self.end_run(token)

    def served_provider(self, mint: str, timeframe: str) -> str:
        found = self._recall(self._served, (mint, timeframe))
        if isinstance(found, str) and found != "":
            return found
        return "geckoterminal"

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
        chosen = self.pool_for(mint)
        if chosen is None:
            raise NoPool(f"no pool for mint {mint}")
        self._pool_map[mint] = chosen.address
        try:
            pending = self._gt.fetch_pages(mint, timeframe, start, end)
            first = next(pending, None)
        except SourceError as exc:
            _reraise_rate_limit(exc)
            raise
        if first is None:
            return
        if first.bars or first.rejected or not first.provider_empty:
            self._mark(mint, timeframe, "geckoterminal")
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
                self._mark(mint, timeframe, "birdeye")
                yield from pages
                return
        self._mark(mint, timeframe, "geckoterminal")
        yield first

    def _fallback_or_raise(
        self,
        mint: str,
        timeframe: str,
        start: int,
        end: int,
        failure: NoPool | NoDexPoolYet,
    ) -> Iterator[FetchPage]:
        if isinstance(failure, NoDexPoolYet) or not self._birdeye.enabled:
            raise failure
        pages = self._birdeye_pages(mint, timeframe, start, end)
        if any(page.bars for page in pages):
            self._mark(mint, timeframe, "birdeye")
            yield from pages
            return
        raise failure

    def _birdeye_pages(self, mint: str, timeframe: str, start: int, end: int) -> list[FetchPage]:
        return list(self._birdeye.fetch_pages(mint, timeframe, start, end))

    def _resolve(self, mint: str) -> NoPool | NoDexPoolYet | None:
        if self._recall(self._chosen, mint) is not None:
            return None
        missing = self._recall(self._missing, mint)
        if isinstance(missing, (NoPool, NoDexPoolYet)):
            return missing
        try:
            payload = self._pool_pages(mint)
            chosen = choose_pool(payload, mint, resolved_at=int(self._now()))
        except NoDexPoolYet as exc:
            self._remember(self._missing, mint, exc)
            return exc
        except NoPool as exc:
            if self._token_ungraduated(mint):
                upgraded = NoDexPoolYet("no DEX pool yet")
                self._remember(self._missing, mint, upgraded)
                return upgraded
            self._remember(self._missing, mint, exc)
            return exc
        self._remember(self._chosen, mint, chosen)
        return None

    def _remember(self, table: dict[Any, tuple[int, Any]], key: Any, value: Any) -> None:
        table[key] = (int(self._now()), value)

    def _recall(self, table: dict[Any, tuple[int, Any]], key: Any) -> Any:
        found = table.get(key)
        if found is None:
            return None
        stamp, value = found
        if int(self._now()) - stamp >= MEMORY_TTL_SECONDS:
            table.pop(key, None)
            if table is self._chosen and isinstance(key, str):
                self._pool_map.pop(key, None)
            return None
        return value

    def _mark(self, mint: str, timeframe: str, provider: str) -> None:
        self._remember(self._served, (mint, timeframe), provider)

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


def _new_client(take: Callable[..., None]) -> httpx.Client:
    return httpx.Client(
        timeout=30.0,
        headers={"Accept": "application/json", "User-Agent": "tradoortest-ohlcv"},
        event_hooks={"request": [take]},
    )


def _reraise_rate_limit(exc: SourceError) -> None:
    if isinstance(exc, RateLimitExhausted):
        raise exc
    if "HTTP 429" in str(exc):
        raise RateLimitExhausted("GeckoTerminal rate limit exhausted") from None
