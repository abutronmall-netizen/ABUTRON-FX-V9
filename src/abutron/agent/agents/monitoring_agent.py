"""MonitoringAgent — the *monitor* stage of the agentic cycle.

Appends the completed decision to the evidence ledger, producing the
audit trail. This is where the evidence-only discipline is preserved in
the agentic era.
"""
from __future__ import annotations

from typing import Any, Dict

from ..base import Agent
from ..interfaces import AgentContext, AgentDecision, AgentResult, AgentRole
from ..memory import DecisionLedger


class MonitoringAgent(Agent):
    role = AgentRole.MONITORING
    name = "monitoring"

    def __init__(self, ledger: DecisionLedger) -> None:
        super().__init__()
        self.capability("ledger", "append decisions to the evidence ledger")
        self.ledger = ledger

    def act(self, context: AgentContext) -> AgentResult:
        decision = context.extras.get("decision")
        if decision is None or not isinstance(decision, AgentDecision):
            return AgentResult(agent=self.name, role=self.role, ok=True,
                               summary="no_decision_to_record")
        record = self.ledger.append(decision)
        return AgentResult(
            agent=self.name, role=self.role, ok=True,
            summary=f"recorded:{self.ledger.count}",
            data={"ledger_count": self.ledger.count,
                  "decision_id": record.get("decision_id")},
        )
