"""Bounded retry delay for provider HTTP calls.

The base delay doubles each attempt. An optional ``rng`` adds jitter so two
callers do not retry in lockstep. ``rng`` returns a fraction from 0 to 1.
HTTP 429 may carry ``Retry-After`` as a number of seconds; that delay is
used when it is longer than the backoff. The delay never exceeds the cap.
Callers that omit ``rng`` and do not receive ``Retry-After`` sleep the base
backoff, which is what the historical ingest already did.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import httpx

RETRY_CAP_SECONDS = 120.0


def retry_after_seconds(response: httpx.Response | None) -> float | None:
    """Parse a numeric ``Retry-After``. An HTTP-date or a bad value is ignored."""
    if response is None:
        return None
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    text = raw.strip()
    if text == "":
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        return None
    return value


def retry_delay(
    backoff: float,
    *,
    rng: Callable[[], float] | None,
    response: httpx.Response | None,
    status: int | None,
    cap: float = RETRY_CAP_SECONDS,
) -> float:
    """Seconds to sleep before the next attempt. ``backoff`` is the base."""
    delay = backoff
    if rng is not None:
        delay = backoff + _fraction(rng()) * backoff
    if status == 429:
        header = retry_after_seconds(response)
        if header is not None and header > delay:
            delay = header
    if delay > cap:
        return cap
    return delay


def _fraction(unit: object) -> float:
    if isinstance(unit, bool) or not isinstance(unit, (int, float)):
        return 0.0
    value = float(unit)
    if not math.isfinite(value) or value <= 0:
        return 0.0
    if value >= 1:
        return 1.0
    return value
