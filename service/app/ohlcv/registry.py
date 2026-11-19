"""SOL and four native Solana majors, keyed by mint.

Wrapped BTC and wrapped ETH are not in this list. These five are large
assets that trade on Solana itself: SOL, the Jupiter aggregator token,
the Jito token, and Bonk and dogwifhat. Pool choice for the current
adapter lives with that adapter, not here, so a mint-native source can
replace it without editing callers.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ohlcv.errors import UnknownSymbol

SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_MINT = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
JTO_MINT = "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL"
BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WIF_MINT = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"


@dataclass(frozen=True, slots=True)
class Asset:
    """A symbol the CLI accepts, and the mint callers pass to a source."""

    symbol: str
    mint: str


MAJORS: tuple[Asset, ...] = (
    Asset("SOL", SOL_MINT),
    Asset("JUP", JUP_MINT),
    Asset("JTO", JTO_MINT),
    Asset("BONK", BONK_MINT),
    Asset("WIF", WIF_MINT),
)

BY_SYMBOL = {asset.symbol: asset for asset in MAJORS}
BY_MINT = {asset.mint: asset for asset in MAJORS}


def require_assets(symbols: list[str]) -> tuple[Asset, ...]:
    """Resolve CLI symbols. Matching is case-insensitive. Order is kept."""
    found: list[Asset] = []
    known = ", ".join(asset.symbol for asset in MAJORS)
    for symbol in symbols:
        key = symbol.strip().upper()
        asset = BY_SYMBOL.get(key)
        if asset is None:
            raise UnknownSymbol(f"unknown symbol {symbol!r}. Known symbols: {known}")
        found.append(asset)
    return tuple(found)
