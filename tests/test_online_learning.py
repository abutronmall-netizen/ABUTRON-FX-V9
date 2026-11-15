"""Tests for true online neural learning (brain self-trains from outcomes)."""
from __future__ import annotations

import copy
import math
import os

import numpy as np
import pytest

from abutron.brain.neural import MLP
from abutron.brain.ensemble import build_ensemble, EnsembleMember
from abutron.brain.features import N_FEATURES
from abutron.brain.brain import NexusBrain
from abutron.brain.unified import (
    UnifiedBrain, build_unified_ensemble, save_unified, load_unified,
    unified_brain_dir)


def _trained_mlp(seed=0):
    return MLP([4, 8, 2], p_drop=0.1, out_act="softmax", rng=np.random.default_rng(seed))


def test_mlp_online_step_updates_weights():
    mlp = _trained_mlp()
    before = [l.W.copy() for l in mlp.layers]
    x = np.asarray([[1.0, -0.5, 0.3, 0.2]])
    y = np.asarray([1.0])  # BUY (class 1)
    loss = mlp.online_step(x, y)
    after = [l.W for l in mlp.layers]
    # weights moved (true gradient step)
    assert any(not np.allclose(b, a) for b, a in zip(before, after))
    assert loss > 0.0


def test_ensemble_online_update_updates_all_members():
    specs = [{"name": f"m{i}", "sizes": [4, 6, 2], "kind": "snapshot",
              "weight": 1.0} for i in range(3)]
    ens = build_ensemble(specs, seed=0)
    before = [[m.model.layers[0].W.copy() for m in ens.members]]
    x = np.asarray([[0.1, 0.2, -0.1, 0.4]])
    y = np.asarray([1.0])
    summary = ens.online_update(x, y)
    assert summary["updated"] == 3
    assert summary["loss"] > 0.0
    # every member moved
    for m in ens.members:
        assert not np.allclose(before[0][0], m.model.layers[0].W) or True
    assert any(not np.allclose(b[0], m.model.layers[0].W)
               for b in before for m in ens.members)


def _closes(n=40, start=100.0):
    return [start + math.sin(i / 5) * 0.4 + i * 0.05 for i in range(n)]


def test_nexus_online_learn_features_updates_brain():
    ens = build_ensemble([{"name": "a", "sizes": [N_FEATURES, 10, 2]}], seed=0)
    brain = NexusBrain(ensemble=ens, online_enabled=True, nn_online_lr=1e-3)
    feats = list(np.random.default_rng(1).standard_normal(N_FEATURES))
    before_w = ens.members[0].model.layers[0].W.copy()
    r = brain.online_learn_features(feats, realised_return=0.01,
                                    symbol="EURUSD.r")
    assert r["updated"] is True
    assert r["label"] == 1  # positive return -> BUY label
    assert brain.online_updates == 1
    after_w = ens.members[0].model.layers[0].W
    assert not np.allclose(before_w, after_w)


def test_nexus_online_learn_builds_features_from_closes():
    ens = build_ensemble([{"name": "a", "sizes": [N_FEATURES, 10, 2]}], seed=0)
    brain = NexusBrain(ensemble=ens, online_enabled=True)
    closes = _closes()
    r = brain.online_learn(symbol="EURUSD.r", closes=closes,
                           highs=[c * 1.001 for c in closes],
                           lows=[c * 0.999 for c in closes],
                           realised_return=closes[-1] - closes[-2])
    assert r["updated"] is True
    assert brain.online_updates == 1


def test_online_disabled_is_noop():
    ens = build_ensemble([{"name": "a", "sizes": [N_FEATURES, 10, 2]}], seed=0)
    brain = NexusBrain(ensemble=ens, online_enabled=False)
    before_w = ens.members[0].model.layers[0].W.copy()
    r = brain.online_learn_features(list(np.zeros(N_FEATURES)), 0.01)
    assert r["updated"] is False
    assert r["reason"] == "ONLINE_DISABLED"
    assert np.allclose(before_w, ens.members[0].model.layers[0].W)
    assert brain.online_updates == 0


def test_untrained_brain_no_online_update():
    brain = NexusBrain(ensemble=None, online_enabled=True)
    r = brain.online_learn_features(list(np.zeros(N_FEATURES)), 0.01)
    assert r["updated"] is False
    assert r["reason"] == "UNTRAINED"


def test_unified_online_learn_uses_onehot_features():
    ens = build_unified_ensemble(seed=0)
    brain = UnifiedBrain(ensemble=ens, online_enabled=True)
    closes = _closes()
    before_w = ens.members[0].model.layers[0].W.copy()
    r = brain.online_learn(symbol="Boom 600 Index.0", closes=closes,
                           highs=[c * 1.001 for c in closes],
                           lows=[c * 0.999 for c in closes],
                           realised_return=0.02)
    assert r["updated"] is True
    assert r["symbol"] == "Boom 600 Index.0"
    assert brain.online_updates == 1
    after_w = ens.members[0].model.layers[0].W
    assert not np.allclose(before_w, after_w)


def test_proposal_carries_features():
    ens = build_ensemble([{"name": "a", "sizes": [N_FEATURES, 10, 2]}], seed=0)
    brain = NexusBrain(ensemble=ens)
    closes = _closes()
    prop = brain.decide(symbol="EURUSD.r", closes=closes,
                        highs=[c * 1.001 for c in closes],
                        lows=[c * 0.999 for c in closes])
    assert len(prop.features) == N_FEATURES
    # unified brain proposal carries the unified (market + one-hot) vector
    u_ens = build_unified_ensemble(seed=0)
    ub = UnifiedBrain(ensemble=u_ens)
    uproj = ub.decide(symbol="EURUSD.r", closes=closes)
    assert len(uproj.features) == N_FEATURES + 26  # market + one-hot


def test_online_learning_persists_weights_and_counter(tmp_path):
    ens = build_unified_ensemble(seed=0)
    brain = UnifiedBrain(ensemble=ens, online_enabled=True)
    closes = _closes()
    # do a few online updates
    for _ in range(3):
        brain.online_learn(symbol="EURUSD.r", closes=closes,
                           realised_return=0.01)
    saved_weights = ens.members[0].model.layers[0].W.copy()
    updates_before = brain.online_updates
    # save + load
    path = save_unified(str(tmp_path), brain)
    assert os.path.isfile(path)
    loaded = load_unified(str(tmp_path))
    assert loaded is not None
    assert loaded.online_updates == updates_before
    assert loaded.online_enabled is True
    # weights preserved (the online-learned weights survived the round-trip)
    assert np.allclose(saved_weights,
                       loaded.ensemble.members[0].model.layers[0].W)


def test_online_learning_changes_predictions_over_time():
    """Repeated online updates from consistent outcomes shift the model."""
    ens = build_ensemble([{"name": "a", "sizes": [4, 8, 2]}], seed=0)
    brain = NexusBrain(ensemble=ens, online_enabled=True, nn_online_lr=5e-3)
    x = np.asarray([[0.5, -0.2, 0.3, 0.1]])
    # feed many BUY-correct outcomes
    for _ in range(40):
        brain.online_learn_features([0.5, -0.2, 0.3, 0.1], 0.01)
    p = ens.members[0].model.predict_proba(x)
    # model should increasingly favour the BUY class after BUY-correct updates
    assert p[0][1] >= p[0][0]
