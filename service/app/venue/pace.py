"""Spacing and a daily ceiling for Jupiter HTTP calls.

The retry backoff in the client is a separate wait: it runs only after a
timeout, an HTTP 429, or an HTTP 5xx, and it stops after five attempts.
This pace is the bound in front of every attempt. A call waits until the
minimum interval has elapsed, and the process stops for the rest of the
calendar day once the cap is reached. The day key moves forward only. A
clock step backward does not clear the count.

The defaults are a request throttle, not a risk limit. P4-04 owns the
risk engine.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from app.venue.errors import VenueError, VenueRejected

# At most one Jupiter HTTP call per second, unless the env overrides it.
DEFAULT_MIN_INTERVAL_SECONDS = 1.0
# Quote and build attempts, including retries, for one calendar day.
DEFAULT_DAILY_CAP = 1000
_MAX_INTERVAL_SECONDS = 120.0
_MAX_DAILY_CAP = 100_000

_INTERVAL_ENV = "JUPITER_MIN_INTERVAL_SECONDS"
_CAP_ENV = "JUPITER_DAILY_CAP"


def min_interval_seconds(raw: str | None = None) -> float:
    """Seconds between Jupiter HTTP attempts. Blank keeps the default."""
    if raw is None:
        raw = os.environ.get(_INTERVAL_ENV)
        if raw is None:
            return DEFAULT_MIN_INTERVAL_SECONDS
    text = raw.strip()
    if text == "":
        return DEFAULT_MIN_INTERVAL_SECONDS
    try:
        value = float(text)
    except ValueError:
        raise VenueError("bad_limit") from None
    if value < 0 or value > _MAX_INTERVAL_SECONDS:
        raise VenueError("bad_limit")
    return value


def daily_cap(raw: str | None = None) -> int:
    """Jupiter HTTP attempts allowed on one calendar day. Blank keeps 1000."""
    if raw is None:
        raw = os.environ.get(_CAP_ENV)
        if raw is None:
            return DEFAULT_DAILY_CAP
    text = raw.strip()
    if text == "":
        return DEFAULT_DAILY_CAP
    if not text.isascii() or not text.isdigit() or (len(text) > 1 and text.startswith("0")):
        raise VenueError("bad_limit")
    value = int(text)
    if value < 1 or value > _MAX_DAILY_CAP:
        raise VenueError("bad_limit")
    return value


class Pace:
    """In-process spacing and daily cap. ``sleep`` is the caller's wait."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], None],
        clock: Callable[[], float] | None = None,
        interval: float | None = None,
        cap: int | None = None,
    ) -> None:
        self._sleep = sleep
        self._clock = time.monotonic if clock is None else clock
        self._interval = DEFAULT_MIN_INTERVAL_SECONDS if interval is None else interval
        self._cap = DEFAULT_DAILY_CAP if cap is None else cap
        if self._interval < 0 or self._cap < 1:
            raise VenueError("bad_limit")
        self._slept = 0.0
        self._last: float | None = None
        self._day: int | None = None
        self._count = 0

    def acquire(self, now_unix: int) -> None:
        """Reserve one attempt. Raises ``quote_cap`` when the day is full."""
        if isinstance(now_unix, bool) or not isinstance(now_unix, int) or now_unix < 0:
            raise VenueError("bad_clock")
        day = now_unix // 86400
        if self._day is None or day > self._day:
            self._day = day
            self._count = 0
        if self._count >= self._cap:
            raise VenueRejected("quote_cap")
        self._count += 1
        now = self._clock() + self._slept
        if self._last is not None and self._interval > 0:
            gap = self._interval - (now - self._last)
            if gap > 0:
                self._sleep(gap)
                self._slept += gap
                now = self._clock() + self._slept
        self._last = now

    def note_sleep(self, seconds: float) -> None:
        """Count a retry backoff as time already spent waiting."""
        if seconds > 0:
            self._slept += seconds
