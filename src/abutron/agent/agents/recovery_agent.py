"""RecoveryAgent — the *recover* stage of the agentic cycle.

Provides fail-soft recovery when a stage errors or an action is
rejected. It quarantines incomplete/failed attempts (mirroring the
Issue #25 incomplete-run quarantine pattern) and records recovery
evidence. A recovery never rewrites committed history — it stages the
problem for inspection and lets the cycle continue.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..base import Agent
from ..interfaces import AgentContext, AgentResult, AgentRole, Verdict


class RecoveryAgent(Agent):
    role = AgentRole.RECOVERY
    name = "recovery"

    def __init__(self) -> None:
        super().__init__()
        self.capability("quarantine", "stage incomplete attempts for inspection")
        self.capability("fail_soft", "degrade gracefully on stage errors")
        self._quarantine: List[Dict[str, Any]] = []

    @property
    def quarantine(self) -> List[Dict[str, Any]]:
        return list(self._quarantine)

    def act(self, context: AgentContext) -> AgentResult:
        errors: List[str] = []
        # Collect any stage errors recorded during the cycle.
        for trace in context.extras.get("agent_trace", []):
            if trace.get("ok") is False or trace.get("error"):
                errors.append(trace.get("error") or trace.get("summary")
                              or "stage_error")

        gate = context.extras.get("gate_result", {})
        verdict = gate.get("verdict", "")

        recovered = False
        notes: List[str] = []
        if errors:
            # Quarantine the failed cycle for inspection; do not abort.
            record = {
                "symbol": context.symbol,
                "errors": errors,
                "verdict": verdict,
                "quarantined": True,
            }
            self._quarantine.append(record)
            recovered = True
            notes.append("quarantined_failed_cycle")

        if verdict == Verdict.REJECT.value:
            notes.append("gate_rejected_no_action")

        if not recovered and not notes:
            return AgentResult(agent=self.name, role=self.role, ok=True,
                               summary="no_recovery_needed")
        return AgentResult(
            agent=self.name, role=self.role, ok=True,
            summary=";".join(notes) or "no_recovery_needed",
            data={"recovered": recovered, "notes": notes,
                  "quarantine_count": len(self._quarantine)},
        )
