"""ExecutionAgent — the *act* stage of the agentic cycle.

Executes an action **only** if the RiskAgent approved it. In shadow
mode (default) it records a simulated receipt and never touches a broker.
In live mode it dispatches through the V9 native single-writer
dispatcher — and even then only after the gate approved.

This agent is the only component that can touch broker state, and it is
hard-wired to require a prior gate approval.
"""
from __future__ import annotations

from typing import Any, Dict

from ..base import Agent
from ..interfaces import AgentAction, AgentContext, AgentResult, AgentRole, Verdict
from ..risk_gate import RiskGate


class ExecutionAgent(Agent):
    role = AgentRole.EXECUTION
    name = "execution"

    def __init__(self, gate: RiskGate, *, live: bool = False,
                 dispatcher: Any = None) -> None:
        super().__init__()
        self.capability("dispatch", "submit approved actions to the broker")
        self.capability("shadow", "record actions without broker submission")
        self.gate = gate
        self.live = live
        self.dispatcher = dispatcher

    def act(self, context: AgentContext) -> AgentResult:
        action = context.extras.get("action")
        if action is None:
            return AgentResult(agent=self.name, role=self.role, ok=True,
                               summary="no_action_to_execute")
        if not isinstance(action, AgentAction):
            action = AgentAction(**action) if isinstance(action, dict) else None
        if action is None:
            return AgentResult(agent=self.name, role=self.role, ok=False,
                               summary="bad_action", error="action_not_parsed")

        gate = context.extras.get("gate_result", {})
        verdict = gate.get("verdict", Verdict.SHADOW.value)

        # Hard rule: never execute an action the gate did not approve.
        if verdict not in (Verdict.APPROVE.value, Verdict.SHADOW.value):
            return AgentResult(
                agent=self.name, role=self.role, ok=True,
                summary=f"blocked_by_gate:{verdict}",
                data={"executed": False, "verdict": verdict},
            )

        if not self.live:
            # Shadow mode: record the action as if it were dispatched,
            # but produce a simulated receipt. No broker contact.
            receipt = self._shadow_receipt(action)
            context.extras["execution_receipt"] = receipt
            return AgentResult(
                agent=self.name, role=self.role, ok=True,
                summary=f"shadow_recorded:{action.kind}",
                data={"executed": False, "mode": "shadow",
                      "receipt": receipt},
            )

        # Live mode: dispatch through the native single-writer dispatcher.
        receipt = self._dispatch_live(action, context)
        context.extras["execution_receipt"] = receipt
        return AgentResult(
            agent=self.name, role=self.role, ok=bool(receipt.get("ok", False)),
            summary=f"dispatched:{action.kind}" if receipt.get("ok")
                    else f"dispatch_failed:{receipt.get('error', '?')}",
            data={"executed": bool(receipt.get("ok", False)),
                  "mode": "live", "receipt": receipt},
        )

    # ------------------------------------------------------------------ #
    def _shadow_receipt(self, action: AgentAction) -> Dict[str, Any]:
        return {
            "ok": True,
            "mode": "shadow",
            "action_id": action.action_id,
            "symbol": action.symbol,
            "kind": action.kind,
            "direction": action.direction.value,
            "note": "recorded only; broker not contacted",
        }

    def _dispatch_live(self, action: AgentAction,
                       context: AgentContext) -> Dict[str, Any]:
        """Dispatch a live action through the V9 dispatcher.

        Delegates to the injected ``dispatcher`` (the native single-writer
        dispatcher) when available. The dispatcher is the only component
        that talks to the broker; this agent never constructs raw broker
        calls itself.
        """
        if self.dispatcher is None:
            return {"ok": False, "error": "NO_DISPATCHER"}
        try:
            method = getattr(self.dispatcher, "submit", None) \
                or getattr(self.dispatcher, "dispatch", None)
            if method is None:
                return {"ok": False, "error": "DISPATCHER_NO_SUBMIT"}
            receipt = method(action.to_dict(), context=context.to_dict())
            return receipt if isinstance(receipt, dict) \
                else {"ok": True, "receipt": receipt}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"DISPATCH:{type(exc).__name__}"}
