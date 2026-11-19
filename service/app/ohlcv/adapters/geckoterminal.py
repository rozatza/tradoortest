"""GeckoTerminal keyless pool OHLCV. On-chain DEX, no API key.

The public API is ``https://api.geckoterminal.com/api/v2``. CoinGecko
documents about 5 calls per minute for the keyless pool, 1000 candles per
call, and about six months of history on the free tier. This client spaces
calls 12 seconds apart and never sends ``include_empty_intervals`` (that
flag copies the previous close forward). Empty minutes stay missing.

1m is fetched natively. Higher timeframes are resampled from those 1m bars.
A mint that is not in ``POOLS`` raises ``UnknownMint``. A later source can
accept any mint through the same ``fetch_bars`` method.

Pool addresses are public on-chain identifiers. No request header carries
a credential, and errors report only the HTTP status.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.ohlcv.bars import (
    TIMEFRAME_SECONDS,
    Bar,
    BarRejection,
    validate_bar,
)
from app.ohlcv.errors import SourceError, UnknownMint
from app.ohlcv.registry import BONK_MINT, JTO_MINT, JUP_MINT, SOL_MINT, WIF_MINT
from app.ohlcv.resample import resample
from app.ohlcv.source import FetchResult

BASE_URL = "https://api.geckoterminal.com/api/v2"
# Chosen 2026-11-19. Each pool was created more than 90 days earlier and
# was the deepest SOL or USDC pair for that mint among pools that old.
# A 1m candle at or within a few minutes of 2026-08-21 was returned for
# each, with currency=usd and token=<mint>. See docs/ohlcv.md.
POOLS: dict[str, str] = {
    SOL_MINT: "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE",  # Orca SOL/USDC
    JUP_MINT: "C8Gr6AUuq9hEdSYJzoEpNcdjpojPZwqG5MtQbeouNNwg",  # Meteora JUP/SOL
    JTO_MINT: "2UhFnySoJi6c89aydGAGS7ZRemo2dbkFRhvSJqDX4gHJ",  # Orca JTO/SOL
    BONK_MINT: "5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9",  # Orca BONK/SOL
    WIF_MINT: "EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx",  # Raydium WIF/SOL
}

_PAGE_LIMIT = 1000
_ATTEMPTS = 5


class _Pace:
    """Minimum gap between HTTP calls. The sleeper is injectable for tests."""

    def __init__(
        self,
        interval: float,
        sleep: Callable[[float], None],
        monotonic: Callable[[], float],
    ) -> None:
        self.interval = interval
        self._sleep = sleep
        self._monotonic = monotonic
        self._next = 0.0

    def wait(self) -> None:
        now = self._monotonic()
        if now < self._next:
            self._sleep(self._next - now)
            now = self._monotonic()
        self._next = now + self.interval


class GeckoTerminalSource:
    """``OhlcvSource`` backed by the keyless GeckoTerminal pool endpoint."""

    name = "geckoterminal"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        pools: dict[str, str] | None = None,
        page_limit: int = _PAGE_LIMIT,
        min_interval: float = 12.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if page_limit < 1 or page_limit > _PAGE_LIMIT:
            raise ValueError("page_limit must be from 1 to 1000")
        self._client = client
        self._owns_client = client is None
        self._pools = POOLS if pools is None else pools
        self._page_limit = page_limit
        self._sleep = sleep
        self._pace = _Pace(min_interval, sleep, monotonic)

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    def __enter__(self) -> GeckoTerminalSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult:
        if timeframe not in TIMEFRAME_SECONDS:
            raise SourceError(f"timeframe {timeframe!r} is not supported")
        if start < 0 or end < start:
            raise SourceError("bar window is invalid")
        if timeframe == "1m":
            return self._fetch_minutes(mint, start, end)
        minutes = self._fetch_minutes(mint, start, end)
        rejected = list(minutes.rejected)
        good: list[Bar] = []
        for bar in minutes.bars:
            reasons = validate_bar(bar)
            if reasons:
                rejected.append(
                    BarRejection(bar.mint, bar.timeframe, bar.open_time, tuple(reasons))
                )
            else:
                good.append(bar)
        step = TIMEFRAME_SECONDS[timeframe]
        resampled = [
            bar
            for bar in resample(good, timeframe)
            if start <= bar.open_time and bar.open_time + step <= end
        ]
        return FetchResult(resampled, rejected)

    def _fetch_minutes(self, mint: str, start: int, end: int) -> FetchResult:
        pool = self._pools.get(mint)
        if pool is None:
            raise UnknownMint(
                f"no GeckoTerminal pool is configured for mint {mint}. "
                "A later source can accept this mint in fetch_bars without changing callers."
            )
        if start == end:
            return FetchResult([], [])
        by_time: dict[int, Bar] = {}
        rejected: list[BarRejection] = []
        conflicts: set[int] = set()
        before = end - 1
        span = end - start
        page_budget = max(2, math.ceil(span / 60 / self._page_limit) + 3)
        pages = 0
        while pages < page_budget:
            payload = self._get_json(pool, mint, before)
            candles = _ohlcv_list(payload)
            pages += 1
            if not candles:
                break
            oldest: int | None = None
            for index, candle in enumerate(candles):
                bar, rejection, stamp = _parse_candle(candle, mint, index)
                if stamp is not None:
                    oldest = stamp if oldest is None else min(oldest, stamp)
                if rejection is not None:
                    rejected.append(rejection)
                    continue
                assert bar is not None
                if bar.open_time < start or bar.open_time >= end:
                    continue
                previous = by_time.get(bar.open_time)
                if previous is None:
                    by_time[bar.open_time] = bar
                elif previous != bar:
                    conflicts.add(bar.open_time)
            if oldest is None:
                raise SourceError("GeckoTerminal page has no candle timestamps")
            if oldest <= start or len(candles) < self._page_limit:
                break
            nxt = oldest - 1
            if nxt >= before:
                raise SourceError("GeckoTerminal pagination did not move backward")
            before = nxt
        else:
            raise SourceError("GeckoTerminal pagination stopped before the window was covered")
        for open_time in conflicts:
            by_time.pop(open_time, None)
            rejected.append(
                BarRejection(
                    mint,
                    "1m",
                    open_time,
                    ("conflicting candles for the same open_time",),
                )
            )
        bars = [by_time[key] for key in sorted(by_time)]
        return FetchResult(bars, rejected)

    def _get_json(self, pool: str, mint: str, before: int) -> dict[str, Any]:
        params = {
            "aggregate": "1",
            "limit": str(self._page_limit),
            "currency": "usd",
            "token": mint,
            "before_timestamp": str(before),
        }
        url = f"{BASE_URL}/networks/solana/pools/{pool}/ohlcv/minute"
        backoff = max(self._pace.interval, 1.0)
        last_status = 0
        for _attempt in range(_ATTEMPTS):
            self._pace.wait()
            response = self._http().get(url, params=params)
            last_status = response.status_code
            if response.status_code == 429:
                self._sleep(backoff)
                backoff = min(backoff * 2, 120.0)
                continue
            if response.status_code != 200:
                raise SourceError(f"GeckoTerminal returned HTTP {response.status_code}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise SourceError("GeckoTerminal returned a body that is not JSON") from exc
            if not isinstance(payload, dict):
                raise SourceError("GeckoTerminal returned a body that is not a JSON object")
            return payload
        raise SourceError(f"GeckoTerminal returned HTTP {last_status}")

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=30.0,
                headers={"Accept": "application/json", "User-Agent": "tradoortest-ohlcv"},
            )
            self._owns_client = True
        return self._client


def _ohlcv_list(payload: dict[str, Any]) -> list[Any]:
    data = payload.get("data")
    attributes = data.get("attributes") if isinstance(data, dict) else None
    candles = attributes.get("ohlcv_list") if isinstance(attributes, dict) else None
    if not isinstance(candles, list):
        raise SourceError("GeckoTerminal OHLCV payload has no ohlcv_list")
    return candles


def _parse_candle(
    candle: object,
    mint: str,
    index: int,
) -> tuple[Bar | None, BarRejection | None, int | None]:
    """Parse one candle. Extra elements and any sibling metadata are ignored."""
    if not isinstance(candle, list) or len(candle) < 6:
        rejection = BarRejection(mint, "1m", None, (f"candle {index} does not have 6 values",))
        return None, rejection, None
    stamp, stamp_ok = _integer_second(candle[0])
    numbers, bad = _numbers(candle[1:6])
    if not stamp_ok or bad:
        reasons: list[str] = []
        if not stamp_ok:
            reasons.append(f"candle {index} open_time is not an integer second")
        reasons.extend(f"candle {index} {name} is not a number" for name in bad)
        return None, BarRejection(mint, "1m", stamp if stamp_ok else None, tuple(reasons)), (
            stamp if stamp_ok else None
        )
    assert stamp is not None
    bar = Bar(
        mint=mint,
        timeframe="1m",
        open_time=stamp,
        open=numbers["open"],
        high=numbers["high"],
        low=numbers["low"],
        close=numbers["close"],
        volume=numbers["volume"],
    )
    return bar, None, stamp


def _integer_second(value: object) -> tuple[int | None, bool]:
    if isinstance(value, bool) or isinstance(value, float):
        return None, False
    if isinstance(value, int):
        return value, True
    if isinstance(value, str) and value.isdecimal():
        return int(value), True
    return None, False


def _numbers(values: list[object]) -> tuple[dict[str, float], list[str]]:
    names = ("open", "high", "low", "close", "volume")
    parsed: dict[str, float] = {}
    bad: list[str] = []
    for name, value in zip(names, values, strict=True):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            bad.append(name)
            continue
        parsed[name] = float(value)
    return parsed, bad
