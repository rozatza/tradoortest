"""Configured trading wallet. A public address only.

``LIVE_WALLET_PUBKEY`` is the address this process may trade. The auto
signer compares a derived public key with this value. This module does
not read the seed file. A missing or malformed value fails closed.
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
