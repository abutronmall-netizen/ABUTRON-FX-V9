"""Issue #12 follow-up: prevent 49-vs-23 trained unified brain regressions."""
from __future__ import annotations
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from abutron.brain.unified_adapter import Unified49Brain, load_unified_49
from abutron.brain.signals import latest_signal_features


class PredictCapture:
    def __init__(self):
        self.members = [SimpleNamespace(model=SimpleNamespace(sizes=[49, 2]))]
        self._fitted = True
        self.seen = None

    def predict(self, x):
        self.seen = np.asarray(x)
        return SimpleNamespace(
            direction="BUY", conviction=.6, confidence=.7,
            uncertainty=.2, votes={"member": .6}, members=1)


def _brain():
    ens = PredictCapture()
    syms = [f"SYM_{i}" for i in range(26)]
    brain = Unified49Brain(
        ensemble=ens, symbols=syms,
        trade_memory={}, loss_guard={}, online={"updates": 99})
    return brain, ens, syms


def test_unified_decision_uses_49_features_and_exact_symbol_onehot():
    brain, ensemble, syms = _brain()
    prices = [100. + i * .05 for i in range(65)]
    result = brain.decide(
        symbol=syms[17], closes=prices,
        highs=[v + .05 for v in prices],
        lows=[v - .05 for v in prices])
    assert result.trained and result.direction == "BUY"
    assert ensemble.seen.shape == (1, 49)
    assert ensemble.seen[0, 23:].tolist() == [
        float(i == 17) for i in range(26)]
    assert brain.online_updates == 99


def test_loss_pattern_hold_blocks_unified_candidate():
    brain, _, syms = _brain()
    brain.loss_guard.should_block = lambda **kw: (
        True, "LOSS_PATTERN_REPEAT:3_losses:-0.4")
    result = brain.decide(
        symbol=syms[3], closes=[100. + .02 * i for i in range(65)])
    assert result.trained and result.direction == "HOLD"
    assert result.position_size == 0
    assert result.blockers and result.blockers[0].startswith("LOSS_PATTERN_REPEAT")


def test_unknown_symbol_does_not_reuse_other_instrument_onehot():
    brain, ensemble, _ = _brain()
    result = brain.decide(
        symbol="UNKNOWN", closes=[100. + .02 * i for i in range(65)])
    assert result.direction == "HOLD"
    assert "SYMBOL_NOT_IN_TRAINED_UNIFIED_CHECKPOINT" in result.blockers
    assert ensemble.seen is None


def test_full_signal_proxy_populates_learned_slots():
    candles = [
        dict(open_time=str(i * 300), open=100. + i * .07,
             high=100. + i * .07 + .17,
             low=100. + i * .07 - .08,
             close=100. + i * .07 + .03,
             volume=0.) for i in range(65)
    ]
    snap = latest_signal_features(candles, symbol="Boom 1000 Index.0",
                                  use_gemma=False)
    assert len(snap["market_features"]) == 23
    assert sum(abs(float(snap["market_features"][i])) > 1e-12
               for i in (10, 11, 12, 13, 14, 19, 20, 21, 22)) == 9


def test_external_unified_checkpoint_load_and_inference():
    directory = os.getenv("ABUTRON_BRAIN_DIR", "")
    if not directory or not (Path(directory) / "nexus_brain.json").exists():
        pytest.skip("real trained unified checkpoint not linked in CI")
    brain = load_unified_49(directory)
    assert brain.trained and len(brain.symbol_ids) == 26
    assert brain.ensemble.members[0].model.sizes[0] == 49
    assert brain.loss_guard.stats()["active_loss_patterns"] > 0
    closes = [100. + .08 * i for i in range(65)]
    p = brain.decide(
        symbol="Crash 1000 Index.0",
        closes=closes,
        highs=[v + .2 for v in closes],
        lows=[v - .2 for v in closes])
    assert p.trained and p.brain_version == "unified_v1"
    assert p.direction in ("BUY", "SELL", "HOLD")
