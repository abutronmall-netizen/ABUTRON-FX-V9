"""OrchestratorAgent — the *think* stage of the agentic cycle.

Calls the pluggable reasoning backend to synthesise a decision intent
from the signal + context, then builds a concrete ``AgentAction``. The
reasoning layer decides *what* to do; this agent translates that into a
gate-able action. It never executes — execution is the ExecutionAgent's
job, and only after the RiskAgent approves.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..base import Agent
from ..interfaces import (
    AgentAction,
    AgentContext,
    AgentDecision,
    AgentResult,
    AgentRole,
    Direction,
    Signal,
    Verdict,
)
from ..reasoning import ReasoningBackend


class OrchestratorAgent(Agent):
    role = AgentRole.ORCHESTRATOR
    name = "orchestrator"

    def __init__(self, reasoning: ReasoningBackend) -> None:
        super().__init__()
        self.capability("reason", "synthesise decision intent from signal/context")
        self.capability("build_action", "translate intent into a gate-able action")
        self.reasoning = reasoning

    def act(self, context: AgentContext) -> AgentResult:
        signal = context.signal
        reasoning = self.reasoning.reason(context, signal)

        intent_direction = reasoning.get("signal", Direction.HOLD)
        confidence = float(reasoning.get("confidence", 0.0))

        # Only build a money action when the intent is a real direction
        # with enough confidence. Otherwise record a HOLD / no-action.
        action: Optional[AgentAction] = None
        if intent_direction is not Direction.HOLD and confidence > 0.0 and signal:
            action = self._build_action(context, signal, intent_direction,
                                        confidence)
        elif intent_direction is Direction.HOLD:
            # Explicit hold: a record-only action the gate passes through.
            action = AgentAction.new("record", context.symbol,
                                     Direction.HOLD,
                                     rationale=reasoning.get("narrative", "hold"))
            action.requires_gate = False

        context.extras["reasoning"] = reasoning
        context.extras["action"] = action
        return AgentResult(
            agent=self.name, role=self.role, ok=True,
            summary=f"intent={intent_direction.value} conf={confidence:.3f} "
                    f"backend={reasoning.get('backend', '?')}",
            data={"reasoning": reasoning,
                  "action": action.to_dict() if action else None},
        )

    def _build_action(self, context: AgentContext, signal: Signal,
                      direction: Direction, confidence: float) -> AgentAction:
        """Translate a reasoned intent into a concrete, gate-able action.

        Stop/target geometry is derived from the bar context. A real
        runtime would source ATR-based levels from the V9 geometry module;
        here we compute a conservative default so the action is complete
        and gate-able. The RiskAgent is the authority on whether it passes.
        """
        price = context.bar_close or context.extras.get("price", 0.0)
        # Conservative default geometry; the gate validates R:R.
        if direction is Direction.BUY:
            stop_loss = price * 0.999  # ~10 bp
            take_profit = price * 1.0015  # ~15 bp  -> R:R 1.5
        else:
            stop_loss = price * 1.001
            take_profit = price * 0.9985
        quantity = max(0.01, signal.position_size)
        return AgentAction.new(
            "submit_order", context.symbol, direction,
            quantity=quantity,
            stop_loss=round(stop_loss, 6),
            take_profit=round(take_profit, 6),
            price=round(price, 6),
            rationale=signal.brain_source + ":intent",
            payload={"stop_risk_usd": 0.0, "confidence": confidence,
                     "source": signal.brain_source},
        )
