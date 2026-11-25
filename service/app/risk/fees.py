"""Fee lines kept apart from strategy P&L.

Network fee, priority fee, and token-account rent each have a column.
A venue platform fee is a fourth column. None of them is added to
``realized_loss_usd``. The loss cap and the fee cap halt separately.

The calendar day is the server's UTC date. A request body does not
choose it. The row is stored in sqlite when a store is passed. That
row has no price and no payload, and the 24-hour purge does not
delete it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from app.risk.limits import DEFAULT_LIMITS, RiskLimits


@dataclass(frozen=True, slots=True)
class FeeLines:
    """One transaction's costs. Lamports are whole numbers."""

    network_lamports: int
    priority_lamports: int
    rent_lamports: int
    venue_fee_amount: int | None = None

    def total_lamports(self) -> int:
        """The three chain costs. The venue fee is not lamports here."""
        return self.network_lamports + self.priority_lamports + self.rent_lamports


@dataclass(frozen=True, slots=True)
class DayLedger:
    """One calendar day. ``day_key`` only moves forward."""

    day_key: str
    notional_usd: Decimal = Decimal("0")
    realized_loss_usd: Decimal = Decimal("0")
    network_lamports: int = 0
    priority_lamports: int = 0
    rent_lamports: int = 0
    venue_fee_amount: int = 0
    halted: bool = False

    def fee_total_lamports(self) -> int:
        return self.network_lamports + self.priority_lamports + self.rent_lamports

    def cost_lines(self) -> dict[str, object]:
        """Report shape. Strategy loss and the fee lines are separate keys."""
        return {
            "strategy_realized_loss_usd": format(self.realized_loss_usd, "f"),
            "network_fee_lamports": self.network_lamports,
            "priority_fee_lamports": self.priority_lamports,
            "rent_lamports": self.rent_lamports,
            "venue_fee_amount": self.venue_fee_amount,
        }


def empty_day(day_key: str) -> DayLedger:
    return DayLedger(day_key=day_key)


def roll_day(ledger: DayLedger, today: str) -> DayLedger:
    """Keep the stored day when the clock steps backward."""
    if today < ledger.day_key:
        return ledger
    if today == ledger.day_key:
        return ledger
    return empty_day(today)


def server_day(now: int | None = None) -> str:
    """UTC ``YYYY-MM-DD`` from the server clock. Not a request field."""
    stamp = int(_clock()) if now is None else now
    if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp < 0:
        raise ValueError("bad_clock")
    return datetime.fromtimestamp(stamp, UTC).date().isoformat()


def set_day_clock(clock: Callable[[], int] | None) -> None:
    """Test helper. ``None`` restores the wall clock."""
    global _clock
    _clock = time.time if clock is None else clock


def add_fees(ledger: DayLedger, fees: FeeLines) -> DayLedger:
    """Add the three lines and the venue fee. Does not touch strategy P&L."""
    venue = fees.venue_fee_amount or 0
    return replace(
        ledger,
        network_lamports=ledger.network_lamports + fees.network_lamports,
        priority_lamports=ledger.priority_lamports + fees.priority_lamports,
        rent_lamports=ledger.rent_lamports + fees.rent_lamports,
        venue_fee_amount=ledger.venue_fee_amount + venue,
    )


_ledger_lock = threading.Lock()
_current_day = DayLedger(day_key="1970-01-01")
_clock: Callable[[], float] = time.time


def reset_day_ledger() -> None:
    """Test helper. Clears the memory copy and the injected clock.

    A sqlite row is left in place. A new store opened on that file still
    has the caps and the halt.
    """
    global _current_day, _clock
    with _ledger_lock:
        _current_day = DayLedger(day_key="1970-01-01")
        _clock = time.time


def take_day(today: str, store: object | None = None) -> DayLedger:
    """The ledger for ``today``, rolled forward only from that clock.

    ``store`` is the durable row. Without one, the memory copy is what
    unit tests use. A client day is not an argument.
    """
    with _ledger_lock:
        current = _current_day if store is None else _read(store)
        return roll_day(current, today)


def save_day(ledger: DayLedger, store: object | None = None) -> None:
    """Remember ``ledger``. With a store, the row is replaced."""
    global _current_day
    with _ledger_lock:
        _current_day = ledger
        if store is not None:
            store.write_live_day(_row(ledger))  # type: ignore[attr-defined]


def _read(store: object) -> DayLedger:
    row = store.read_live_day()  # type: ignore[attr-defined]
    if row is None:
        return DayLedger(day_key="1970-01-01")
    return DayLedger(
        day_key=str(row[0]),
        notional_usd=Decimal(str(row[1])),
        realized_loss_usd=Decimal(str(row[2])),
        network_lamports=int(row[3]),
        priority_lamports=int(row[4]),
        rent_lamports=int(row[5]),
        venue_fee_amount=int(row[6]),
        halted=bool(row[7]),
    )


def _row(ledger: DayLedger) -> tuple[object, ...]:
    return (
        ledger.day_key,
        format(ledger.notional_usd, "f"),
        format(ledger.realized_loss_usd, "f"),
        ledger.network_lamports,
        ledger.priority_lamports,
        ledger.rent_lamports,
        ledger.venue_fee_amount,
        1 if ledger.halted else 0,
    )


def record_realized_loss(
    ledger: DayLedger,
    pnl_usd: Decimal,
    limits: RiskLimits | None = None,
) -> DayLedger:
    """Book strategy P&L. A gain does not reduce the loss counter.

    ``pnl_usd`` is the trade's in and out amounts. Fees are not part of it.
    """
    chosen = DEFAULT_LIMITS if limits is None else limits
    if pnl_usd >= 0:
        return ledger
    loss = ledger.realized_loss_usd + (-pnl_usd)
    halted = ledger.halted or loss >= chosen.daily_loss_usd
    return replace(ledger, realized_loss_usd=loss, halted=halted)
