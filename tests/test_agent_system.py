"""Tests for the ABUTRON FX agentic hybrid multi-agent system.

Covers the full sense -> think -> gate -> act -> monitor -> recover
pipeline, the pluggable reasoning backends, the unbypassable RiskGate,
the brain substrate fallback, and the evidence ledger. All tests run
offline with the deterministic fallback — no brain checkpoint, LLM
gateway or broker is required.
"""
from __future__ import annotations

import unittest
from typing import Any, Dict

from abutron.agent import (
    AgentContext,
    AgentOrchestrator,
    Direction,
    LLMReasoningBackend,
    RuleReasoningBackend,
    RiskGate,
    Verdict,
    build_agent_system,
)
from abutron.agent.interfaces import AgentAction, AgentDecision, Signal
from abutron.agent.memory import DecisionLedger
from abutron.agent.substrate import BrainSubstrate


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def make_signal(direction=Direction.BUY, conviction=0.8, confidence=0.7,
                uncertainty=0.2, source="test"):
    return Signal(symbol="V50", direction=direction, conviction=conviction,
                  confidence=confidence, uncertainty=uncertainty,
                  position_size=0.5, regime="trend", brain_source=source,
                  reasons=["test"])


def make_injected_substrate(signal=None):
    """A BrainSubstrate whose predict() returns a fixed signal."""
    sub = BrainSubstrate(brain=None)
    sig = signal or make_signal()
    sub.predict = lambda ctx, features=None: sig
    return sub


