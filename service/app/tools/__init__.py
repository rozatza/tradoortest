"""Typed tool layer for the Phase 2 author agent.

The dispatcher validates arguments, calls a Phase 1 or Phase 2 service, and
returns a JSON envelope. It does not call a model and it does not hold a
wallet. See ``docs/agent-tools.md``.
"""

from app.tools.dispatch import Dispatcher
from app.tools.registry import REGISTRY, TOOL_ORDER, ToolSpec
from app.tools.runtime import ToolRuntime

__all__ = [
    "REGISTRY",
    "TOOL_ORDER",
    "Dispatcher",
    "ToolRuntime",
    "ToolSpec",
]
