"""Near-real-time OHLCV for SOL and the native majors.

GeckoTerminal is the primary source. Birdeye runs only after that source
fails, and only when it has an API key. With no key the fallback is not
called and the missing key is not an error by itself.

A bar is closed only when ``open_time + bar_seconds + CLOSE_GRACE_SECONDS
<= now``. The grace keeps a clock that is fast by about 40 seconds from
admitting the forming bar. Synthetic bars are dropped. Nothing here copies
a close forward or builds a gap bar. Closed real bars and the raw response
bodies go into the bar cache, which purges them on the same 24-hour clock.
A bar a strategy has already decided is not overwritten.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Protocol

from app.auth import LOCAL_USER_ID
from app.ohlcv.bars import TIMEFRAME_SECONDS, Bar, validate_bar
from app.ohlcv.errors import SourceError
from app.ohlcv.source import FetchResult
from app.ohlcv.store import SqliteBarStore

logger = logging.getLogger("app.live")

# A local clock this far ahead of the exchange must not treat a still-forming
# bar as closed. 60s covers a skew of about 40s with a small margin. The same
# margin is applied in ``bar_is_closed`` and ``last_closed_open``.
CLOSE_GRACE_SECONDS = 60


class _Source(Protocol):
    name: str

    def fetch_bars(self, mint: str, timeframe: str, start: int, end: int) -> FetchResult: ...


def bar_is_closed(
    open_time: int,
    bar_seconds: int,
    now: int,
    *,
    grace: int = CLOSE_GRACE_SECONDS,
) -> bool:
    """True once the bucket has finished and the close grace has elapsed.

    ``grace`` is ``CLOSE_GRACE_SECONDS``. A clock that is fast by less than
    that does not admit the forming bar.
    """
    if grace < 0:
        grace = 0
    return open_time + bar_seconds + grace <= now


def last_closed_open(
    now: int, bar_seconds: int, *, grace: int = CLOSE_GRACE_SECONDS
) -> int | None:
    """Open time of the newest bar that is already closed, or None."""
    if bar_seconds < 1 or grace < 0 or now < bar_seconds + grace:
        return None
    latest = now - bar_seconds - grace
    closed = latest - (latest % bar_seconds)
    if closed < 0:
        return None
    return closed


def closed_real_bars(bars: Sequence[Bar], timeframe: str, now: int) -> list[Bar]:
    """Real, valid, closed bars. Drops the forming bar, synthetics, and conflicts.

    Two different candles for one open time are both dropped. Two equal
    candles count once. Order in the input does not matter.
    """
    step = TIMEFRAME_SECONDS.get(timeframe)
    if step is None:
        return []
    grouped: dict[int, list[Bar]] = {}
    for bar in bars:
        if bar.timeframe != timeframe or bar.synthetic:
            continue
        if validate_bar(bar):
            continue
        if not bar_is_closed(bar.open_time, step, now):
            continue
        grouped.setdefault(bar.open_time, []).append(bar)
    kept: list[Bar] = []
    for open_time in sorted(grouped):
        items = grouped[open_time]
        first = items[0]
        if all(item == first for item in items):
            kept.append(first)
    return kept


class LiveFeed:
    """Fetch one window and store the closed real bars."""

    def __init__(
        self,
        store: SqliteBarStore,
        primary: _Source,
        fallback: _Source | None = None,
        *,
        now: Callable[[], int],
        user_id: str = LOCAL_USER_ID,
    ) -> None:
        self._store = store
        self._primary = primary
        self._fallback = fallback
        self._now = now
        self._user_id = user_id

    def close(self) -> None:
        for source in (self._primary, self._fallback):
            close = getattr(source, "close", None)
            if close is not None:
                close()

    def fetch(self, mint: str, timeframe: str, start: int, end: int) -> list[Bar]:
        """Store closed real bars the provider returned before ``end``.

        ``start`` is the request bound. A closed bar older than ``start`` is
        still stored, so an out-of-order arrival is visible to the scheduler.
        The primary source is tried first. The fallback is used only when the
        primary raises and the fallback is enabled. A disabled fallback is
        not called.
        """
        if timeframe not in TIMEFRAME_SECONDS:
            raise SourceError(f"timeframe {timeframe!r} is not supported")
        if isinstance(start, bool) or isinstance(end, bool):
            raise SourceError("bar window is invalid")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
            raise SourceError("bar window is invalid")
        try:
            result, provider = self._pull(self._primary, mint, timeframe, start, end)
        except SourceError:
            fallback = self._usable_fallback()
            if fallback is None:
                raise
            result, provider = self._pull(fallback, mint, timeframe, start, end)
        closed = [
            bar
            for bar in closed_real_bars(result.bars, timeframe, int(self._now()))
            if bar.mint == mint and bar.open_time < end
        ]
        if closed:
            conflicts = self._store.upsert_live(
                closed, user_id=self._user_id, provider=provider
            )
            for bar in conflicts:
                logger.warning(
                    "live bar conflict provider=%s mint=%s timeframe=%s open_time=%s",
                    provider,
                    bar.mint,
                    bar.timeframe,
                    bar.open_time,
                )
        return closed

    def _usable_fallback(self) -> _Source | None:
        fallback = self._fallback
        if fallback is None:
            return None
        if not getattr(fallback, "enabled", True):
            return None
        return fallback

    def _pull(
        self, source: _Source, mint: str, timeframe: str, start: int, end: int
    ) -> tuple[FetchResult, str]:
        payloads = getattr(source, "payloads", None)
        before = len(payloads) if isinstance(payloads, list) else 0
        try:
            result = source.fetch_bars(mint, timeframe, start, end)
        except BaseException:
            if isinstance(payloads, list):
                del payloads[before:]
            raise
        if isinstance(payloads, list):
            fresh = [payloads[index] for index in range(before, len(payloads))]
            del payloads[: len(payloads)]
            texts = [_decode(body) for body in fresh]
            rows = [
                (str(getattr(source, "name", "")), mint, timeframe, text)
                for text in texts
                if text is not None
            ]
            if rows:
                self._store.put_live_payloads(self._user_id, rows)
        return result, str(getattr(source, "name", ""))


def _decode(body: object) -> str | None:
    if not isinstance(body, (bytes, bytearray)):
        return None
    try:
        return bytes(body).decode("utf-8")
    except UnicodeDecodeError:
        return None
