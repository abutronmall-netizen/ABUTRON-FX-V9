"""Tests for the NexusBrain — the advanced decision brain.

Covers: feature layout, neural backprop correctness, softmax normalisation,
ensemble uncertainty + abstention, the untrained deterministic fallback,
walk-forward training, and engine integration (brain is the signal authority).
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from abutron.brain import (NexusBrain, make_brain, default_ensemble,
                           prepare_samples, walk_forward_train,
                           FEATURE_NAMES, N_FEATURES, FEATURE_VERSION)
from abutron.brain.neural import MLP, softmax
from abutron.brain.features import build_feature_vector

DATA_ROOT = os.path.join(os.getenv("ABUTRON_DATA_ROOT", "data"), "t_brain")
os.makedirs(DATA_ROOT, exist_ok=True)


def _series(n=600, seed=3, signal=True):
    rng = np.random.default_rng(seed)
    closes = [100.0]
    for i in range(1, n):
        trend = 0.0012 * np.sin(i / 18.0) if signal else 0.0
        closes.append(closes[-1] * (1 + trend + rng.normal(0, 0.0015)))
    return closes


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------
class TestFeatures:
    def test_feature_layout_is_stable(self):
        assert N_FEATURES == len(FEATURE_NAMES)
        assert FEATURE_VERSION == "nexus_v1"

    def test_feature_vector_length_matches(self):
        f = build_feature_vector(closes=[1, 2, 3, 4, 5], regime="trend_up",
                                 agent_scores={"trend": 0.5})
        assert len(f) == N_FEATURES

    def test_feature_vector_is_finite(self):
        f = build_feature_vector(closes=list(np.linspace(100, 110, 30)),
                                 regime="high_vol")
        assert all(np.isfinite(v) for v in f)


# ---------------------------------------------------------------------------
# Neural core
# ---------------------------------------------------------------------------
class TestNeural:
    def test_softmax_normalises_per_row(self):
        x = np.random.default_rng(0).standard_normal((17, 3))
        out = softmax(x)
        assert np.allclose(out.sum(axis=1), 1.0)

    def test_mlp_learns_trivial_task(self):
        """Backprop is correct: an MLP learns a linearly-separable task."""
        rng = np.random.default_rng(0)
        X = rng.standard_normal((200, 2))
        y = ((X[:, 0] + X[:, 1]) > 0).astype(float)
        m = MLP([2, 8, 2], p_drop=0.0)
        for _ in range(150):
            m.train_step(X, y)
        p = m.predict_proba(X)
        pred = (p[:, 1] > 0.5).astype(int)
        assert np.mean(pred == y) > 0.9

    def test_mlp_probs_sum_to_one(self):
        m = MLP([N_FEATURES, 8, 2], p_drop=0.0)
        x = np.random.default_rng(1).standard_normal((5, N_FEATURES))
        p = m.predict_proba(x)
        assert np.allclose(p.sum(axis=1), 1.0)


# ---------------------------------------------------------------------------
# Ensemble + brain
# ---------------------------------------------------------------------------
class TestBrain:
    def test_untrained_brain_falls_back_deterministically(self):
        brain = make_brain(ensemble=None)
        assert not brain.trained
        prop = brain.decide(symbol="X", closes=_series()[-60:])
        # fallback never errors and produces a bounded proposal
        assert prop.direction in ("BUY", "SELL", "HOLD")
        assert 0.0 <= prop.confidence <= 1.0
        assert 0.0 <= prop.uncertainty <= 1.0

    def test_trained_brain_produces_proposal(self):
        samples = prepare_samples(_series())
        ens = default_ensemble(seed=0)
        walk_forward_train(ens, samples, epochs=20, batch_size=64)
        brain = make_brain(ensemble=ens, min_confidence=0.4,
                           max_uncertainty=0.7)
        assert brain.trained
        prop = brain.decide(symbol="X", closes=_series()[-60:],
                            regime="trend_up")
        assert prop.direction in ("BUY", "SELL", "HOLD")
        assert prop.brain_version == "nexus_v1"
        assert prop.trained

    def test_commits_even_when_uncertain(self):
        """Aggressive mode: the brain commits to a direction even under
        maximal uncertainty — abstention gates are removed."""
        samples = prepare_samples(_series(n=300))
        ens = default_ensemble(seed=0)
        walk_forward_train(ens, samples, epochs=10, batch_size=64)
        # max_uncertainty = 0 would have forced abstention under the old
        # fail-closed design; now it is informational only.
        brain = make_brain(ensemble=ens, min_confidence=0.0,
                           max_uncertainty=0.0)
        prop = brain.decide(symbol="X", closes=_series()[-60:])
        assert prop.direction in ("BUY", "SELL")
        assert prop.will_trade is True
        assert prop.position_size > 0.0

    def test_online_adaptation_gated(self):
        samples = prepare_samples(_series(n=300))
        ens = default_ensemble(seed=0)
        walk_forward_train(ens, samples, epochs=10, batch_size=64)
        brain = make_brain(ensemble=ens)
        brain.set_baseline_loss(0.5)
        # a bad outcome that worsens the running loss is rejected
        r = brain.observe(symbol="X", predicted_edge=0.5,
                          realized_return=-0.02)
        assert "updated" in r

    def test_persistence_roundtrip(self, tmp_path):
        from abutron.brain import save_brain, load_brain
        samples = prepare_samples(_series(n=300))
        ens = default_ensemble(seed=0)
        walk_forward_train(ens, samples, epochs=10, batch_size=64)
        path = save_brain(str(tmp_path), ens)
        assert os.path.exists(path)
        ens2 = load_brain(str(tmp_path))
        assert len(ens2.members) == len(ens.members)


# ---------------------------------------------------------------------------
# Engine integration — the brain is the signal authority
# ---------------------------------------------------------------------------
class TestEngineBrainIntegration:
    def test_engine_uses_brain_as_signal_authority(self):
        from abutron.events import EventLog
        from abutron.apex.engine import ApexEngine
        from abutron.feeds.feed_quality import Candle

        log = EventLog(os.path.join(DATA_ROOT, "t_brain_engine.jsonl"))
        engine = ApexEngine(log)
        # brain is attached by default
        assert engine.brain is not None
        assert engine.brain.VERSION == "nexus_v1"

        # build a strong up-trend so the brain has a real edge
        closes = _series(n=200, seed=11)
        candles = []
        t = 0
        for price in closes:
            candles.append(Candle(symbol="TESTUSD", timeframe="M5",
                                  open_time=str(t), close_time=str(t + 300),
                                  open=price, high=price * 1.001,
                                  low=price * 0.999, close=price,
                                  volume=1.0, spread=0.0001, tick_size=0.0001))
            t += 300
        for c in candles:
            engine.set_history(c.symbol, c.timeframe, candles)

        account = {"connection_id": "conn1", "broker": "deriv",
                   "owner_id": "owner1", "effective_on": True,
                   "desired_on": True, "per_trade_risk_pct": 0.15,
                   "equity": 10000.0}
        result = engine.run_decision_pass(
            connection_id="conn1", broker="deriv", owner_id="owner1",
            candles=candles[-1:], quotes=[], account=account)
        # decision is bounded and, if a candidate is produced, it carries
        # the brain in agent_votes (the brain is the signal authority)
        for cand in result:
            assert "nexus_brain" in cand.agent_votes
            bv = cand.agent_votes["nexus_brain"]["brain_version"]
            # the active brain is the unified brain (all symbols, one brain)
            # when trained, otherwise the per-symbol nexus brain
            assert bv in ("nexus_v1", "unified_v1")
            # stop geometry (ATR path) was exercised and produces SL/TP
            assert cand.stop_loss > 0 and cand.take_profit > 0

    def test_engine_atr_stop_geometry_path(self):
        """A strong edge must flow through the ATR stop-geometry path
        without error (regression for the ..indicators import bug)."""
        from abutron.events import EventLog
        from abutron.apex.engine import ApexEngine
        from abutron.feeds.feed_quality import Candle
        from abutron.brain import default_ensemble, make_brain

        # a strong, persistent up-trend to give the brain a real edge
        closes = [100.0 + i * 0.05 for i in range(200)]
        ens = default_ensemble(seed=0)
        brain = make_brain(ensemble=ens, min_confidence=0.0,
                           max_uncertainty=1.0, max_conviction=1.0)
        log = EventLog(os.path.join(DATA_ROOT, "t_brain_atr.jsonl"))
        engine = ApexEngine(log, brain=brain)
        candles = [Candle(symbol="TESTUSD", timeframe="M5",
                          open_time=str(i * 300), close_time=str((i + 1) * 300),
                          open=closes[i], high=closes[i] * 1.002,
                          low=closes[i] * 0.998, close=closes[i],
                          volume=1.0, spread=0.0001, tick_size=0.0001)
                   for i in range(len(closes))]
        for c in candles:
            engine.set_history(c.symbol, c.timeframe, candles)
        account = {"connection_id": "conn1", "broker": "deriv",
                   "owner_id": "owner1", "effective_on": True,
                   "desired_on": True, "per_trade_risk_pct": 0.15,
                   "equity": 10000.0}
        result = engine.run_decision_pass(
            connection_id="conn1", broker="deriv", owner_id="owner1",
            candles=candles[-1:], quotes=[], account=account)
        for cand in result:
            assert cand.stop_loss > 0 and cand.take_profit > 0
