"""Tests for the agentic engine integration into ApexIntegration.

Validates that when AGENTIC_ENGINE_ENABLED is on,
``ApexIntegration.process_candle`` routes through the agentic
multi-agent system (sense -> think -> gate -> act -> monitor -> recover)
instead of the vendored apex bridge, and produces the same candidate
dict format downstream consumers expect.

Runs offline with the deterministic reasoning backend and a real
unified brain checkpoint (trained on synthetic data in the test setup).
No brokers, no orders, no production services.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any, Dict

from abutron.agent.production import AgenticDecisionEngine
from abutron.agent.substrate import BrainSubstrate
from abutron.agent.reasoning import RuleReasoningBackend
from abutron.agent.interfaces import Direction, Signal
from abutron.brain.unified import (
    SYMBOL_UNIVERSE, UnifiedBrain, build_unified_ensemble,
    build_unified_feature_vector, save_unified, train_unified,
)
from abutron.brain.trainer import TrainingSample


def _train_real_brain(brain_dir: str) -> str:
    """Train + save a small unified brain checkpoint."""
    os.makedirs(brain_dir, exist_ok=True)
    import numpy as np
    universe = list(SYMBOL_UNIVERSE[:5])
    ensemble = build_unified_ensemble(universe=universe)
    symbol_samples = {}
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


def _make_candle_dict(symbol: str = "EURUSD.r") -> Dict[str, Any]:
    """Build a candle dict matching the platform's input format."""
    return {
        "symbol": symbol,
        "open_time": "2026-11-18T10:00:00Z",
        "close_time": "2026-11-18T10:05:00Z",
        "close_time_epoch": 1700000000.0,
        "open": 1.10, "high": 1.103, "low": 1.099, "close": 1.102,
        "volume": 100.0, "spread": 0.0001, "point": 0.0001,
        "tick_size": 0.00001,
    }


def _make_account() -> Dict[str, Any]:
    return {
        "broker": "fp_markets",
        "owner_id": "owner1",
        "equity": 10000.0,
        "per_trade_risk_pct": 0.0015,
        "desired_on": 1, "effective_on": 1,
    }


class AgenticIntegrationTests(unittest.TestCase):
    """Validates the agentic engine wiring into ApexIntegration."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.brain_dir = os.path.join(cls._tmp.name, "brain")
        _train_real_brain(cls.brain_dir)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _make_integration(self):
        """Build an ApexIntegration with the agentic engine enabled."""
        from abutron.events import EventLog
        from abutron.apex_integration import ApexIntegration
        # Patch the config flag at the instance level by building the
        # agentic engine manually and injecting it.
        log_path = os.path.join(self._tmp.name, "eventlog.jsonl")
        integration = ApexIntegration(EventLog(log_path))
        from abutron.agent.substrate import BrainSubstrate
        from abutron.agent.reasoning import RuleReasoningBackend
        sub = BrainSubstrate(brain_dir=self.brain_dir)
        engine = AgenticDecisionEngine(
            brain_substrate=sub, reasoning=RuleReasoningBackend(),
            live=False)
        integration._agentic_engine = engine
        return integration

    def test_process_candle_uses_agentic_engine(self):
        """process_candle routes through the agentic engine when set."""
        integration = self._make_integration()
        self.assertIsNotNone(integration._agentic_engine)
        result = integration.process_candle(
            connection_id="a" * 32,
            account=_make_account(),
            candle=_make_candle_dict(),
            manifest=[],
            equity=10000.0)
        # The result must be one of the standard statuses.
        self.assertIn(result["status"],
                      ("READY", "HOLD", "BLOCKED", "AGENTIC_HOLD"))
        # If READY, the candidate must have the expected format.
        if result["status"] == "READY":
            cand = result["candidate"]
            self.assertIn("signal_id", cand)
            self.assertIn("side", cand)
            self.assertIn("symbol", cand)
            self.assertIn("volume_lots", cand)
            self.assertEqual(cand["symbol"], "EURUSD.r")

    def test_process_candle_agentic_engine_is_authority(self):
        """The agentic engine is the decision authority (not apex)."""
        integration = self._make_integration()
        # The apex vendor is not present in the sandbox, so without the
        # agentic engine the result would be APEX_VENDOR_MISSING. With
        # the agentic engine set, we must NOT get APEX_VENDOR_MISSING.
        result = integration.process_candle(
            connection_id="b" * 32,
            account=_make_account(),
            candle=_make_candle_dict(),
            manifest=[],
            equity=10000.0)
        self.assertNotEqual(result["status"], "APEX_VENDOR_MISSING")

    def test_process_candle_returns_valid_dict_shape(self):
        """The return dict has the keys downstream consumers expect."""
        integration = self._make_integration()
        result = integration.process_candle(
            connection_id="c" * 32,
            account=_make_account(),
            candle=_make_candle_dict(),
            manifest=[],
            equity=10000.0)
        for key in ("status", "decision", "candidate", "blockers"):
            self.assertIn(key, result)

    def test_agentic_hold_when_no_gate_approved(self):
        """Fail-closed: weak signal -> no candidate, HOLD status."""
        # Build an integration with a brain that produces low-conviction
        # signals by using a fresh untrained ensemble.
        from abutron.events import EventLog
        from abutron.apex_integration import ApexIntegration
        from abutron.agent.substrate import BrainSubstrate
        from abutron.brain.unified import build_unified_ensemble
        log_path = os.path.join(self._tmp.name, "eventlog2.jsonl")
        integration = ApexIntegration(EventLog(log_path))
        # Use an untrained ensemble (random weights -> low conviction).
        ens = build_unified_ensemble(universe=list(SYMBOL_UNIVERSE[:5]))
        from abutron.brain.unified import UnifiedBrain
        brain = UnifiedBrain(ensemble=ens, symbol_ids=list(SYMBOL_UNIVERSE[:5]))
        sub = BrainSubstrate(brain=brain)
        engine = AgenticDecisionEngine(
            brain_substrate=sub,
            reasoning=RuleReasoningBackend(), live=False)
        integration._agentic_engine = engine
        result = integration.process_candle(
            connection_id="d" * 32,
            account=_make_account(),
            candle=_make_candle_dict(),
            manifest=[],
            equity=10000.0)
        # Must not produce a READY candidate from an untrained brain.
        self.assertNotEqual(result["status"], "READY")


if __name__ == "__main__":
    unittest.main()
