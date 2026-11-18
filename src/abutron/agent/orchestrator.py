"""AgentOrchestrator — the runtime that wires the full agentic cycle.

Runs the six-stage pipeline for every decision:

    sense      -> SignalAgent        (brain signal)
    think      -> OrchestratorAgent  (reasoning + action build)
    gate       -> RiskAgent          (unbypassable risk evaluation)
    act        -> ExecutionAgent     (dispatch only if gate approved)
    monitor    -> MonitoringAgent    (evidence ledger)
    recover    -> RecoveryAgent      (fail-soft + quarantine)

Each stage is invoked through ``Agent._safe_act`` so a single agent's
failure degrades to a recorded error rather than aborting the cycle. The
orchestrator assembles the final ``AgentDecision`` with a full agent
trace, the reasoning, the gate verdict, and the execution receipt.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .agents import (
    ExecutionAgent,
    MonitoringAgent,
    OrchestratorAgent,
    RecoveryAgent,
    RiskAgent,
    SignalAgent,
)
from .interfaces import (
    AgentContext,
    AgentDecision,
    AgentResult,
    Verdict,
)
from .reasoning import ReasoningBackend


class AgentOrchestrator:
    """Coordinates the hybrid multi-agent decision cycle."""

    def __init__(
        self,
        signal_agent: SignalAgent,
        risk_agent: RiskAgent,
        execution_agent: ExecutionAgent,
        monitoring_agent: MonitoringAgent,
        recovery_agent: RecoveryAgent,
        reasoning: ReasoningBackend,
    ) -> None:
        self.signal_agent = signal_agent
        self.think_agent = OrchestratorAgent(reasoning)
        self.risk_agent = risk_agent
        self.execution_agent = execution_agent
        self.monitoring_agent = monitoring_agent
        self.recovery_agent = recovery_agent
        self.reasoning = reasoning

        # Declared pipeline order: (stage name, agent).
        self._pipeline = [
            ("sense", self.signal_agent),
            ("think", self.think_agent),
            ("gate", self.risk_agent),
            ("act", self.execution_agent),
        ]

    # ------------------------------------------------------------------ #
    def decide(self, context: AgentContext) -> AgentDecision:
        """Run the full decision cycle for one context."""
        decision = AgentDecision.new(context.symbol, context.now_utc)
        trace: List[Dict[str, Any]] = []

        # 1. sense -> think -> gate -> act. A stage failure short-circuits
        #    the pipeline; recovery handles the failure.
        for stage_name, agent in self._pipeline:
            result = agent._safe_act(context)
            trace.append(self._trace_entry(stage_name, result))
            if not result.ok:
                break

        # 2. Assemble the decision from what the stages recorded, BEFORE
        #    monitor/recover so the monitor can journal the complete record.
        decision.agent_trace = trace
        decision.signal = context.signal
        decision.reasoning = context.extras.get("reasoning", {})
        decision.action = context.extras.get("action")
        gate = context.extras.get("gate_result", {})
        decision.verdict = Verdict(gate.get("verdict", Verdict.SHADOW.value))
        decision.gate_reason = gate.get("reason", "")
        receipt = context.extras.get("execution_receipt")
        if receipt:
            decision.execution_receipt = receipt
            decision.executed = bool(receipt.get("ok", False)) and \
                receipt.get("mode") == "live"
        decision.cycle_finished_utc = time.time()
        context.extras["decision"] = decision

        # 3. monitor + recover always run, on the assembled decision.
        trace.append(self._trace_entry(
            "monitor", self.monitoring_agent._safe_act(context)))
        trace.append(self._trace_entry("recover", self._recover(context)))
        decision.agent_trace = trace
        return decision

    # ------------------------------------------------------------------ #
    def _recover(self, context: AgentContext) -> AgentResult:
        return self.recovery_agent._safe_act(context)

    @staticmethod
    def _trace_entry(stage: str, result: AgentResult) -> Dict[str, Any]:
        return {
            "stage": stage,
            "agent": result.agent,
            "ok": result.ok,
            "summary": result.summary,
            "error": result.error,
            "latency_ms": result.data.get("latency_ms"),
        }

    # ------------------------------------------------------------------ #
    @property
    def ledger(self):
        return self.monitoring_agent.ledger

    @property
    def recovery(self):
        return self.recovery_agent
