"""Simulated paper fills for one registered strategy version.

The book is a cash balance and open longs. Nothing here has a key, a
signature, or a chain. See ``docs/paper.md``.
"""

from app.paper.broker import ORDER_TTL_SECONDS, PaperBroker

__all__ = ["ORDER_TTL_SECONDS", "PaperBroker"]
