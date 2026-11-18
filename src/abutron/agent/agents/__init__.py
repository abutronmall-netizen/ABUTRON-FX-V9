"""Specialised agents for the ABUTRON FX hybrid multi-agent system.

Each agent owns one stage of the decision cycle:

* ``SignalAgent``        — sense: brain signal from the substrate.
* ``OrchestratorAgent``  — think: reasoning synthesis + action build.
* ``RiskAgent``          — gate: unbypassable risk evaluation.
* ``ExecutionAgent``     — act: dispatch approved actions.
* ``MonitoringAgent``    — monitor: evidence ledger.
* ``RecoveryAgent``      — recover: fail-soft handling and quarantine.
"""
from __future__ import annotations

from .signal_agent import SignalAgent
from .orchestrator_agent import OrchestratorAgent
from .risk_agent import RiskAgent
from .execution_agent import ExecutionAgent
from .monitoring_agent import MonitoringAgent
from .recovery_agent import RecoveryAgent

__all__ = [
    "SignalAgent",
    "OrchestratorAgent",
    "RiskAgent",
    "ExecutionAgent",
    "MonitoringAgent",
    "RecoveryAgent",
]
