"""Self-learning AI acceptance tests.

Verifies the online model updates its own weights from live outcomes,
the skill learner up-weights predictive agents over noise, guardrails
clamp the overlay to hard bounds, the learner never gains execution
authority, and state persists across save/load.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "vendor", "abutron-apex", "apex11"))

from abutron.config import DATA_ROOT
from abutron.selflearn.engine import SelfLearningEngine
from abutron.selflearn.online_model import OnlineLinearModel
from abutron.selflearn.skill_learner import SkillLearner
from abutron.selflearn.guardrails import SelfLearnGuardrails
from abutron.selflearn.features import build_feature_vector, N_FEATURES
import numpy as np


# ---------------------------------------------------------------------------
# Online model
# ---------------------------------------------------------------------------
class TestOnlineModel:
    def test_dimensions(self):
        m = OnlineLinearModel(n_features=N_FEATURES)
        feats = build_feature_vector(regime="range")
        assert len(feats) == N_FEATURES
        assert m.predict(np.asarray(feats)) == 0.0  # untrained -> init

    def test_weights_change_on_update(self):
        m = OnlineLinearModel(n_features=N_FEATURES, lr=0.1)
        feats = build_feature_vector(regime="trend_up",
                                     agent_scores={"trend": 0.8})
        w0 = m.w.copy()
        b0 = m.b
        error = m.update(np.asarray(feats), 0.001)  # positive target
        assert error == pytest.approx(0.001 - m.predict(np.asarray(feats)),
                                      abs=1e-9) or abs(error) > 0
        assert not np.allclose(m.w, w0) or not np.isclose(m.b, b0)
        assert m.updates == 1

    def test_learns_linear_relationship(self):
        """With a known linear signal, the model's prediction should align."""
        m = OnlineLinearModel(n_features=3, lr=0.1)
        # feature 0 predicts target with slope 2
        np.random.seed(0)
        for _ in range(200):
            x = np.array([np.random.uniform(-1, 1), 0.0, 0.0])
            y = 2.0 * x[0] + np.random.normal(0, 0.01)
            m.update(x, y)
        # prediction should be monotonic in feature 0
        p_lo = m.predict(np.array([-0.9, 0, 0]))
        p_hi = m.predict(np.array([0.9, 0, 0]))
        assert p_hi > p_lo

    def test_persistence_roundtrip(self):
        m = OnlineLinearModel(n_features=4, lr=0.05)
        m.update(np.array([1.0, 0.2, 0.3, 0.4]), 0.01)
        m.update(np.array([0.5, 0.1, 0.2, 0.3]), -0.01)
        d = m.to_dict()
        m2 = OnlineLinearModel.from_dict(d)
        x = np.array([0.7, 0.15, 0.25, 0.35])
        assert m2.predict(x) == pytest.approx(m.predict(x), abs=1e-6)


