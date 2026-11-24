"""Cap on running paper strategies.

The process still has one user. The cap is enforced on that user. The
default is 3, a small book: two versions can run and a third still fits,
and a loop cannot register an unbounded set. ``PAPER_MAX_RUNNING`` overrides
it. The value is a whole number from 1 to 100. Blank means the default.
"""

from __future__ import annotations

import os

DEFAULT_PAPER_MAX_RUNNING = 3
_MAX_PAPER_RUNNING = 100


def paper_max_running(raw: str | None = None) -> int:
    """Return the running-paper cap. ``ValueError`` means the setting is unusable."""
    if raw is None:
        raw = os.environ.get("PAPER_MAX_RUNNING", "")
    text = raw.strip()
    if text == "":
        return DEFAULT_PAPER_MAX_RUNNING
    if not text.isdigit() or len(text) > 3 or (len(text) > 1 and text.startswith("0")):
        raise ValueError
    value = int(text)
    if value < 1 or value > _MAX_PAPER_RUNNING:
        raise ValueError
    return value
