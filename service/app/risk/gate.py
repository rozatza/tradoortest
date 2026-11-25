"""Per-tick live gate. Paper decisions are not this object.

``note`` runs at the top of a scheduler tick. It reads the kill file
first. A gap longer than K intervals halts and does not emit an order
per skipped bar. ``consider`` returns at most the newest bar, and
returns nothing once halted.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from app.risk.killfile import kill_file_path, kill_halted, write_kill_halt
from app.risk.limits import DEAD_MAN_TICKS
from app.risk.pending import PendingBook, pending_book
from app.venue.flags import live_trading_enabled


class LiveGate:
    """One scheduler's dead-man clock. The kill file is shared."""

    def __init__(
        self,
        *,
        miss_limit: int = DEAD_MAN_TICKS,
        pending: PendingBook | None = None,
        kill_path: Path | None = None,
    ) -> None:
        self.miss_limit = miss_limit
        self.pending = pending_book() if pending is None else pending
        self.kill_path = kill_path
        self.last_at: float | None = None
        self.halted = False
        self.dead_man = False

    def note(self, *, now: float, interval: float) -> None:
        """Read the flag, then the dead-man. Does not walk skipped bars."""
        path = self.kill_path if self.kill_path is not None else kill_file_path()
        if kill_halted(path):
            self.halted = True
            self.pending.cancel_all()
        elif not self.dead_man:
            self.halted = False
        if (
            self.last_at is not None
            and interval > 0
            and now - self.last_at > self.miss_limit * interval
        ):
            self.dead_man = True
            self.halted = True
            self.pending.cancel_all()
            if path is not None:
                write_kill_halt(path)
        self.last_at = now

    def consider(self, bars: Sequence[object]) -> tuple[object, ...]:
        """The newest bar only, and only while the gate is clear.

        Older bars are not returned. A halted gate returns nothing, so a
        missed stretch does not become one order per skipped bar.
        """
        if self.halted or self.dead_man or not live_trading_enabled():
            return ()
        if kill_halted(self.kill_path if self.kill_path is not None else kill_file_path()):
            return ()
        if not bars:
            return ()
        return (bars[-1],)
