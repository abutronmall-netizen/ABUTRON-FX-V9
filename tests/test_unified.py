"""Tests for the unified (all-symbols-one-brain) brain."""
from __future__ import annotations

import math
import os

import pytest

from abutron.brain.unified import (
    SYMBOL_UNIVERSE, UnifiedBrain, build_unified_ensemble,
    build_unified_feature_vector, n_unified_features, symbol_index,
    prepare_unified_samples, train_unified, save_unified, load_unified,
    unified_brain_dir)
from abutron.brain.features import N_FEATURES
from abutron.brain.neural import MLP
from abutron.brain import NexusBrain


def test_symbol_index_maps_known_and_unknown():
    assert symbol_index("EURUSD.r") == SYMBOL_UNIVERSE.index("EURUSD.r")
    assert symbol_index("Boom 600 Index.0") == \
        SYMBOL_UNIVERSE.index("Boom 600 Index.0")
    # unknown symbols fall back to index 0 (safe default one-hot)
    assert symbol_index("NOPE.notreal") == 0


def test_unified_feature_dimension_is_market_plus_onehot():
    feats = build_unified_feature_vector(
        symbol="EURUSD.r", closes=[1.0, 1.01, 1.02, 1.01, 1.03])
    assert len(feats) == n_unified_features()
    assert len(feats) == N_FEATURES + len(SYMBOL_UNIVERSE)
    # the one-hot tail marks the symbol
    tail = feats[N_FEATURES:]
    assert tail[symbol_index("EURUSD.r")] == 1.0
    assert sum(tail) == 1.0


def test_unified_brain_is_nexusbrain_and_decides_any_symbol():
    brain = UnifiedBrain()
    assert isinstance(brain, NexusBrain)
    assert brain.trained  # ensemble built
    # decides for any symbol in the universe (one brain, all symbols)
    for sym in ("EURUSD.r", "Boom 600 Index.0", "US100"):
        closes = [100.0 + i * 0.1 for i in range(30)]
        prop = brain.decide(symbol=sym, closes=closes,
                            highs=[c * 1.001 for c in closes],
                            lows=[c * 0.999 for c in closes])
        assert prop.direction in ("BUY", "SELL")
        assert prop.symbol == sym
        assert prop.brain_version == "unified_v1"


def test_unified_size_position_inherited():
    brain = UnifiedBrain()
    r = brain.size_position(equity=1000.0, risk_pct=0.01,
                            stop_distance_points=20.0, point_value=1.0,
                            lot_min=0.01, lot_max=50.0)
    assert r["lots"] > 0
    assert r["reason"] == "OK"


def _synth(symbol, n=120, start=100.0):
    closes = [start + math.sin(i / 7) * 0.5 + i * 0.01 for i in range(n)]
    return {
        "closes": closes,
        "highs": [c + 0.3 for c in closes],
        "lows": [c - 0.3 for c in closes],
    }


def test_train_unified_pools_symbols_and_reports():
    ensemble = build_unified_ensemble(seed=0)
    data = {s: _synth(s, n=120) for s in ("EURUSD.r", "US100", "Boom 600 Index.0")}
    samples_by_sym = {
        s: prepare_unified_samples({s: d}) for s, d in data.items()}
    report = train_unified(ensemble, samples_by_sym, epochs=5, seed=0)
    assert report.n_samples > 0
    assert report.val_accuracy >= 0.0
    # per-symbol accuracy breakdown present
    assert "per_symbol_accuracy" in report.__dict__


def test_save_load_unified_roundtrip(tmp_path):
    brain = UnifiedBrain()
    # give it a trained ensemble so state is meaningful
    ensemble = build_unified_ensemble(seed=1)
    brain.attach_ensemble(ensemble)
    path = save_unified(str(tmp_path), brain)
    assert os.path.isfile(path)
    loaded = load_unified(str(tmp_path))
    assert loaded is not None
    assert isinstance(loaded, UnifiedBrain)
    assert loaded.symbol_ids == brain.symbol_ids
    assert len(loaded.ensemble.members) == len(ensemble.members)
    # loaded brain still decides
    closes = [100.0 + i * 0.1 for i in range(30)]
    prop = loaded.decide(symbol="EURUSD.r", closes=closes)
    assert prop.direction in ("BUY", "SELL")


def test_load_unified_none_when_absent(tmp_path):
    assert load_unified(str(tmp_path)) is None


def test_load_unified_none_for_non_unified_brain(tmp_path):
    # a per-symbol brain dir (not unified) must not load as unified
    from abutron.brain import default_ensemble
    from abutron.brain.trainer import save_brain
    ens = default_ensemble()
    save_brain(str(tmp_path), ens)
    assert load_unified(str(tmp_path)) is None
