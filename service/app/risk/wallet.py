"""Configured trading wallet. A public address only.

The process never loads a keypair, a seed, or a secret file. The
environment holds the pubkey the user will sign with. A missing or
malformed value fails closed.
"""

from __future__ import annotations

import os

from app.venue.address import require_address
from app.venue.errors import VenueError


def configured_wallet() -> str | None:
    """The pubkey in ``LIVE_WALLET_PUBKEY``, or None when it is unusable."""
    raw = os.environ.get("LIVE_WALLET_PUBKEY")
    if raw is None:
        return None
    text = raw.strip()
    if text == "":
        return None
    try:
        return require_address(text)
    except VenueError:
        return None
