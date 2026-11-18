"""RiskAgent — the *gate* stage of the agentic cycle.

Evaluates the proposed action through the unbypassable ``RiskGate`` and
records the verdict. This is the single point where money-safety
invariants are enforced; no downstream agent can bypass it.
"""
from __future__ import annotations

from typing import Any, Dict

from ..base import Agent
from ..interfaces import AgentAction, AgentContext, AgentResult, AgentRole, Verdict
from ..risk_gate import RiskGate


class RiskAgent(Agent):
    role = AgentRole.RISK
    name = "risk"

    def __init__(self, gate: RiskGate) -> None:
        super().__init__()
        self.capability("gate", "enforce money-safety invariants on actions")
        self.gate = gate

    def act(self, context: AgentContext) -> AgentResult:
        action = context.extras.get("action")
        if action is None:
            return AgentResult(
                agent=self.name, role=self.role, ok=True,
                summary="no_action_to_gate",
                data={"verdict": Verdict.SHADOW.value,
                      "reason": "NO_ACTION"},
            )
        if not isinstance(action, AgentAction):
            action = AgentAction(**action) if isinstance(action, dict) else None
        if action is None:
            return AgentResult(agent=self.name, role=self.role, ok=False,
                               summary="bad_action", error="action_not_parsed")

        result = self.gate.evaluate(action, context)
        context.extras["gate_result"] = {
            "verdict": result.verdict.value,
            "reason": result.reason,
            "details": result.details,
        }
        return AgentResult(
            agent=self.name, role=self.role, ok=True,
            summary=f"verdict={result.verdict.value} reason={result.reason}",
            data={"verdict": result.verdict.value,
                  "reason": result.reason,
                  "details": result.details,
                  "approved": result.approved},
        )
