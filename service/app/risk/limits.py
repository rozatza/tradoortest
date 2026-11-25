"""Config defaults for the live risk engine.

Dollar checks share one caller-supplied USD mark. A missing mark rejects
the order and refuses signer startup. Network fees, priority fees, and
token-account rent are lamports. They are not added to strategy P&L.

The numbers that the design left open are set here:

- slippage sent on ``/order`` and ``/build``: 50 bps. The cap is 100 bps.
- price impact: absolute value of the quote's ``priceImpact`` number, cap 1
  (the pinned field's scale; the synthetic fixture uses ``-0.1``).
- pool liquidity: $50,000. Pool age: 86,400 seconds.
- stale bars: older than 2 bar intervals. Bar-versus-quote divergence: 1%.
- dead-man: a gap strictly longer than 2 scheduler intervals.
- per-transaction fee (network + priority + rent): 5,000,000 lamports.
- daily fee cap, separate from the $5 loss halt: 20,000,000 lamports.
- compute-unit limit cap: 1,400,000. Price cap: 1,000,000 micro-lamports.
  The product, rounded up to lamports, is capped at 100,000. A price with
  no limit is charged as if the limit were 1,400,000.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.ohlcv.registry import BONK_MINT, JTO_MINT, JUP_MINT, SOL_MINT, WIF_MINT

PER_TRADE_USD = Decimal("2.50")
DAILY_NOTIONAL_USD = Decimal("10")
DAILY_LOSS_USD = Decimal("5")
MAX_OPEN_POSITIONS = 3
WALLET_CEILING_USD = Decimal("25")
SLIPPAGE_BPS = 50
MAX_SLIPPAGE_BPS = 100
MAX_PRICE_IMPACT = Decimal("1")
MIN_POOL_LIQUIDITY_USD = Decimal("50000")
MIN_POOL_AGE_SECONDS = 86_400
STALE_BARS = 2
MAX_DIVERGENCE = Decimal("0.01")
DEAD_MAN_TICKS = 2
MAX_FEE_LAMPORTS = 5_000_000
DAILY_FEE_LAMPORTS = 20_000_000
MAX_CU_LIMIT = 1_400_000
MAX_CU_PRICE_MICRO_LAMPORTS = 1_000_000
MAX_PRIORITY_FEE_LAMPORTS = 100_000

ALLOWLIST_MINTS = frozenset({SOL_MINT, JUP_MINT, JTO_MINT, BONK_MINT, WIF_MINT})


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """The pure check's configuration. Defaults are the canary sizes."""

    per_trade_usd: Decimal = PER_TRADE_USD
    daily_notional_usd: Decimal = DAILY_NOTIONAL_USD
    daily_loss_usd: Decimal = DAILY_LOSS_USD
    max_open_positions: int = MAX_OPEN_POSITIONS
    wallet_ceiling_usd: Decimal = WALLET_CEILING_USD
    slippage_bps: int = SLIPPAGE_BPS
    max_slippage_bps: int = MAX_SLIPPAGE_BPS
    max_price_impact: Decimal = MAX_PRICE_IMPACT
    min_pool_liquidity_usd: Decimal = MIN_POOL_LIQUIDITY_USD
    min_pool_age_seconds: int = MIN_POOL_AGE_SECONDS
    stale_bars: int = STALE_BARS
    max_divergence: Decimal = MAX_DIVERGENCE
    dead_man_ticks: int = DEAD_MAN_TICKS
    max_fee_lamports: int = MAX_FEE_LAMPORTS
    daily_fee_lamports: int = DAILY_FEE_LAMPORTS
    max_cu_limit: int = MAX_CU_LIMIT
    max_cu_price_micro_lamports: int = MAX_CU_PRICE_MICRO_LAMPORTS
    max_priority_fee_lamports: int = MAX_PRIORITY_FEE_LAMPORTS
    allowlist: frozenset[str] = ALLOWLIST_MINTS


DEFAULT_LIMITS = RiskLimits()


def order_slippage_bps(limits: RiskLimits | None = None) -> int:
    """``slippageBps`` that a live ``/order`` must send. Never omitted."""
    chosen = DEFAULT_LIMITS if limits is None else limits
    return chosen.slippage_bps


def build_slippage_bps(limits: RiskLimits | None = None) -> int:
    """``slippageBps`` that a live ``/build`` must send. The same number."""
    return order_slippage_bps(limits)