# ---------------------------------------------------------------------------
# Skill learner
# ---------------------------------------------------------------------------
class TestSkillLearner:
    def test_upweights_predictive_agent(self):
        sl = SkillLearner()
        np.random.seed(1)
        for _ in range(100):
            t = np.random.uniform(-1, 1)
            m = np.random.uniform(-1, 1)
            ret = 0.3 * t * 0.0005 + np.random.normal(0, 0.0001)
            sl.observe(agent="trend", symbol="X", regime="trend_up",
                       score=t, realised_ret=ret)
            sl.observe(agent="meanrev", symbol="X", regime="trend_up",
                       score=m, realised_ret=ret)
        t_trust = sl.trust(agent="trend", symbol="X", regime="trend_up")
        m_trust = sl.trust(agent="meanrev", symbol="X", regime="trend_up")
        assert t_trust > m_trust

    def test_trust_bounded(self):
        sl = SkillLearner()
        # extreme positive skill should clamp to trust_max
        for _ in range(200):
            sl.observe(agent="x", symbol="X", regime="range",
                       score=1.0, realised_ret=0.01)
        assert sl.trust(agent="x", symbol="X", regime="range") <= sl.trust_max
        # extreme negative skill should clamp to trust_min
        sl2 = SkillLearner()
        for _ in range(200):
            sl2.observe(agent="y", symbol="X", regime="range",
                        score=1.0, realised_ret=-0.01)
        assert sl2.trust(agent="y", symbol="X", regime="range") >= sl2.trust_min

    def test_persistence_roundtrip(self):
        sl = SkillLearner()
        for _ in range(20):
            sl.observe(agent="trend", symbol="X", regime="range",
                       score=0.5, realised_ret=0.001)
        d = sl.to_dict()
        sl2 = SkillLearner.from_dict(d)
        assert sl2.trust(agent="trend", symbol="X", regime="range") == \
            pytest.approx(sl.trust(agent="trend", symbol="X", regime="range"))


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------
class TestGuardrails:
    def test_clamps_global_multiplier(self):
        g = SelfLearnGuardrails()
        ov = g.build_overlay(learned_multiplier=5.0, per_symbol_multiplier={})
        assert ov.multiplier <= g.global_max
        assert ov.clipped is True

    def test_clamps_symbol_multiplier(self):
        g = SelfLearnGuardrails()
        ov = g.build_overlay(learned_multiplier=1.0,
                              per_symbol_multiplier={"X": 99.0})
        assert ov.per_symbol["X"] <= g.symbol_max
        assert ov.clipped is True

    def test_overlay_only_multiplies_weights(self):
        g = SelfLearnGuardrails()
        ov = g.build_overlay(learned_multiplier=1.2,
                              per_symbol_multiplier={"X": 1.1})
        weights = {"X": 0.2, "Y": -0.1}
        out = g.apply_to_weights(weights, ov)
        # directions preserved, magnitudes scaled
        assert out["X"] > 0 and out["X"] != 0.2
        assert out["Y"] < 0
        # no new symbols introduced
        assert set(out) == set(weights)

    def test_no_execution_authority(self):
        assert SelfLearnGuardrails.execution_safety_assertion() is True


# ---------------------------------------------------------------------------
# SelfLearningEngine end-to-end
# ---------------------------------------------------------------------------
class TestSelfLearningEngine:
    def test_learns_and_persists(self, tmp_path):
        path = str(tmp_path / "sl_state.json")
        sl = SelfLearningEngine(None, state_path=path)
        np.random.seed(3)
        for _ in range(150):
            t = np.random.uniform(-1, 1)
            m = np.random.uniform(-1, 1)
            ret = 0.4 * t * 0.0005 + np.random.normal(0, 0.0001)
            feats = build_feature_vector(regime="trend_up",
                                         agent_scores={"trend": t,
                                                       "meanrev": m})
            sl.produce_overlay(symbol="X", regime="trend_up",
                               agent_scores={"trend": t, "meanrev": m})
            sl.observe_outcome(symbol="X", features=feats, realised_ret=ret,
                               regime="trend_up",
                               agent_scores={"trend": t, "meanrev": m})
        sl.save()
        # reload in a fresh engine
        sl2 = SelfLearningEngine(None, state_path=path)
        assert sl2.load() is True
        assert sl2.observations == sl.observations
        assert sl2.model.updates == sl.model.updates
        # learned trust should be preserved
        sk = sl2.skill.skill_report()
        assert "trend" in sk

    def test_overlay_adjusts_weight(self):
        sl = SelfLearningEngine(None)
        ov = sl.produce_overlay(symbol="X", regime="range",
                                agent_scores={"trend": 0.9})
        assert 0.5 <= ov.multiplier <= 1.5
        weights = {"X": 0.2}
        out = sl.apply_overlay(weights, ov)
        assert out["X"] == pytest.approx(0.2 * ov.multiplier *
                                         ov.per_symbol.get("X", 1.0))

    def test_health_reports_no_execution_authority(self):
        sl = SelfLearningEngine(None)
        h = sl.health()
        assert h["execution_authority"] is False
        assert "model_updates" in h and "observations" in h

    def test_causal_lag_observe_then_predict(self):
        """The engine must observe the prior outcome before predicting."""
        sl = SelfLearningEngine(None)
        # first bar: predict only (no pending outcome)
        feats1 = build_feature_vector(regime="range")
        ov1 = sl.produce_overlay(symbol="X", regime="range")
        assert sl.observations == 0
        # second bar: observe first, then predict
        sl.observe_outcome(symbol="X", features=feats1, realised_ret=0.001,
                           regime="range")
        assert sl.observations == 1
        assert sl.model.updates == 1
