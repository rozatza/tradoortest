"""Live-trading switch.

The only on value is ``1``. Unset, blank, and every other value are off.
Quote and unsigned build do not read it: shadow mode quotes while trading
is off. ``execute`` raises whether the flag is on or off.
"""

from __future__ import annotations

import os


def live_trading_enabled(raw: str | None = None) -> bool:
    """True only for the exact string ``1``."""
    if raw is None:
        raw = os.environ.get("LIVE_TRADING_ENABLED")
        if raw is None:
            return False
    return raw == "1"
