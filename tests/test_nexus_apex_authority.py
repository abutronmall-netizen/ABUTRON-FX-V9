"""Regression tests for V9 NexusBrain authority in APEX signal routing."""
from __future__ import annotations

import json
from types import SimpleNamespace

from abutron.brain.apex_authority import NexusSignalAuthority
from abutron.brain.features import FEATURE_VERSION, N_FEATURES


def _candle(i: int, sym="EURUSD", base=1.1):
    t = 1800000000 + 300 * i
    p = base + i * 0.0001
    return {"symbol": sym, "close_time_epoch": t,
            "close": p, "high": p + 0.0002, "low": p - 0.0002}


def test_model_missing_cannot_trade(tmp_path):
    a = NexusSignalAuthority(str(tmp_path))
    assert a.brain is None
    for i in range(35):
        r = a.decide(connection_id="demo1", candle=_candle(i))
    assert r["status"] == "BRAIN_MODEL_UNAVAILABLE"
    assert "MODEL_MISSING" in r["reason"]
    assert not r.get("proposal")


def test_random_weights_without_training_evidence_rejected(tmp_path):
    (tmp_path / "nexus_brain.json").write_text(json.dumps({
        "feature_version": FEATURE_VERSION, "n_features": N_FEATURES,
        "members": [{"name": "random", "state": {}}],
        "report": None, "calibration": {"fitted": False}}))
    a = NexusSignalAuthority(str(tmp_path))
    assert a.brain is None
    assert "TRAINING_EVIDENCE_MISSING" in a.load_reason


def test_feature_version_mismatch_rejected(tmp_path):
    (tmp_path / "nexus_brain.json").write_text(json.dumps({
        "feature_version": "old_v0", "n_features": N_FEATURES,
        "report": {"calibrated": True, "n_train": 50, "n_val": 20}}))
    a = NexusSignalAuthority(str(tmp_path))
    assert a.brain is None
    assert "FEATURE_SCHEMA_MISMATCH" in a.load_reason


def test_history_is_per_account_and_gap_resets(tmp_path):
    a = NexusSignalAuthority(str(tmp_path))
    class FakeBrain:
        def decide(self, *, symbol, closes, **kwargs):
            return SimpleNamespace(direction="BUY", confidence=0.7,
                    conviction=0.5, uncertainty=0.2, rationale="fixture",
                    to_dict=lambda: {"direction": "BUY", "trained": True})
    a.brain = FakeBrain()
    a.model_digest = "a" * 64
    for i in range(30):
        r = a.decide(connection_id="demo1", candle=_candle(i))
    assert r["status"] == "BUY"
    assert r["model_sha256"] == "a" * 64
    other = a.decide(connection_id="demo2", candle=_candle(29))
    assert other["status"] == "BRAIN_FEATURE_WARMUP"
    gap = a.decide(connection_id="demo1", candle=_candle(45))
    assert gap["status"] == "BRAIN_FEATURE_WARMUP"
    duplicate = a.decide(connection_id="demo1", candle=_candle(45))
    assert duplicate["status"] == "BRAIN_INVALID_BAR"


def test_non_finite_brain_proposal_cannot_trade(tmp_path):
    a = NexusSignalAuthority(str(tmp_path))
    class BadBrain:
        def decide(self, **kwargs):
            return SimpleNamespace(direction="BUY", confidence=float("nan"),
                    conviction=0.8, uncertainty=0.2, rationale="bad")
    a.brain = BadBrain()
    for i in range(30):
        r = a.decide(connection_id="demo1", candle=_candle(i))
    assert r["status"] == "BRAIN_INVALID_PROPOSAL"
