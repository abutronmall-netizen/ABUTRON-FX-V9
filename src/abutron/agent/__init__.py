"""ABUTRON FX V9 — Agentic AI Hybrid Multi-Agent System.

This package is the agentic orchestration layer that unifies the V9
platform into a single hybrid multi-agent system:

    sense  ->  think  ->  gate  ->  act  ->  monitor  ->  recover

It composes, rather than replaces, the existing V9 subsystems:

* **Brains**  (``abutron.brain``)       — NexusBrain / UnifiedBrain signal
  substrate. Exposed to agents as a callable ``predict`` tool.
* **Risk**    (``abutron.risk``)        — RiskGuard / RiskEngine. Wrapped by
  the deterministic, *unbypassable* ``RiskGate`` that sits on every action
  path. V9 is the main system; the agent runs on top of it, never past it.
* **Execution** (``abutron.execution``) — the native single-writer
  dispatcher. Routed through ``ExecutionAgent``.
* **LLM layer** (``abutron.apex.gemma`` / ``debate``) — the pluggable
  reasoning backend. When no LLM gateway is configured, the system
  degrades to a deterministic ``RuleReasoningBackend`` so it runs and
  tests anywhere.

Design invariants (see ``docs/AGENTIC_SYSTEM_DESIGN.md``):

1. ``RiskGate`` is always in the path. Even post-deploy, when the agentic
   AI is the primary operating layer, money-safety invariants stay
   deterministic and unbypassable.
2. Reasoning is pluggable: an LLM backend when configured, a deterministic
   rule-based backend otherwise. The agentic AI is never hard-blocked by
   a missing model gateway.
3. Shadow / record-only mode by default (evidence-only during build, test
   and deploy). Live mode is an explicit, separately-certified toggle that
   still traverses the RiskGate.
4. Every decision produces an evidence record in the ``DecisionLedger``.

Public API::

    from abutron.agent import build_agent_system
    system = build_agent_system()            # or build_agent_system(live=False)
    decision = system.decide(context)
"""
from __future__ import annotations

from .interfaces import (
    AgentAction,
    AgentCapability,
    AgentContext,
    AgentDecision,
    AgentResult,
    AgentRole,
    Direction,
    Signal,
    Verdict,
)
from .base import Agent
from .memory import DecisionLedger
from .reasoning import (
    LLMReasoningBackend,
    ReasoningBackend,
    RuleReasoningBackend,
)
from .risk_gate import RiskGate
from .substrate import BrainSubstrate
from .orchestrator import AgentOrchestrator
from .agents import (
    ExecutionAgent,
    OrchestratorAgent,
    MonitoringAgent,
    RecoveryAgent,
    RiskAgent,
    SignalAgent,
)

__all__ = [
    "Agent",
    "AgentAction",
    "AgentCapability",
    "AgentContext",
    "AgentDecision",
    "AgentResult",
    "AgentRole",
    "AgentOrchestrator",
    "BrainSubstrate",
    "Direction",
    "ExecutionAgent",
    "LLMReasoningBackend",
    "MonitoringAgent",
    "OrchestratorAgent",
    "ReasoningBackend",
    "RecoveryAgent",
    "RiskAgent",
    "RiskGate",
    "RuleReasoningBackend",
    "Signal",
    "SignalAgent",
    "Verdict",
    "DecisionLedger",
    "build_agent_system",
]


def build_agent_system(
    *,
    live: bool = False,
    brain_substrate: "BrainSubstrate | None" = None,
    reasoning: "ReasoningBackend | None" = None,
    risk_gate: "RiskGate | None" = None,
    ledger: "DecisionLedger | None" = None,
) -> "AgentOrchestrator":
    """Assemble the full hybrid multi-agent system.

    Parameters
    ----------
    live:
        When ``False`` (default) the system runs in shadow / record-only
        mode: actions are evaluated through the RiskGate and recorded but
        never submitted to a broker. When ``True`` the ExecutionAgent is
        allowed to dispatch — still only after the RiskGate approves.
    brain_substrate:
        Optional pre-built ``BrainSubstrate``. If omitted, a substrate is
        built lazily from the configured V9 brain root.
    reasoning:
        Optional ``ReasoningBackend``. If omitted, an LLM backend is used
        when a gateway is configured, otherwise the deterministic
        ``RuleReasoningBackend``.
    risk_gate:
        Optional pre-built ``RiskGate``. If omitted, one is built from the
        V9 risk configuration.
    ledger:
        Optional pre-built ``DecisionLedger``. If omitted, an in-memory
        ledger is created.
    """
    substrate = brain_substrate or BrainSubstrate()
    gate = risk_gate or RiskGate()
    if reasoning is None:
        reasoning = LLMReasoningBackend.from_config().or_fallback()
    return AgentOrchestrator(
        signal_agent=SignalAgent(substrate),
        risk_agent=RiskAgent(gate),
        execution_agent=ExecutionAgent(gate, live=live),
        monitoring_agent=MonitoringAgent(ledger or DecisionLedger()),
        recovery_agent=RecoveryAgent(),
        reasoning=reasoning,
    )
