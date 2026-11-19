"""Historical OHLCV for SOL and native Solana majors.

Callers use a mint address. The provider adapter is replaceable.
See ``docs/ohlcv.md``.
"""

from app.ohlcv.registry import MAJORS, Asset
from app.ohlcv.source import FetchResult, OhlcvSource

__all__ = ["MAJORS", "Asset", "FetchResult", "OhlcvSource"]
