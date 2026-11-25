"""Unsigned pending trades. Kill cancels them. Nothing here is signed."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class PendingTrade:
    """Ids and amounts only. No transaction bytes and no signature."""

    pending_id: int
    kind: str
    mint: str
    side: str
    status: str


class PendingBook:
    """Process-local book. A restart drops unsigned work."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[PendingTrade] = []
        self._next = 1

    def add(self, *, kind: str, mint: str, side: str) -> PendingTrade:
        with self._lock:
            row = PendingTrade(self._next, kind, mint, side, "pending")
            self._next += 1
            self._rows.append(row)
            return row

    def cancel_all(self) -> int:
        """Mark every pending row canceled. Returns how many changed."""
        with self._lock:
            changed = 0
            updated: list[PendingTrade] = []
            for row in self._rows:
                if row.status == "pending":
                    updated.append(replace(row, status="canceled"))
                    changed += 1
                else:
                    updated.append(row)
            self._rows = updated
            return changed

    def open_rows(self) -> tuple[PendingTrade, ...]:
        with self._lock:
            return tuple(row for row in self._rows if row.status == "pending")

    def reset(self) -> None:
        with self._lock:
            self._rows = []
            self._next = 1


_book = PendingBook()


def pending_book() -> PendingBook:
    return _book
