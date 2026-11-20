"""Author agent loop, guardrails, and conversation store.

The loop calls the ROZ-76 dispatcher. It does not reimplement tools, and it
does not hold a wallet. See ``docs/agent-loop.md``.
"""

from app.agent.loop import accept_proposal, reject_proposal, run_turn
from app.agent.providers import LLMProvider, StubProvider
from app.agent.store import AgentStore

__all__ = [
    "AgentStore",
    "LLMProvider",
    "StubProvider",
    "accept_proposal",
    "reject_proposal",
    "run_turn",
]
