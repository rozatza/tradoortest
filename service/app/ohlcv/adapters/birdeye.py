"""Optional Birdeye free-Standard OHLCV fallback.

The key is read from the environment or passed to the constructor. It is
sent only as the ``X-API-KEY`` header. It is not written to a fixture, a
log line, an error message, or this object's repr. When the key is unset
the adapter is off: callers skip it, and calling it does not raise and
does not open a socket.

Free Standard is 1 request per second and 30,000 compute units per month.
This client does not go faster than 1 request per second, and it refuses
the next call when the configured budget cannot pay for it. The default
budget is 30,000. ``padding`` is always false, so empty candles are not
copied forward. ``outlier`` is false, so a candle is not dropped for us.
"""

from __future__ import annotations

import contextvars
import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.ohlcv.adapters.geckoterminal import _Pace
from app.ohlcv.bars import TIMEFRAME_SECONDS, Bar, BarRejection
from app.ohlcv.errors import ComputeBudgetExceeded, RateLimitExhausted, SourceError
from app.ohlcv.source import FetchPage, FetchResult

_UNSET = object()
DEFAULT_CU_BUDGET = 30_000
DEFAULT_MIN_INTERVAL = 1.0
_MAX_CANDLES = 5000
_ATTEMPTS = 5
_NO_RETRY = (httpx.UnsupportedProtocol, httpx.LocalProtocolError, httpx.ProxyError)
BASE_URL = "https://public-api.birdeye.so"
_call_box: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "birdeye_calls", default=None
)
_TYPE = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
}


def cu_for_candles(count: int) -> int:
    """Compute units for one ``/defi/v3/ohlcv`` call of this requested size.

    The published tiers are by response size. We reserve the tier for the
    number of candles we asked for, which is at least the response tier, so
    a successful call cannot cost more than we reserved.
    """
    if count <= 100:
        return 25
    if count <= 300:
        return 30
    if count <= 1000:
        return 40
    if count <= 2000:
        return 75
    return 100


class CuLedger:
    """Monthly compute-unit counter. The file, when set, holds no key."""

    def __init__(
        self,
        budget: int = DEFAULT_CU_BUDGET,
        *,
        path: Path | None = None,
        now: Callable[[], int] | None = None,
    ) -> None:
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise ValueError("Birdeye compute-unit budget must be a positive whole number")
        self.budget = budget
        self.path = path
        self._now = (lambda: int(time.time())) if now is None else now
        self._lock = threading.Lock()
        self._inflight = 0
        self.period, self.used = self._load()

    def reserve(self, candles: int) -> int:
        """Refuse before a call the budget cannot pay for, else return the cost."""
        with self._lock:
            return self._reserve_locked(candles)

    def _reserve_locked(self, candles: int) -> int:
        self._roll()
        cost = cu_for_candles(candles)
        held = self._inflight
        self.used += held
        if self.used + cost > self.budget:
            self.used -= held
            raise ComputeBudgetExceeded("Birdeye compute-unit budget exceeded")
        self.used -= held
        self._inflight += cost
        return cost

    def commit(self, cost: int) -> None:
        """Record a call that returned HTTP 200. A failed call is not billed."""
        with self._lock:
            self._commit_locked(cost)

    def _commit_locked(self, cost: int) -> None:
        self._inflight = max(0, self._inflight - cost)
        self._roll()
        self.used += cost
        self._write()

    def release(self, cost: int) -> None:
        """Drop a reservation when the call did not return HTTP 200."""
        with self._lock:
            self._inflight = max(0, self._inflight - cost)

    def _roll(self) -> None:
        period = _period(self._now())
        if period != self.period:
            self.period = period
            self.used = 0
            self._write()

    def _load(self) -> tuple[str, int]:
        period = _period(self._now())
        if self.path is None or not self.path.is_file():
            return period, 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise SourceError("Birdeye compute-unit ledger is unreadable") from None
        if not isinstance(raw, dict):
            raise SourceError("Birdeye compute-unit ledger is unreadable")
        if raw.get("period") != period:
            return period, 0
        used = raw.get("used")
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            raise SourceError("Birdeye compute-unit ledger is unreadable")
        return period, used

    def _write(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"period": self.period, "used": self.used}) + "\n"
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".birdeye-cu-", suffix=".tmp")
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def _period(now: int) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m")


