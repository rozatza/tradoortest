"""Kill and flatten. Flatten is never called from kill."""

from __future__ import annotations

from pathlib import Path

from app.risk.engine import LiveOrder, RiskResult, evaluate
from app.risk.fees import DayLedger
from app.risk.killfile import kill_file_path, mark_process_halt, write_kill_halt
from app.risk.limits import RiskLimits
from app.risk.pending import PendingBook, pending_book


class KillPersistError(Exception):
    """The halt could not be written. Pending work is still canceled."""

    code = "kill_unreadable"


def apply_kill(path: Path | None = None, book: PendingBook | None = None) -> int:
    """Halt and cancel pending unsigned trades. Does not flatten."""
    target = kill_file_path() if path is None else path
    book = pending_book() if book is None else book
    canceled = book.cancel_all()
    if target is None:
        mark_process_halt()
        raise KillPersistError()
    if not write_kill_halt(target):
        raise KillPersistError()
    return canceled


def apply_flatten(
    order: LiveOrder,
    ledger: DayLedger,
    *,
    limits: RiskLimits | None = None,
    today: str,
) -> RiskResult:
    """Explicit flatten. The kill switch does not call this."""
    if order.action != "flatten":
        raise ValueError("flatten")
    return evaluate(order, ledger, limits, today=today)
