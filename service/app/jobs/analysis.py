"""Time-of-day and weekday counts from a stored trade log.

The clock is ``entry_time`` on the trade log. That value is a unix second.
It is read as UTC. ``hour_utc`` is that instant's hour, 0 through 23.
``weekday_utc`` is Monday 0 through Sunday 6, the same numbering as
``datetime.date.weekday`` of that UTC instant.

A bucket is a count of trade-log rows. It is not a return, a win rate, or
an average. A row whose ``entry_time`` is missing or not a whole unix
second is not counted. Hours and weekdays with no counted row are omitted.
An empty counted set is ``available: false`` and has no hour list and no
weekday list, so a reader cannot draw 24 zeros or 7 zeros.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

ANALYSIS_SCHEMA = "tradoortest.backtest_analysis.v1"

WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def trade_timing(trades: object) -> dict[str, Any]:
    """Bucket ``trades``. ``trades`` must be a list. A bad row is skipped."""
    if not isinstance(trades, list):
        raise ValueError("trades")
    used: list[int] = []
    for trade in trades:
        if not isinstance(trade, Mapping):
            continue
        stamp = _unix(trade.get("entry_time"))
        if stamp is None:
            continue
        used.append(stamp)
    base = {
        "schema": ANALYSIS_SCHEMA,
        "timezone": "UTC",
        "trades_total": len(trades),
        "trades_used": len(used),
    }
    if not used:
        base["available"] = False
        return base
    hours: Counter[int] = Counter()
    days: Counter[int] = Counter()
    for stamp in used:
        instant = datetime.fromtimestamp(stamp, UTC)
        hours[instant.hour] += 1
        days[instant.weekday()] += 1
    base["available"] = True
    base["hours"] = [
        {"hour_utc": hour, "count": hours[hour]} for hour in sorted(hours)
    ]
    base["weekdays"] = [
        {"weekday_utc": day, "weekday": WEEKDAYS[day], "count": days[day]}
        for day in sorted(days)
    ]
    return base


def _unix(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None
