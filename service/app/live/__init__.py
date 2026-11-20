"""Near-real-time bars and the paper scheduler.

The feed stores closed bars. The scheduler calls the shared runner once per
closed bar for each registered strategy version. Nothing here sends an order.
See ``docs/live-feed.md``.
"""

from app.live.scheduler import PaperScheduler, register_paper_strategy

__all__ = ["PaperScheduler", "register_paper_strategy"]