class BirdeyeSource:
    """``OhlcvSource`` for ``GET /defi/v3/ohlcv``. Off when no key is set."""

    name = "birdeye"

    def __init__(
        self,
        api_key: str | None | object = _UNSET,
        *,
        client: httpx.Client | None = None,
        budget: int = DEFAULT_CU_BUDGET,
        ledger_path: Path | None = None,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_calls: int = 60,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], int] | None = None,
    ) -> None:
        if isinstance(min_interval, bool) or not isinstance(min_interval, (int, float)):
            raise ValueError("Birdeye pace must be a number of seconds")
        if min_interval < 0:
            raise ValueError("Birdeye pace must be a number of seconds")
        if min_interval == 0 or not math.isfinite(min_interval):
            raise ValueError("Birdeye pace must be a positive number of seconds")
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
            raise ValueError("Birdeye call cap must be a positive whole number")
        key = _key(api_key)
        self.enabled = key != ""
        self._key = key
        self.min_interval = float(min_interval)
        self.max_calls = max_calls
        self._calls = 0
        self._call_lock = threading.Lock()
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep
        self._pace = _Pace(float(min_interval), sleep, monotonic)
        self.ledger = CuLedger(budget, path=ledger_path, now=now)

    def __repr__(self) -> str:
        return f"BirdeyeSource(enabled={self.enabled})"

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult:
        bars: list[Bar] = []
        rejected: list[BarRejection] = []
        for page in self.fetch_pages(mint, timeframe, start, end):
            bars.extend(page.bars)
            rejected.extend(page.rejected)
        bars.sort(key=lambda bar: bar.open_time)
        return FetchResult(bars, rejected)

    def fetch_pages(self, mint: str, timeframe: str, start: int, end: int) -> Iterator[FetchPage]:
        """Yield candle pages. A disabled source yields nothing and does not call out."""
        if not self.enabled:
            return
        if timeframe not in _TYPE:
            raise SourceError(f"timeframe {timeframe!r} is not supported")
        if start < 0 or end < start:
            raise SourceError("bar window is invalid")
        if start == end:
            return
        step = TIMEFRAME_SECONDS[timeframe]
        span = _MAX_CANDLES * step
        hi = end
        while hi > start:
            lo = max(start, hi - span)
            yield self._page(mint, timeframe, lo, hi)
            hi = lo

    def _page(self, mint: str, timeframe: str, start: int, end: int) -> FetchPage:
        step = TIMEFRAME_SECONDS[timeframe]
        candles = max(1, (end - start + step - 1) // step)
        candles = min(candles, _MAX_CANDLES)
        cost = self.ledger.reserve(candles)
        try:
            payload = self._get_json(mint, timeframe, start, end)
        except BaseException:
            self.ledger.release(cost)
            raise
        self.ledger.commit(cost)
        items = _items(payload)
        if not items:
            return FetchPage([], [], start, end, True)
        bars, rejected = _parse_items(items, mint, timeframe, start, end)
        return FetchPage(bars, rejected, start, end, False)

    def _get_json(self, mint: str, timeframe: str, start: int, end: int) -> dict[str, Any]:
        params = {
            "address": mint,
            "type": _TYPE[timeframe],
            "time_from": str(start),
            "time_to": str(end - 1),
            "currency": "usd",
            "mode": "range",
            "padding": "false",
            "outlier": "false",
        }
        url = f"{BASE_URL}/defi/v3/ohlcv"
        backoff = max(self._pace.interval, 1.0)
        last_status = 0
        for attempt in range(_ATTEMPTS):
            self._take_call()
            self._pace.wait()
            try:
                response = self._http().get(url, params=params, headers=self._headers())
            except httpx.TimeoutException as exc:
                if attempt == _ATTEMPTS - 1:
                    raise SourceError("Birdeye request timed out") from None
                self._sleep(backoff)
                backoff = min(backoff * 2, 120.0)
                del exc
                continue
            except httpx.TransportError as exc:
                if isinstance(exc, _NO_RETRY) or attempt == _ATTEMPTS - 1:
                    raise SourceError("Birdeye request failed") from None
                self._sleep(backoff)
                backoff = min(backoff * 2, 120.0)
                continue
            except httpx.HTTPError:
                raise SourceError("Birdeye request failed") from None
            last_status = response.status_code
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == _ATTEMPTS - 1:
                    if response.status_code == 429:
                        raise RateLimitExhausted("Birdeye rate limit exhausted")
                    raise SourceError(f"Birdeye returned HTTP {response.status_code}")
                self._sleep(backoff)
                backoff = min(backoff * 2, 120.0)
                continue
            if response.status_code != 200:
                raise SourceError(f"Birdeye returned HTTP {response.status_code}")
            try:
                payload = response.json()
            except ValueError:
                raise SourceError("Birdeye returned a body that is not JSON") from None
            if not isinstance(payload, dict):
                raise SourceError("Birdeye returned a body that is not a JSON object")
            return payload
        raise SourceError(f"Birdeye returned HTTP {last_status}")

    def begin_run(self) -> contextvars.Token[list[int] | None]:
        """Start a fresh call cap for this ingest. The monthly ledger is not reset."""
        return _call_box.set([0])

    def end_run(self, token: contextvars.Token[list[int] | None]) -> None:
        _call_box.reset(token)

    def _take_call(self) -> None:
        box = _call_box.get()
        if box is not None:
            if box[0] >= self.max_calls:
                raise RateLimitExhausted("Birdeye request cap exhausted")
            box[0] += 1
            return
        with self._call_lock:
            if self._calls >= self.max_calls:
                raise RateLimitExhausted("Birdeye request cap exhausted")
            self._calls += 1

    def _headers(self) -> dict[str, str]:
        # Per request, so an injected test client still sends the key, and
        # the client object itself does not carry it in its default headers.
        return {
            "Accept": "application/json",
            "X-API-KEY": self._key,
            "x-chain": "solana",
        }

    def _http(self) -> httpx.Client:
        if self._client is None:
            # The key lives on this client and nowhere else. Callers do not
            # log the client or its headers.
            self._client = httpx.Client(
                timeout=30.0,
                headers={"Accept": "application/json", "User-Agent": "tradoortest-ohlcv"},
            )
            self._owns_client = True
        return self._client


def _key(api_key: str | None | object) -> str:
    if api_key is _UNSET:
        raw = os.environ.get("BIRDEYE_API_KEY")
    elif api_key is None:
        raw = None
    elif isinstance(api_key, str):
        raw = api_key
    else:
        raw = None
    if raw is None:
        return ""
    text = raw.strip()
    if text == "" or any(char.isspace() for char in text):
        return ""
    return text


def _items(payload: dict[str, Any]) -> list[Any]:
    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if items is None and isinstance(data, list):
        items = data
    if not isinstance(items, list):
        raise SourceError("Birdeye OHLCV payload has no items")
    return items


def _parse_items(
    items: list[Any], mint: str, timeframe: str, start: int, end: int
) -> tuple[list[Bar], list[BarRejection]]:
    """Keep candles inside the window. A missing timestamp is not invented."""
    found: dict[int, Bar] = {}
    rejected: list[BarRejection] = []
    conflicts: set[int] = set()
    for item in items:
        bar, rejection = _parse_item(item, mint, timeframe)
        if rejection is not None:
            stamp = rejection.open_time
            if stamp is None or start <= stamp < end:
                rejected.append(rejection)
            continue
        assert bar is not None
        if bar.open_time < start or bar.open_time >= end:
            continue
        previous = found.get(bar.open_time)
        if previous is None:
            found[bar.open_time] = bar
        elif previous != bar:
            conflicts.add(bar.open_time)
    for stamp in conflicts:
        bar = found.pop(stamp, None)
        if bar is None:
            continue
        rejected.append(
            BarRejection(
                mint,
                timeframe,
                stamp,
                ("conflicting candles for the same open_time",),
            )
        )
    ordered = [found[key] for key in sorted(found)]
    rejected.sort(key=lambda item: item.open_time if isinstance(item.open_time, int) else 0)
    return ordered, rejected


def _parse_item(
    item: object, mint: str, timeframe: str
) -> tuple[Bar | None, BarRejection | None]:
    if not isinstance(item, dict):
        return None, BarRejection(mint, timeframe, None, ("candle is not an object",))
    stamp, stamp_ok = _integer_second(item.get("unix_time"))
    volume = item.get("v_usd", item.get("v"))
    numbers, bad = _numbers(
        {
            "open": item.get("o"),
            "high": item.get("h"),
            "low": item.get("l"),
            "close": item.get("c"),
            "volume": volume,
        }
    )
    if not stamp_ok or bad:
        label = f"candle at {stamp}" if stamp_ok and stamp is not None else "candle"
        reasons: list[str] = []
        if not stamp_ok:
            reasons.append(f"{label} open_time is not an integer second")
        reasons.extend(f"{label} {name} is not a number" for name in bad)
        return None, BarRejection(mint, timeframe, stamp if stamp_ok else None, tuple(reasons))
    assert stamp is not None
    return (
        Bar(
            mint=mint,
            timeframe=timeframe,
            open_time=stamp,
            open=numbers["open"],
            high=numbers["high"],
            low=numbers["low"],
            close=numbers["close"],
            volume=numbers["volume"],
        ),
        None,
    )


def _integer_second(value: object) -> tuple[int | None, bool]:
    if isinstance(value, bool) or isinstance(value, float):
        return None, False
    if isinstance(value, int):
        return value, True
    if isinstance(value, str) and value.isdecimal():
        return int(value), True
    return None, False


def _numbers(values: dict[str, object]) -> tuple[dict[str, float], list[str]]:
    parsed: dict[str, float] = {}
    bad: list[str] = []
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            bad.append(name)
            continue
        parsed[name] = float(value)
    return parsed, bad
