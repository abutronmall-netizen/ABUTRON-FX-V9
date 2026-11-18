"""Agent base contract for the ABUTRON FX agentic multi-agent system.

Extends the V9 ``apex.agents.base.BaseAgent`` concept into a richer
agentic contract: every agent has a role, declared capabilities, a
reasoning channel, and a single ``act`` entry point that produces an
``AgentResult``. Agents never place orders directly — execution-capable
agents route through the ``RiskGate`` and the native dispatcher.
"""
from __future__ import annotations

import time
import traceback
from typing import Any, Dict, List, Optional

from .interfaces import (
    AgentCapability,
    AgentContext,
    AgentDecision,
    AgentResult,
    AgentRole,
)


class Agent:
    """Base class for all agents in the hybrid multi-agent system.

    Subclasses implement ``act``. The base class handles timing, error
    isolation (an agent failure degrades to a failed ``AgentResult``
    rather than aborting the cycle), and trace recording.
    """

    #: Human-readable role; subclasses override.
    role: AgentRole = AgentRole.ORCHESTRATOR
    #: Unique agent name.
    name: str = "agent"

    def __init__(self) -> None:
        self._capabilities: List[AgentCapability] = []

    # ------------------------------------------------------------------ #
    # Capabilities
    # ------------------------------------------------------------------ #
    @property
    def capabilities(self) -> List[AgentCapability]:
        return list(self._capabilities)

    def capability(self, name: str, description: str = "") -> "Agent":
        """Declare a capability this agent provides."""
        self._capabilities.append(AgentCapability(name, description))
        return self

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def setup(self) -> None:
        """One-time initialisation. Override in subclasses if needed."""

    def teardown(self) -> None:
        """Cleanup. Override in subclasses if needed."""

    # ------------------------------------------------------------------ #
    # Core contract
    # ------------------------------------------------------------------ #
    def act(self, context: AgentContext) -> AgentResult:
        """Produce an ``AgentResult`` for the given context.

        The default implementation raises ``NotImplementedError``.
        Subclasses override this. Implementations should be defensive:
        never let an exception escape unhandled beyond what ``_safe_act``
        permits.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Safe invocation
    # ------------------------------------------------------------------ #
    def _safe_act(self, context: AgentContext) -> AgentResult:
        """Run ``act`` with error isolation and timing.

        Returns a failed ``AgentResult`` if ``act`` raises, so one agent's
        failure never aborts the whole decision cycle.
        """
        t0 = time.time()
        try:
            result = self.act(context)
        except Exception as exc:  # noqa: BLE001 - deliberate fail-soft
            return AgentResult(
                agent=self.name, role=self.role, ok=False,
                summary="agent raised", error=f"{type(exc).__name__}: {exc}",
                data={"traceback": traceback.format_exc().splitlines()[-1],
                      "latency_ms": round((time.time() - t0) * 1000.0, 2)},
            )
        if not result.agent:
            result.agent = self.name
        if not result.role:
            result.role = self.role
        result.data.setdefault("latency_ms",
                               round((time.time() - t0) * 1000.0, 2))
        return result

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__} {self.name} role={self.role.value}>"
