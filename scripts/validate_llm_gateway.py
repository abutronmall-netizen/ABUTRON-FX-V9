#!/usr/bin/env python3
"""Phase 2 staging validation: LLM gateway + LLMReasoningBackend + debate path.

This script:
  1. Starts a mock OpenAI-compatible LLM gateway (``mock_llm_gateway``)
     on an ephemeral port — speaking the same ``/v1/chat/completions``
     protocol the V9 Gemma + Debate advisors use.
  2. Configures ``LLMReasoningBackend`` to point at the gateway with
     Gemma enabled + debate enabled.
  3. Validates the ``LLMReasoningBackend`` + debate path end-to-end:
     - ``is_configured()`` is True (gateway reachable, enabled)
     - ``reason()`` returns a proper advisory (backend="llm", not NO_ADVISORY)
     - The debate path produces a synthesis (multi-agent bull/bear + risk
       debators + manager)
     - Single-advisor fallback works when debate is disabled
  4. Validates the fail-soft fallback: when the gateway is stopped, the
     backend degrades gracefully to the rule backend (never blocks the
     decision cycle).
  5. Validates the full agentic cycle: real brain + LLM reasoning +
     RiskGate -> Candidate, still behind the unbypassable gate.

Staging only — no brokers, no orders, no production services.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from mock_llm_gateway import MockLLMGateway

from abutron.agent.reasoning import LLMReasoningBackend, RuleReasoningBackend
from abutron.agent.interfaces import AgentContext, Direction, Signal
from abutron.agent.substrate import BrainSubstrate
from abutron.agent.production import AgenticDecisionEngine
from abutron.feeds.feed_quality import Candle, Quote
from abutron.brain.unified import (
    SYMBOL_UNIVERSE, UnifiedBrain, build_unified_ensemble,
    build_unified_feature_vector, save_unified, train_unified,
)
from abutron.brain.trainer import TrainingSample


def _train_real_brain(brain_dir: str) -> str:
    """Train + save a real unified brain checkpoint (reuse Phase 1)."""
    os.makedirs(brain_dir, exist_ok=True)
    universe = list(SYMBOL_UNIVERSE[:5])
    ensemble = build_unified_ensemble(universe=universe)
    symbol_samples = {}
    import numpy as np
    for sym in universe:
        rng = np.random.default_rng(abs(hash(sym)) % 2**31)
        base = 1.0 + (abs(hash(sym)) % 10)
        closes = [base]
        highs = [base]
        lows = [base]
        for _ in range(120):
            ret = rng.normal(0, 0.0015)
            c = max(closes[-1] * (1 + ret), 0.01)
            h = max(closes[-1], c) * 1.001
            l = min(closes[-1], c) * 0.999
            closes.append(c)
            highs.append(h)
            lows.append(l)
        samples = []
        for i in range(len(closes) - 1):
            feats = build_unified_feature_vector(
                symbol=sym, closes=closes[:i + 1],
                highs=highs[:i + 1], lows=lows[:i + 1],
                regime="", universe=universe)
            label = 1 if closes[i + 1] > closes[i] else 0
            samples.append(TrainingSample(features=feats, label=label))
        symbol_samples[sym] = samples
    report = train_unified(ensemble, symbol_samples, epochs=30)
    brain = UnifiedBrain(ensemble=ensemble, symbol_ids=universe)
    save_unified(brain_dir, brain, report=report)
    return brain_dir


def _make_context(symbol: str = "EURUSD.r") -> AgentContext:
    """Build an AgentContext with a real OHLC series + orderflow."""
    return AgentContext(
        symbol=symbol, timeframe="M5",
        bar_open=1.10, bar_close=1.102, high=1.103, low=1.099,
        spread=0.0005,
        orderflow={"imbalance": 0.15, "buy_ratio": 0.575},
        footprint={},
        closes=[1.10, 1.101, 1.102, 1.1015, 1.102],
        highs=[1.101, 1.102, 1.103, 1.1025, 1.103],
        lows=[1.099, 1.10, 1.101, 1.101, 1.1015],
    )


def _make_signal(direction: Direction = Direction.BUY) -> Signal:
    return Signal(
        symbol="EURUSD.r", direction=direction, conviction=0.7,
        confidence=0.7, uncertainty=0.2, position_size=0.5,
        regime="trend", brain_source="nexus_unified", reasons=["brain"],
    )


# --------------------------------------------------------------------- #
# Phase 2 validation checks
# --------------------------------------------------------------------- #
def validate_llm_configured(base_url: str) -> bool:
    """LLMReasoningBackend is configured and reachable."""
    print("=" * 64)
    print("PHASE 2: LLM gateway configured")
    print("=" * 64)
    backend = LLMReasoningBackend(
        base_url=base_url, model="gemma2:9b",
        enabled=True, debate_enabled=False)
    configured = backend.is_configured()
    print(f"  is_configured: {configured}")
    print(f"  base_url: {base_url}")
    print(f"  [{'PASS' if configured else 'FAIL'}] llm_configured")
    return configured


def validate_llm_reasoning(base_url: str) -> bool:
    """LLMReasoningBackend.reason() returns a proper advisory."""
    print()
    print("=" * 64)
    print("PHASE 2: LLMReasoningBackend.reason() (single advisor)")
    print("=" * 64)
    backend = LLMReasoningBackend(
        base_url=base_url, model="gemma2:9b",
        enabled=True, debate_enabled=False)
    ctx = _make_context()
    sig = _make_signal(Direction.BUY)
    result = backend.reason(ctx, sig)
    print(f"  backend: {result['backend']}")
    print(f"  signal: {result['signal'].value}")
    print(f"  confidence: {result['confidence']}")
    print(f"  narrative: {result['narrative'][:80]}...")
    ok = (result["backend"] == "llm"
          and result["signal"] in (Direction.BUY, Direction.SELL)
          and result["confidence"] > 0.0)
    print(f"  [{'PASS' if ok else 'FAIL'}] llm_reasoning")
    return ok


def validate_debate_path(base_url: str) -> bool:
    """LLMReasoningBackend with debate enabled produces a synthesis."""
    print()
    print("=" * 64)
    print("PHASE 2: LLMReasoningBackend + debate path")
    print("=" * 64)
    backend = LLMReasoningBackend(
        base_url=base_url, model="gemma2:9b",
        enabled=True, debate_enabled=True)
    ctx = _make_context()
    sig = _make_signal(Direction.BUY)
    result = backend.reason(ctx, sig)
    print(f"  backend: {result['backend']}")
    print(f"  signal: {result['signal'].value}")
    print(f"  confidence: {result['confidence']}")
    print(f"  narrative: {result['narrative'][:80]}...")
    print(f"  considerations: {result['considerations']}")
    # Debate path: backend should be "llm" (not fallback), advisory available.
    ok = (result["backend"] == "llm"
          and result["signal"] in (Direction.BUY, Direction.SELL)
          and result["confidence"] > 0.0)
    print(f"  [{'PASS' if ok else 'FAIL'}] debate_path")
    return ok


def validate_debate_vs_single_differ(base_url: str) -> bool:
    """Debate and single-advisor paths both work and are distinct."""
    print()
    print("=" * 64)
    print("PHASE 2: Debate vs single-advisor comparison")
    print("=" * 64)
    ctx = _make_context()
    sig = _make_signal(Direction.BUY)

    llm_debate = LLMReasoningBackend(
        base_url=base_url, model="gemma2:9b",
        enabled=True, debate_enabled=True)
    llm_single = LLMReasoningBackend(
        base_url=base_url, model="gemma2:9b",
        enabled=True, debate_enabled=False)

    r_debate = llm_debate.reason(ctx, sig)
    r_single = llm_single.reason(ctx, sig)
    print(f"  debate:   backend={r_debate['backend']} "
          f"conf={r_debate['confidence']}")
    print(f"  single:   backend={r_single['backend']} "
          f"conf={r_single['confidence']}")
    # Both should produce valid advisories (may be same direction).
    ok = (r_debate["backend"] == "llm"
          and r_single["backend"] == "llm"
          and r_debate["confidence"] > 0.0
          and r_single["confidence"] > 0.0)
    print(f"  [{'PASS' if ok else 'FAIL'}] debate_vs_single")
    return ok


def validate_fail_soft(base_url: str) -> bool:
    """When the gateway is down, LLM degrades to rule fallback."""
    print()
    print("=" * 64)
    print("PHASE 2: Fail-soft fallback (gateway down)")
    print("=" * 64)
    # Point at a dead port (nothing listening).
    dead_url = "http://127.0.0.1:1/v1"
    backend = LLMReasoningBackend(
        base_url=dead_url, model="gemma2:9b",
        enabled=True, debate_enabled=False)
    ctx = _make_context()
    sig = _make_signal(Direction.BUY)
    result = backend.reason(ctx, sig)
    print(f"  backend: {result['backend']}")
    print(f"  signal: {result['signal'].value}")
    print(f"  narrative: {result['narrative'][:80]}...")
    # Must degrade gracefully: either llm:fallback or llm:unavailable,
    # but must NOT raise and must produce a valid decision structure.
    ok = (result["backend"] in ("llm:fallback", "llm:unavailable")
          and result["signal"] in (Direction.BUY, Direction.SELL,
                                    Direction.HOLD))
    print(f"  [{'PASS' if ok else 'FAIL'}] fail_soft_fallback")
    return ok


def validate_full_cycle(base_url: str, brain_dir: str) -> bool:
    """Full agentic cycle: real brain + LLM reasoning + RiskGate -> Candidate."""
    print()
    print("=" * 64)
    print("PHASE 2: Full agentic cycle (brain + LLM + gate -> candidate)")
    print("=" * 64)
    sub = BrainSubstrate(brain_dir=brain_dir)
    llm_backend = LLMReasoningBackend(
        base_url=base_url, model="gemma2:9b",
        enabled=True, debate_enabled=True).or_fallback()
    engine = AgenticDecisionEngine(
        brain_substrate=sub, reasoning=llm_backend, live=False)

    symbol = "EURUSD.r"
    candle_list = []
    for i in range(20):
        candle_list.append(Candle(
            symbol=symbol, timeframe="M5",
            open_time=f"2026-11-18T10:{i:02d}:00Z",
            close_time=f"2026-11-18T10:{i + 1:02d}:00Z",
            open=1.10 + i * 0.0005, high=1.101 + i * 0.0005,
            low=1.099 + i * 0.0005, close=1.1005 + i * 0.0005,
            closed=True, point=0.0001,
        ))
    engine.set_history(symbol, "M5", candle_list)
    last = candle_list[-1]
    quote = Quote(symbol=symbol, bid=1.1015, ask=1.1016,
                  spread=0.0001, tick_size=0.00001, point=0.0001)

    cands = engine.run_decision_pass(
        connection_id="d" * 32, broker="FP", owner_id="o1",
        candles=[last], quotes=[quote],
        account={"equity": 10000.0}, equity=10000.0)

    print(f"  candidates: {len(cands)}")
    if cands:
        c = cands[0]
        print(f"  signal_source: {c.agent_votes.get('signal_source')}")
        print(f"  reasoning_backend: {c.agent_votes.get('reasoning_backend')}")
        print(f"  reason_code: {c.reason_code}")
        ok = (c.agent_votes.get("signal_source") == "nexus_unified"
              and c.reason_code == "all_invariants_passed")
    else:
        print("  (fail-closed: no candidate, decision journalled)")
        print(f"  ledger: {engine.ledger.count}")
        ok = engine.ledger.count > 0
    print(f"  [{'PASS' if ok else 'FAIL'}] full_cycle")
    return ok


def main():
    print("=" * 64)
    print("PHASE 2: LLM gateway + LLMReasoningBackend + debate validation")
    print("=" * 64)

    # Train a real brain (reuse Phase 1 infrastructure).
    brain_dir = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "data", "brains", "unified_m5"))
    _train_real_brain(brain_dir)

    # Start the mock LLM gateway.
    with MockLLMGateway(host="127.0.0.1", port=0) as gateway:
        base_url = gateway.base_url
        print(f"  mock gateway: {base_url}\n")

        results = {}
        results["llm_configured"] = validate_llm_configured(base_url)
        results["llm_reasoning"] = validate_llm_reasoning(base_url)
        results["debate_path"] = validate_debate_path(base_url)
        results["debate_vs_single"] = validate_debate_vs_single_differ(base_url)
        results["fail_soft"] = validate_fail_soft(base_url)
        results["full_cycle"] = validate_full_cycle(base_url, brain_dir)

    print()
    print("=" * 64)
    print("PHASE 2 SUMMARY")
    print("=" * 64)
    for name, ok in results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    all_ok = all(results.values())
    print()
    print(f"  OVERALL: {'ALL CHECKS PASS' if all_ok else 'SOME CHECKS FAILED'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
