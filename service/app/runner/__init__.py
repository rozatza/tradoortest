"""Shared per-bar runner. Decisions only: no orders are sent.

The backtester applies these decisions to a simulated book. A paper runner
can call ``step`` once per bar. See ``docs/runner.md``.
"""

from app.runner.core import (
    BarDecision,
    BarWindow,
    BookView,
    OcoDrop,
    OcoLevels,
    OcoTouch,
    RuleAttempt,
    RunnerConfig,
    RunnerState,
    SignalTruth,
    StepResult,
    config_from_document,
    initial_state,
    make_window,
    run,
    step,
)

__all__ = [
    "BarDecision",
    "BarWindow",
    "BookView",
    "OcoDrop",
    "OcoLevels",
    "OcoTouch",
    "RuleAttempt",
    "RunnerConfig",
    "RunnerState",
    "SignalTruth",
    "StepResult",
    "config_from_document",
    "initial_state",
    "make_window",
    "run",
    "step",
]
