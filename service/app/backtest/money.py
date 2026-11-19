"""Cent rounding, and the fill-quantity grid.

A fill quantity is notional divided by fill price, then rounded down to
``1e-12``. That grid stands in for a token's base units. The quotient often
does not terminate, and an unrounded quantity grows without a bound as a
position is added to. ``quantize_cents`` then rounds ``qty * price`` itself,
so the cent is the half-even cent of that grid quantity times the price.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, getcontext, localcontext
from fractions import Fraction

from app.backtest.errors import BacktestError

_QTY_PREC = 50
# A fill quantity is a whole number of these units. The grid stands in for a
# token's base units: the engine does not model a mint's decimals.
_QTY_UNITS = 10**12


def quantize_cents(amount: Fraction) -> Decimal:
    """Half-even to 0.01 USDC from the exact rational ``amount``."""
    try:
        return _cents(amount)
    except DecimalException as exc:
        raise BacktestError("order amount is outside the range this book can price") from exc


def floor_qty(amount: Fraction) -> Decimal:
    """Round a fill quantity down to the ``1e-12`` grid.

    A buy uses this before the cash leaves the account, so the quantity
    costs at most the notional it was given. Zero means the notional cannot
    buy one unit.
    """
    if amount <= 0:
        return Decimal(0)
    scaled = amount * _QTY_UNITS
    units = scaled.numerator // scaled.denominator
    if units <= 0:
        return Decimal(0)
    # ``scaleb`` keeps the coefficient when it fits in the active context.
    # A wider context used to return the exact integer, and the 50-digit
    # book then rounded it on ``qty += qty``. That miss showed up later as
    # a P&L identity error. Refuse it here, with a message that names the
    # precision, before the book records a quantity it cannot hold.
    ctx = getcontext()
    digits = len(str(units))
    if digits > ctx.prec:
        raise BacktestError(
            f"fill quantity has {digits} digits, above the {ctx.prec}-digit "
            "precision this book can price"
        )
    return Decimal(units).scaleb(-12)


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
