"""GeckoTerminal keyless pool OHLCV. On-chain DEX, no API key.

The public API is ``https://api.geckoterminal.com/api/v2``. CoinGecko
documents about 5 calls per minute for the keyless pool, 1000 candles per
call, and about six months of history on the free tier. This client spaces
calls 12 seconds apart and never sends ``include_empty_intervals`` (that
flag copies the previous close forward). Empty minutes stay missing.
HTTP 429, HTTP 5xx, timeouts, and transport errors (connect, read, write,
and remote protocol) retry with a bounded backoff. After five attempts the
call is a ``SourceError``. Other 4xx responses are not retried. A bad
proxy, an unsupported URL scheme, and a local protocol error are not
retried either. The public API only serves about 180 days; a 401 is
reported as that limit.

Every timeframe is a native aggregate: minute 1/5/15, hour 1/4, and day.
Higher timeframes are not built from 1m bars.

A mint that is not in ``POOLS`` raises ``UnknownMint``. A later source can
accept any mint through the same ``fetch_bars`` method.

Pool addresses are public on-chain identifiers. No request header carries
a credential, and errors report only the HTTP status.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from app.ohlcv.bars import TIMEFRAME_SECONDS, Bar, BarRejection, validate_bar
from app.ohlcv.errors import SourceError, UnknownMint
from app.ohlcv.registry import BONK_MINT, JTO_MINT, JUP_MINT, SOL_MINT, WIF_MINT
from app.ohlcv.source import FetchPage, FetchResult

# path segment, aggregate. GeckoTerminal native candles, not a 1m resample.
_NATIVE: dict[str, tuple[str, int]] = {
    "1m": ("minute", 1),
    "5m": ("minute", 5),
    "15m": ("minute", 15),
    "1h": ("hour", 1),
    "4h": ("hour", 4),
    "1d": ("day", 1),
}

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
# A seed older than this is not a prior close. Using it would mark every
# quiet minute back to that candle as fetched and store a synthetic bar for
# each one. A token idle for 10 days, requested for 1 day, used to store
# about 14,404 of those rows. One day of lookback still covers a quiet open
# and a bad candle that hides the real prior close on the last page.
MAX_SEED_LOOKBACK_SECONDS = 24 * 60 * 60
# Misconfiguration, not a blip. Retrying cannot make the URL or the proxy valid.
_NO_RETRY = (httpx.UnsupportedProtocol, httpx.LocalProtocolError, httpx.ProxyError)
# Live probe on 2026-11-19: a window of 181 days returned 200, and 185 days
# returned 401. ``--days`` above this fails before any request.
PUBLIC_HISTORY_DAYS = 180
PUBLIC_HISTORY_MESSAGE = (
    "the public GeckoTerminal API only serves about 180 days of history; "
    "--days must be 180 or less"
)


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
        bars: list[Bar] = []
        rejected: list[BarRejection] = []
        for page in self.fetch_pages(mint, timeframe, start, end):
            bars.extend(page.bars)
            rejected.extend(page.rejected)
        bars.sort(key=lambda bar: bar.open_time)
        return FetchResult(bars, rejected)

    def fetch_pages(self, mint: str, timeframe: str, start: int, end: int) -> Iterator[FetchPage]:
        """Yield one HTTP page at a time. The caller commits each page.

        A later page that fails does not un-yield the pages already returned.
        Coverage is the span this response answered, not only the candles
        that contained a trade.
        """
        if timeframe not in _NATIVE:
            raise SourceError(f"timeframe {timeframe!r} is not supported")
        if start < 0 or end < start:
            raise SourceError("bar window is invalid")
        yield from self._pages(mint, timeframe, start, end)

    def _pages(self, mint: str, timeframe: str, start: int, end: int) -> Iterator[FetchPage]:
        pool = self._pools.get(mint)
        if pool is None:
            raise UnknownMint(
                f"no GeckoTerminal pool is configured for mint {mint}. "
                "A later source can accept this mint in fetch_bars without changing callers."
            )
        if start == end:
            return
        path, aggregate = _NATIVE[timeframe]
        step = TIMEFRAME_SECONDS[timeframe]
        before = end - 1
        page_hi = end
        span = end - start
        lookback_pages = math.ceil(MAX_SEED_LOOKBACK_SECONDS / step / self._page_limit) + 1
        page_budget = max(2, math.ceil(span / step / self._page_limit) + 3) + lookback_pages
        pages = 0
        # The page that first reaches before the window stays unyielded while
        # older pages are read for a seed. Committing it first would store a
        # span with no prior close, and a seed found later could not widen it.
        held: FetchPage | None = None
        carried: list[BarRejection] = []
        while pages < page_budget:
            payload = self._get_json(pool, mint, before, path, aggregate)
            candles = _ohlcv_list(payload)
            pages += 1
            if not candles:
                # No candles at all. This is provider-missing, not a quiet market.
                if held is not None:
                    yield held
                    return
                yield FetchPage([], [], start, page_hi, True)
                return
            parsed = _parse_page(candles, mint, timeframe)
            if parsed.oldest is None:
                raise SourceError("GeckoTerminal page has no candle timestamps")
            if held is None:
                # A short page is the end of history. Coverage starts at the
                # window, so the minutes before the first candle are a gap
                # rather than an unfetched range. A full page that is still
                # inside the window covers only its own candles.
                reached = parsed.oldest <= start or len(candles) < self._page_limit
                cover_lo = start if reached else parsed.oldest
                page = _page_in_range(parsed, cover_lo, page_hi)
            else:
                page = held
            if parsed.oldest < start:
                seed, older_rejected = _prior_close(parsed, start)
                if seed is not None and start - seed.open_time > MAX_SEED_LOOKBACK_SECONDS:
                    # Older than the cap. A further page is older still.
                    seed = None
                    older_rejected = []
                    stop = True
                else:
                    stop = False
                if seed is not None:
                    between = [
                        *carried,
                        *older_rejected,
                    ]
                    between = [
                        item
                        for item in between
                        if isinstance(item.open_time, int)
                        and not isinstance(item.open_time, bool)
                        and seed.open_time < item.open_time < start
                    ]
                    between.sort(
                        key=lambda item: item.open_time if isinstance(item.open_time, int) else 0
                    )
                    yield FetchPage(
                        page.bars,
                        [*page.rejected, *between],
                        page.start,
                        page.end,
                        False,
                        seed,
                    )
                    return
                if not stop:
                    carried.extend(older_rejected)
                page_is_short = len(candles) < self._page_limit
                beyond = parsed.oldest <= start - MAX_SEED_LOOKBACK_SECONDS
                if stop or page_is_short or beyond:
                    yield page
                    return
                held = page
            else:
                yield page
                if parsed.oldest <= start or len(candles) < self._page_limit:
                    return
            nxt = parsed.oldest - 1
            if nxt >= before:
                raise SourceError("GeckoTerminal pagination did not move backward")
            before = nxt
            if held is None:
                page_hi = parsed.oldest
        raise SourceError("GeckoTerminal pagination stopped before the window was covered")

    def _get_json(
        self, pool: str, mint: str, before: int, path: str, aggregate: int
    ) -> dict[str, Any]:
        params = {
            "aggregate": str(aggregate),
            "limit": str(self._page_limit),
            "currency": "usd",
            "token": mint,
            "before_timestamp": str(before),
        }
        url = f"{BASE_URL}/networks/solana/pools/{pool}/ohlcv/{path}"
        backoff = max(self._pace.interval, 1.0)
        last_status = 0
        for attempt in range(_ATTEMPTS):
            self._pace.wait()
            try:
                response = self._http().get(url, params=params)
            except httpx.TimeoutException as exc:
                if attempt == _ATTEMPTS - 1:
                    raise SourceError("GeckoTerminal request timed out") from exc
                self._sleep(backoff)
                backoff = min(backoff * 2, 120.0)
                continue
            except httpx.TransportError as exc:
                # ConnectError, ReadError, WriteError, RemoteProtocolError.
                # One blip used to abort a 90-day run. Same bound as timeouts.
                # UnsupportedProtocol, LocalProtocolError, and ProxyError are
                # config errors and fail on the first attempt.
                if isinstance(exc, _NO_RETRY):
                    raise SourceError("GeckoTerminal request failed") from exc
                if attempt == _ATTEMPTS - 1:
                    raise SourceError("GeckoTerminal request failed") from exc
                self._sleep(backoff)
                backoff = min(backoff * 2, 120.0)
                continue
            except httpx.HTTPError as exc:
                raise SourceError("GeckoTerminal request failed") from exc
            last_status = response.status_code
            if response.status_code == 401:
                raise SourceError(
                    "GeckoTerminal public API only serves about 180 days of history"
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == _ATTEMPTS - 1:
                    raise SourceError(f"GeckoTerminal returned HTTP {response.status_code}")
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


@dataclass
class _ParsedPage:
    bars: dict[int, Bar]
    rejected: list[BarRejection]
    conflicts: set[int]
    oldest: int | None


def _parse_page(candles: list[Any], mint: str, timeframe: str) -> _ParsedPage:
    bars: dict[int, Bar] = {}
    rejected: list[BarRejection] = []
    conflicts: set[int] = set()
    oldest: int | None = None
    for index, candle in enumerate(candles):
        bar, rejection, stamp = _parse_candle(candle, mint, index, timeframe)
        if stamp is not None:
            oldest = stamp if oldest is None else min(oldest, stamp)
        if rejection is not None:
            rejected.append(rejection)
            continue
        assert bar is not None
        previous = bars.get(bar.open_time)
        if previous is None:
            bars[bar.open_time] = bar
        elif previous != bar:
            conflicts.add(bar.open_time)
    return _ParsedPage(bars, rejected, conflicts, oldest)


def _page_in_range(parsed: _ParsedPage, start: int, end: int) -> FetchPage:
    """Keep candles inside ``[start, end)`` and turn conflicts into rejections."""
    rejected = [
        item
        for item in parsed.rejected
        if item.open_time is None or start <= item.open_time < end
    ]
    kept: dict[int, Bar] = {}
    for open_time, bar in parsed.bars.items():
        if open_time < start or open_time >= end:
            continue
        if open_time in parsed.conflicts:
            rejected.append(
                BarRejection(
                    bar.mint,
                    bar.timeframe,
                    open_time,
                    ("conflicting candles for the same open_time",),
                )
            )
            continue
        kept[open_time] = bar
    ordered = [kept[key] for key in sorted(kept)]
    return FetchPage(ordered, rejected, start, end)


def _prior_close(parsed: _ParsedPage, start: int) -> tuple[Bar | None, list[BarRejection]]:
    """Latest older candle that can seed a close, and the bad candles after it.

    Every older candle is considered. A conflicting timestamp, a candle that
    did not parse, and a candle that fails ``validate_bar`` are skipped.
    Skipped candles that sit strictly after the chosen seed are returned so
    the fetched span keeps them as rejections instead of synthetic bars.
    With no usable seed, the skipped candles inside the lookback cap are
    still returned. A later page can use them once it finds a seed. A seed
    older than ``MAX_SEED_LOOKBACK_SECONDS`` is not used.
    """
    skipped: list[BarRejection] = []
    for item in parsed.rejected:
        stamp = item.open_time
        if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp >= start:
            continue
        skipped.append(item)
    candidates: list[Bar] = []
    for open_time, bar in parsed.bars.items():
        if open_time >= start:
            continue
        if open_time in parsed.conflicts:
            skipped.append(
                BarRejection(
                    bar.mint,
                    bar.timeframe,
                    open_time,
                    ("conflicting candles for the same open_time",),
                )
            )
            continue
        reasons = validate_bar(bar)
        if reasons:
            skipped.append(BarRejection(bar.mint, bar.timeframe, open_time, tuple(reasons)))
            continue
        candidates.append(bar)
    if not candidates:
        # No usable seed on this page. The bad candles stay available so a
        # later page can reject the minutes between its seed and the window.
        # A candle older than the lookback cap is not part of that span.
        kept = [
            item
            for item in skipped
            if isinstance(item.open_time, int)
            and not isinstance(item.open_time, bool)
            and 0 <= start - item.open_time <= MAX_SEED_LOOKBACK_SECONDS
        ]
        return None, kept
    seed = max(candidates, key=lambda bar: bar.open_time)
    between = [
        item
        for item in skipped
        if isinstance(item.open_time, int)
        and not isinstance(item.open_time, bool)
        and seed.open_time < item.open_time < start
    ]
    between.sort(key=lambda item: item.open_time if isinstance(item.open_time, int) else 0)
    return seed, between


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
    _index: int,
    timeframe: str = "1m",
) -> tuple[Bar | None, BarRejection | None, int | None]:
    """Parse one candle. Extra elements and any sibling metadata are ignored.

    The reason text names the candle by its timestamp when one was parsed.
    It does not use ``_index``. That index is the position on this page, so
    the same candle would be stored under a different reason after a
    different request order.
    """
    if not isinstance(candle, list) or len(candle) < 6:
        rejection = BarRejection(mint, timeframe, None, ("candle does not have 6 values",))
        return None, rejection, None
    stamp, stamp_ok = _integer_second(candle[0])
    numbers, bad = _numbers(candle[1:6])
    if not stamp_ok or bad:
        label = f"candle at {stamp}" if stamp_ok and stamp is not None else "candle"
        reasons: list[str] = []
        if not stamp_ok:
            reasons.append(f"{label} open_time is not an integer second")
        reasons.extend(f"{label} {name} is not a number" for name in bad)
        return None, BarRejection(mint, timeframe, stamp if stamp_ok else None, tuple(reasons)), (
            stamp if stamp_ok else None
        )
    assert stamp is not None
    bar = Bar(
        mint=mint,
        timeframe=timeframe,
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
