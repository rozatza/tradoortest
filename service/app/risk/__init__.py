"""Risk engine, kill switch, and pre-sign guard.

Live orders call :func:`evaluate` before anything is signed. The manual
path does not load a seed. The auto signer, when armed, loads one from
a file outside the repo and signs only the guarded message.
See ``docs/live.md``.
"""

from app.risk.engine import evaluate
from app.risk.limits import RiskLimits

__all__ = ["RiskLimits", "evaluate"]
