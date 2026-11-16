"""Compatibility runtime for the trained 49-feature unified M5 checkpoint.

The release checkpoint is one 23-market-feature plus 26-symbol-one-hot model.
Legacy NexusBrain.decide accepts only 23 features and cannot be used as the
signal authority for this saved model. No broker order code is modified.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence
import numpy as np
from .brain import BrainProposal, NexusBrain, detect_regime
from .features import FEATURE_VERSION, N_FEATURES, _atr, build_feature_vector, returns_from_closes
from .loss_guard import LossPatternGuard
from .trainer import load_brain


class Unified49Brain(NexusBrain):
    """Calibrated checkpoint weights, trained symbol encoding and loss patterns."""

    VERSION = "unified_v1"

    def __init__(self, *, ensemble, symbols: Sequence[str],
                 trade_memory: Dict[str, Any], loss_guard: Dict[str, Any],
                 online: Dict[str, Any]):
        super().__init__(ensemble=ensemble)
        self.symbol_ids = list(symbols)
        self.loss_guard = LossPatternGuard()
        self.loss_guard.load_state(loss_guard)
        self.trade_memory.load_state(trade_memory)
        self.online_updates = int(online.get("updates", 0))

    def decide(self, *, symbol: str, closes: Optional[List[float]] = None,
               highs: Optional[List[float]] = None,
               lows: Optional[List[float]] = None,
               orderflow: Optional[Dict[str, float]] = None,
               footprint: Optional[Dict[str, float]] = None,
               agent_scores: Optional[Dict[str, float]] = None,
               regime: str = "") -> BrainProposal:
        closes = list(closes or [])
        highs = list(highs or closes)
        lows = list(lows or closes)
        if symbol not in self.symbol_ids:
            return BrainProposal(
                symbol=symbol, direction="HOLD", conviction=0.,
                confidence=0., uncertainty=1., trained=True, regime=regime,
                blockers=["SYMBOL_NOT_IN_TRAINED_UNIFIED_CHECKPOINT"],
                brain_version=self.VERSION)
        if not regime and closes:
            regime = detect_regime(closes, _atr(highs, lows, closes))
        ret = returns_from_closes(closes)
        market = build_feature_vector(
            closes=closes, highs=highs, lows=lows,
            orderflow=orderflow, footprint=footprint,
            regime=regime, agent_scores=agent_scores, **ret)
        if len(market) != N_FEATURES:
            raise ValueError("TRAINED_FEATURE_WIDTH_MISMATCH")
        onehot = [0.] * len(self.symbol_ids)
        onehot[self.symbol_ids.index(symbol)] = 1.
        features = market + onehot
        if len(features) != self.ensemble.members[0].model.sizes[0]:
            raise ValueError("TRAINED_UNIFIED_INPUT_WIDTH_MISMATCH")
        pred = self.ensemble.predict(np.asarray([features], dtype=float))
        blocked, reason = self.loss_guard.should_block(
            symbol=symbol, direction=pred.direction,
            regime=regime, features=features)
        direction = "HOLD" if blocked else pred.direction
        conviction = min(float(pred.conviction), self.max_conviction)
        position_size = min(self.size_cap, .25 + .75 * conviction)
        return BrainProposal(
            symbol=symbol, direction=direction,
            conviction=0. if blocked else conviction,
            confidence=0. if blocked else float(pred.confidence),
            uncertainty=1. if blocked else float(pred.uncertainty),
            regime=regime, model_agreement=dict(pred.votes),
            position_size=0. if blocked else float(position_size),
            blockers=[reason] if blocked else [],
            rationale="trained_unified_49" if not blocked
                      else "trained_unified_loss_pattern_hold",
            brain_version=self.VERSION, trained=True)


def load_unified_49(brain_dir: str, *, mc_samples: int = 16) -> Unified49Brain:
    """Load only a persisted and calibrated unified checkpoint."""
    path = os.path.join(brain_dir, "nexus_brain.json")
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    symbols = payload.get("symbol_ids") or []
    if (payload.get("unified") is not True or
            payload.get("feature_version") != FEATURE_VERSION or
            payload.get("market_features") != N_FEATURES or
            not isinstance(symbols, list) or len(symbols) != 26 or
            len(set(symbols)) != len(symbols) or
            payload.get("n_features") != N_FEATURES + len(symbols) or
            not isinstance(payload.get("loss_guard"), dict)):
        raise ValueError("TRAINED_UNIFIED_FORMAT_MISMATCH")
    ens = load_brain(brain_dir, mc_samples=mc_samples)
    if (not ens.members or not ens._fitted or
            any(m.model.sizes[0] != payload["n_features"]
                for m in ens.members)):
        raise ValueError("TRAINED_UNIFIED_CALIBRATION_OR_FEATURES_INVALID")
    return Unified49Brain(
        ensemble=ens, symbols=symbols,
        trade_memory=payload.get("trade_memory") or {},
        loss_guard=payload["loss_guard"],
        online=payload.get("online") or {})
