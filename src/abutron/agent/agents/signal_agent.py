"""SignalAgent — the *sense* stage of the agentic cycle.

Reads the brain signal substrate and produces the ``Signal`` for the
current symbol/bar, attaching it to the context for downstream agents.
"""
from __future__ import annotations

from typing import Any, Dict

from ..base import Agent
from ..interfaces import AgentContext, AgentResult, AgentRole
from ..substrate import BrainSubstrate


class SignalAgent(Agent):
    role = AgentRole.SIGNAL
    name = "signal"

    def __init__(self, substrate: BrainSubstrate) -> None:
        super().__init__()
        self.capability("predict", "produce a brain signal for a symbol/bar")
        self.substrate = substrate

    def act(self, context: AgentContext) -> AgentResult:
        signal = self.substrate.predict(context)
        # Attach for downstream agents.
        context.signal = signal
        return AgentResult(
            agent=self.name, role=self.role, ok=True,
            summary=f"signal={signal.direction.value} "
                    f"source={signal.brain_source} conv={signal.conviction:.3f}",
            data={"signal": signal.to_dict()},
        )
