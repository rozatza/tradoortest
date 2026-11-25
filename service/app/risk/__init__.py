"""Risk engine, kill switch, and pre-sign guard.

Live orders call :func:`evaluate` before anything is signed. This package
does not sign, does not load a key, and does not submit a transaction.
See ``docs/live.md``.
"""

from app.risk.engine import evaluate
from app.risk.limits import RiskLimits

__all__ = ["RiskLimits", "evaluate"]