def make_context(**overrides):
    ctx = AgentContext(
        symbol="V50", bar_open=100.0, bar_close=100.20, high=100.30,
        low=99.90, spread=0.002,
        orderflow={"imbalance": 0.20, "buy_ratio": 0.70},
        portfolio={"equity": 10000.0},
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


# ---------------------------------------------------------------------- #
class ReasoningBackendTests(unittest.TestCase):
    def test_rule_backend_holds_on_low_edge(self):
        backend = RuleReasoningBackend()
        ctx = make_context()
        sig = make_signal(conviction=0.1, confidence=0.1, uncertainty=0.5)
        r = backend.reason(ctx, sig)
        self.assertEqual(r["signal"], Direction.HOLD)
        self.assertIn("low_edge_hold", r["considerations"])

    def test_rule_backend_uncertainty_veto(self):
        backend = RuleReasoningBackend()
        ctx = make_context()
        sig = make_signal(conviction=0.9, confidence=0.9, uncertainty=0.99)
        r = backend.reason(ctx, sig)
        self.assertEqual(r["signal"], Direction.HOLD)
        self.assertIn("uncertainty_veto", r["considerations"])

    def test_rule_backend_acts_on_strong_signal(self):
        backend = RuleReasoningBackend()
        ctx = make_context()
        sig = make_signal(conviction=0.8, confidence=0.7, uncertainty=0.2)
        r = backend.reason(ctx, sig)
        self.assertEqual(r["signal"], Direction.BUY)
        self.assertGreater(r["confidence"], 0.20)

    def test_rule_backend_handles_no_signal(self):
        backend = RuleReasoningBackend()
        r = backend.reason(make_context(), None)
        self.assertEqual(r["signal"], Direction.HOLD)
        self.assertIn("no_brain_signal", r["considerations"])

    def test_llm_backend_degrades_to_rule_when_disabled(self):
        backend = LLMReasoningBackend(base_url="", enabled=False)
        self.assertFalse(backend.is_configured())
        # or_fallback must return the rule backend when not configured.
        effective = backend.or_fallback()
        self.assertIsInstance(effective, RuleReasoningBackend)
        r = backend.reason(make_context(), make_signal())
        # Degrades gracefully — no crash, returns a HOLD/rule synthesis.
        self.assertIn("signal", r)

    def test_llm_backend_fallback_uses_rule_on_unavailable(self):
        # Configured but gateway unreachable -> degrade to rule synthesis.
        backend = LLMReasoningBackend(base_url="http://127.0.0.1:1",
                                      enabled=True, debate_enabled=False)
        r = backend.reason(make_context(), make_signal())
        # Must not raise; returns a synthesis (rule fallback).
        self.assertIn("signal", r)
        self.assertTrue(r["backend"].startswith("llm"))


# ---------------------------------------------------------------------- #
class RiskGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = RiskGate(portfolio={"equity": 10000.0})

    def _valid_action(self):
        # price 100.10, stop 100.00 (risk 0.10), target 100.30 (reward 0.20)
        # -> R:R 2.0, passes the 1.5 floor.
        return AgentAction.new(
            "submit_order", "V50", Direction.BUY, quantity=0.1,
            stop_loss=100.0, take_profit=100.30, price=100.10)

    def test_approves_valid_action(self):
        res = self.gate.evaluate(self._valid_action(), make_context())
        self.assertEqual(res.verdict, Verdict.APPROVE)
        self.assertTrue(res.approved)

    def test_rejects_bad_reward_risk(self):
        action = AgentAction.new(
            "submit_order", "V50", Direction.BUY, quantity=0.1,
            stop_loss=100.0, take_profit=100.05, price=100.10)  # R:R 0.5
        res = self.gate.evaluate(action, make_context())
        self.assertEqual(res.verdict, Verdict.REJECT)
        self.assertIn("REWARD_RISK_BELOW_FLOOR", res.reason)

    def test_rejects_missing_stop(self):
        action = AgentAction.new(
            "submit_order", "V50", Direction.BUY, quantity=0.1,
            stop_loss=0.0, take_profit=100.15, price=100.10)
        res = self.gate.evaluate(action, make_context())
        self.assertEqual(res.verdict, Verdict.REJECT)
        self.assertIn("MISSING_STOP_LOSS", res.reason)

    def test_rejects_hold_direction(self):
        action = AgentAction.new(
            "submit_order", "V50", Direction.HOLD, quantity=0.1,
            stop_loss=100.0, take_profit=100.15, price=100.10)
        res = self.gate.evaluate(action, make_context())
        self.assertEqual(res.verdict, Verdict.REJECT)
        self.assertIn("HOLD_NO_POSITION", res.reason)

    def test_rejects_per_trade_risk_exceeded(self):
        # Valid R:R (risk 1.0, reward 2.0 -> 2.0) but stop_risk_usd far
        # above the 0.15% per-trade cap of 10000 equity (= 15 USD).
        action = AgentAction.new(
            "submit_order", "V50", Direction.BUY, quantity=0.1,
            stop_loss=99.0, take_profit=102.0, price=100.0,
            payload={"stop_risk_usd": 1000.0})
        res = self.gate.evaluate(action, make_context())
        self.assertEqual(res.verdict, Verdict.REJECT)
        # Either the V9 RiskGuard (RISKGUARD_RISK_CAP) or the independent
        # hard-invariant layer (PER_TRADE_RISK_EXCEEDED) catches it.
        self.assertTrue(
            "RISK_CAP" in res.reason or "PER_TRADE_RISK_EXCEEDED" in res.reason,
            f"unexpected reason: {res.reason}")

    def test_read_only_action_skips_money_checks(self):
        action = AgentAction.new("record", "V50", Direction.HOLD,
                                 requires_gate=False)
        res = self.gate.evaluate(action, make_context())
        self.assertEqual(res.verdict, Verdict.SHADOW)

    def test_gate_enforces_even_without_guard(self):
        # Build a gate with no RiskGuard available (force guard unavailable).
        gate = RiskGate.__new__(RiskGate)
        gate._guard = None
        gate._guard_available = False
        gate._portfolio = {}
        gate.per_trade_pct = 0.0015
        gate.portfolio_cap_pct = 0.20
        # Bad R:R still rejected by the independent hard check.
        action = AgentAction.new(
            "submit_order", "V50", Direction.BUY, quantity=0.1,
            stop_loss=100.0, take_profit=100.05, price=100.10)
        res = gate.evaluate(action, make_context())
        self.assertEqual(res.verdict, Verdict.REJECT)


# ---------------------------------------------------------------------- #
class BrainSubstrateTests(unittest.TestCase):
    def test_rule_fallback_when_no_brain(self):
        sub = BrainSubstrate(brain=None)
        # Force the load attempt to fail cleanly.
        sub._load_error = "BRAIN_LOAD:NoModule"
        sub._loaded = False
        sig = sub.predict(make_context())
        self.assertEqual(sig.brain_source, "rule_fallback")
        self.assertIn("rule_fallback", sig.reasons[0])

    def test_injected_brain_path(self):
        sub = BrainSubstrate(brain=None)
        expected = make_signal(Direction.SELL, 0.6, 0.6, 0.3, "nexus")
        sub.predict = lambda ctx, features=None: expected
        sig = sub.predict(make_context())
        self.assertEqual(sig.direction, Direction.SELL)
        self.assertEqual(sig.brain_source, "nexus")


# ---------------------------------------------------------------------- #
class OrchestratorEndToEndTests(unittest.TestCase):
    def test_full_cycle_approves_and_shadows(self):
        system = build_agent_system(
            live=False, brain_substrate=make_injected_substrate())
        d = system.decide(make_context())
        self.assertEqual(d.verdict, Verdict.APPROVE)
        self.assertFalse(d.executed)  # shadow mode: no broker
        self.assertEqual(d.action.kind, "submit_order")
        self.assertEqual(d.action.direction, Direction.BUY)
        # All six stages present in the trace.
        stages = [t["stage"] for t in d.agent_trace]
        self.assertEqual(stages, ["sense", "think", "gate", "act",
                                  "monitor", "recover"])
        # Ledger recorded the decision.
        self.assertEqual(system.ledger().count, 1)

    def test_gate_rejection_blocks_execution(self):
        # Inject a signal that produces a bad-R:R action by forcing a
        # HOLD-less path: use a SELL with a stop/target that fails R:R.
        # Simpler: build the system, then verify a directly-built bad
        # action is blocked by injecting it via the risk agent path.
        system = build_agent_system(
            live=False, brain_substrate=make_injected_substrate())
        ctx = make_context()
        # Force the orchestrator to build an action, then override the
        # action to a bad one before the gate runs.
        orig_think = system.think_agent.act
        def think_and_inject(context):
            res = orig_think(context)
            context.extras["action"] = AgentAction.new(
                "submit_order", "V50", Direction.BUY, quantity=0.1,
                stop_loss=100.0, take_profit=100.05, price=100.10)
            return res
        system.think_agent.act = think_and_inject
        d = system.decide(ctx)
        self.assertEqual(d.verdict, Verdict.REJECT)
        self.assertFalse(d.executed)

    def test_low_edge_signal_holds(self):
        sig = make_signal(conviction=0.1, confidence=0.1, uncertainty=0.5)
        system = build_agent_system(
            live=False, brain_substrate=make_injected_substrate(sig))
        d = system.decide(make_context())
        # Reasoning holds -> record-only action, no order.
        self.assertIn(d.verdict, (Verdict.SHADOW, Verdict.APPROVE))
        self.assertFalse(d.executed)

    def test_live_mode_requires_dispatcher(self):
        # Live mode with no dispatcher: gate approves but execution fails
        # gracefully (no dispatcher) rather than crashing.
        system = build_agent_system(
            live=True, brain_substrate=make_injected_substrate())
        d = system.decide(make_context())
        self.assertEqual(d.verdict, Verdict.APPROVE)
        # Execution attempted but no dispatcher -> not executed.
        self.assertFalse(d.executed)

    def test_fail_soft_on_stage_error(self):
        system = build_agent_system(
            live=False, brain_substrate=make_injected_substrate())
        # Make the signal agent raise; the cycle must not abort.
        system.signal_agent.act = lambda ctx: (_ for _ in ()).throw(
            RuntimeError("boom"))
        d = system.decide(make_context())
        # Recovery quarantined the failed cycle; decision still produced.
        sense_trace = next(t for t in d.agent_trace if t["stage"] == "sense")
        self.assertFalse(sense_trace["ok"])
        self.assertTrue(any(t["stage"] == "recover" for t in d.agent_trace))

    def test_decision_ledger_summary(self):
        ledger = DecisionLedger()
        system = build_agent_system(
            live=False, brain_substrate=make_injected_substrate(),
            ledger=ledger)
        for _ in range(3):
            system.decide(make_context())
        summary = ledger.summary()
        self.assertEqual(summary["total_decisions"], 3)
        self.assertEqual(summary["executed"], 0)


if __name__ == "__main__":
    unittest.main()
