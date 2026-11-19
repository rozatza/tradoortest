"""Cent rounding from an exact rational.

Quantity is notional divided by fill price. That quotient often does not
terminate. Rounding it to 50 significant digits, then multiplying by the
sale price, lands just off an exact half-cent. Half-even then keeps the
wrong cent. ``quantize_cents`` rounds the fraction itself, so the cent is
the half-even cent of the exact value.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, localcontext
from fractions import Fraction

from app.backtest.errors import BacktestError

_QTY_PREC = 50


def quantize_cents(amount: Fraction) -> Decimal:
    """Half-even to 0.01 USDC from the exact rational ``amount``."""
    try:
        return _cents(amount)
    except DecimalException as exc:
        raise BacktestError("order amount is outside the range this book can price") from exc


def decimal_ratio(amount: Fraction) -> Decimal:
    """Render a quantity or an average price at 50 significant digits.

    This is the figure stored on a fill. Money uses ``quantize_cents`` on
    the fraction, not this rounded decimal.
    """
    if amount.denominator == 0:
        raise BacktestError("order amount is outside the range this book can price")
    try:
        with localcontext() as ctx:
            ctx.prec = _QTY_PREC
            ctx.rounding = ROUND_HALF_EVEN
            return Decimal(amount.numerator) / Decimal(amount.denominator)
    except DecimalException as exc:
        raise BacktestError("order amount is outside the range this book can price") from exc


def _cents(amount: Fraction) -> Decimal:
    if amount == 0:
        return Decimal("0.00")
    sign = -1 if amount < 0 else 1
    if sign < 0:
        amount = -amount
    scaled = amount * 100
    q, r = divmod(scaled.numerator, scaled.denominator)
    if r * 2 > scaled.denominator or (r * 2 == scaled.denominator and q % 2 == 1):
        q += 1
    # scaleb changes the exponent. It does not round through the 50-digit context.
    return Decimal(sign * q).scaleb(-2)
