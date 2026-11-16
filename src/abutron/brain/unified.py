"""Unified brain — all per-symbol brains working as ONE brain.

The directive: *"all brains must work as one brain if possible."* The
platform trained 26 per-symbol ``NexusBrain`` ensembles (one per
instrument). This module fuses them into a single brain that trades any
symbol through one model.

**How it works.** The per-symbol brains are equity- and symbol-agnostic
on the *input* side: ``build_feature_vector`` consumes only normalised
price / order-flow / footprint / regime features, so the same 23-dim
feature layout describes every instrument. The only thing missing for a
single cross-symbol model is *which* symbol the features belong to. We
add a **symbol one-hot** to the feature vector, so one pooled ensemble
can learn symbol-specific behaviour (each instrument has its own price
scale, vol regime and edge) while sharing one set of weights — i.e. one
brain.

The unified brain is a ``NexusBrain`` subclass: it keeps the exact same
``decide`` / ``size_position`` / ``learn_from_trade`` interface, so the
engine wiring is unchanged — only the signal ensemble is the single
unified, symbol-conditioned model. Trade memory (entry-timing biases)
remains per-symbol, which is correct: the *signal* is one brain, the
*learned patience* is still tracked per instrument.

Training (``train_unified``) pools every symbol's walk-forward samples,
each augmented with its symbol one-hot, splits each symbol's series
80/20 by time (so validation is held-out per instrument, not leaked),
and trains one ensemble. The result is saved to
``data/brains/unified_m5/nexus_brain.json`` and loaded by the engine for
*every* symbol — the per-symbol specialists are no longer on the order
path.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .neural import MLP, RNG
from .ensemble import BrainEnsemble, EnsembleMember, build_ensemble
from .features import N_FEATURES, FEATURE_VERSION, build_feature_vector, \
    returns_from_closes
from .signals import SIGNAL_DATA_VERSION
from .brain import NexusBrain
from .trainer import TrainingSample, TrainReport, _make_labels


# ---------------------------------------------------------------------------
# The traded symbol universe (one entry per trained per-symbol brain).
# The unified brain conditions on all of these via a one-hot.
# ---------------------------------------------------------------------------
SYMBOL_UNIVERSE: Tuple[str, ...] = (
    # FP-Markets FX
    "AUDUSD.r", "EURUSD.r", "GBPUSD.r", "NZDUSD.r", "USDCHF.r", "USDJPY.r",
    "XAUUSD.r",
    # FP-Markets indices / commodities
    "BTCUSD", "XTIUSD", "US30", "US500", "US100", "GER40",
    # Deriv synthetic indices
    "Boom 1000 Index.0", "Boom 600 Index.0", "Boom 300 Index.0",
    "Boom 150 Index.0",
    "Crash 1000 Index.0", "Crash 600 Index.0", "Crash 300 Index.0",
    "Crash 150 Index.0",
    "Volatility 10 Index.0", "Volatility 25 Index.0", "Volatility 50 Index.0",
    "Volatility 75 Index.0", "Volatility 100 Index.0",
)


def symbol_index(symbol: str,
                 universe: Sequence[str] = SYMBOL_UNIVERSE) -> int:
    """Return the one-hot index of ``symbol`` in the universe.

    Unknown symbols map to index 0 (a safe default one-hot) so the unified
    brain never breaks on a new instrument — it just treats it as the
    first symbol until retrained with the new universe.
    """
    for i, s in enumerate(universe):
        if s == symbol:
            return i
    return 0


def _one_hot(symbol: str, universe: Sequence[str] = SYMBOL_UNIVERSE
             ) -> List[float]:
    vec = [0.0] * len(universe)
    vec[symbol_index(symbol, universe)] = 1.0
    return vec


def n_unified_features(universe: Sequence[str] = SYMBOL_UNIVERSE) -> int:
    """Total input dim of the unified brain: market features + one-hot."""
    return N_FEATURES + len(universe)


# ---------------------------------------------------------------------------
# Unified feature builder
# ---------------------------------------------------------------------------
def build_unified_feature_vector(
    *, symbol: str, closes: Optional[Sequence[float]] = None,
    highs: Optional[Sequence[float]] = None,
    lows: Optional[Sequence[float]] = None,
    orderflow: Optional[Dict[str, float]] = None,
    footprint: Optional[Dict[str, float]] = None,
    regime: str = "", agent_scores: Optional[Dict[str, float]] = None,
    universe: Sequence[str] = SYMBOL_UNIVERSE,
) -> List[float]:
    """Market features + symbol one-hot (the unified brain's input)."""
    rets = returns_from_closes(closes or [])
    feats = build_feature_vector(
        closes=closes, highs=highs, lows=lows, orderflow=orderflow,
        footprint=footprint, regime=regime, agent_scores=agent_scores,
        **rets)
    return list(feats) + _one_hot(symbol, universe)


# ---------------------------------------------------------------------------
# Unified ensemble + brain
# ---------------------------------------------------------------------------
def build_unified_ensemble(universe: Sequence[str] = SYMBOL_UNIVERSE,
                           *, seed: int = 0) -> BrainEnsemble:
    """A diverse ensemble sized for the unified (market + one-hot) input."""
    d = n_unified_features(universe)
    specs = [
        {"name": "u_snap_a", "sizes": [d, 48, 24, 16, 2],
         "kind": "snapshot", "weight": 1.0, "p_drop": 0.1},
        {"name": "u_snap_b", "sizes": [d, 32, 32, 16, 2],
         "kind": "snapshot", "weight": 1.0, "p_drop": 0.15},
        {"name": "u_seq_c", "sizes": [d, 56, 28, 16, 2],
         "kind": "sequence", "weight": 1.0, "p_drop": 0.1},
    ]
    return build_ensemble(specs, seed=seed)


class UnifiedBrain(NexusBrain):
    """One brain for all symbols — a symbol-conditioned ensemble.

    Behaves exactly like ``NexusBrain`` (``decide``, ``size_position``,
    ``learn_from_trade``, ``get_entry_bias``) but the signal ensemble is
    the single unified, symbol-conditioned model. ``decide`` appends the
    symbol one-hot before prediction.
    """

    VERSION = "unified_v1"

    def __init__(self, *, ensemble: Optional[BrainEnsemble] = None,
                 symbol_ids: Sequence[str] = SYMBOL_UNIVERSE,
                 signal_sources: Optional[Dict[str, Any]] = None,
                 feature_audit: Optional[Dict[str, Any]] = None,
                 **kwargs):
        self.symbol_ids = list(symbol_ids)
        self.signal_sources = dict(signal_sources or {})
        self.feature_audit = dict(feature_audit or {})
        super().__init__(ensemble=ensemble, **kwargs)
        if self.ensemble is None or not self.ensemble.members:
            self.ensemble = build_unified_ensemble(self.symbol_ids)

    # -- decision: market features + symbol one-hot ----------------------
    def decide(self, *, symbol: str, closes: Optional[List[float]] = None,
               highs: Optional[List[float]] = None,
               lows: Optional[List[float]] = None,
               orderflow: Optional[Dict[str, float]] = None,
               footprint: Optional[Dict[str, float]] = None,
               agent_scores: Optional[Dict[str, float]] = None,
               regime: str = "") -> Any:
        from .brain import detect_regime
        from .features import _atr
        closes = list(closes or [])
        highs = list(highs or [])
        lows = list(lows or [])
        if not regime and closes:
            regime = detect_regime(closes, _atr(highs or closes,
                                                lows or closes, closes))
        if self.ensemble is None or not self.ensemble.members:
            return self._fallback_unified(symbol, regime)
        feats = build_unified_feature_vector(
            symbol=symbol, closes=closes, highs=highs, lows=lows,
            orderflow=orderflow, footprint=footprint, regime=regime,
            agent_scores=agent_scores, universe=self.symbol_ids)
        x = np.asarray([feats], dtype=float)
        pred = self.ensemble.predict(x)
        # Aggressive mode: commit to a direction on any edge (no abstention),
        # except for an explicitly recorded, still-losing feature pattern.
        direction = pred.direction
        blocked, guard_reason = self.loss_guard.should_block(
            symbol=symbol, direction=direction, regime=regime, features=feats)
        if blocked:
            from .brain import BrainProposal
            return BrainProposal(
                symbol=symbol, direction="HOLD", conviction=0.0,
                confidence=0.0, uncertainty=1.0, regime=regime,
                model_agreement=dict(pred.votes), position_size=0.0,
                blockers=[guard_reason],
                rationale="LOSS_PATTERN_REPEAT: unified brain vetoed a repeat loss setup",
                brain_version=self.VERSION, trained=True, features=list(feats))
        conviction = min(pred.conviction, self.max_conviction)
        position_size = float(min(self.size_cap, 0.25 + 0.75 * conviction))
        rationale = (f"unified ensemble={pred.members} dir={direction} "
                     f"conv={conviction:.3f} conf={pred.confidence:.3f} "
                     f"unc={pred.uncertainty:.3f} size={position_size:.3f} "
                     f"regime={regime} sym={symbol}")
        from .brain import BrainProposal
        return BrainProposal(
            symbol=symbol, direction=direction, conviction=conviction,
            confidence=pred.confidence, uncertainty=pred.uncertainty,
            regime=regime, model_agreement=pred.votes,
            position_size=position_size, rationale=rationale,
            brain_version=self.VERSION, trained=True,
            features=list(feats))

    def online_learn(
        self, *, symbol: str,
        closes: Optional[List[float]] = None,
        highs: Optional[List[float]] = None,
        lows: Optional[List[float]] = None,
        realised_return: float = 0.0,
        orderflow: Optional[Dict[str, float]] = None,
        footprint: Optional[Dict[str, float]] = None,
        agent_scores: Optional[Dict[str, float]] = None,
        regime: str = "",
    ) -> Dict[str, Any]:
        """Online neural update using the unified (market + one-hot) features.

        Same contract as ``NexusBrain.online_learn``: one single-sample
        gradient step per member toward the realised direction. The
        symbol one-hot is appended so each instrument's outcome refines
        that instrument's slice of the shared weights.
        """
        if not self.online_enabled or not self.trained:
            return {"updated": False,
                    "reason": "ONLINE_DISABLED" if not self.online_enabled
                    else "UNTRAINED"}
        closes = list(closes or [])
        highs = list(highs or closes)
        lows = list(lows or closes)
        if not closes:
            return {"updated": False, "reason": "NO_DATA"}
        feats = build_unified_feature_vector(
            symbol=symbol, closes=closes, highs=highs, lows=lows,
            orderflow=orderflow, footprint=footprint, regime=regime,
            agent_scores=agent_scores, universe=self.symbol_ids)
        return self.online_learn_features(feats, realised_return,
                                          symbol=symbol)

    def unified_features_from_market(self, market_features: Sequence[float],
                                     symbol: str) -> List[float]:
        """Append this brain's symbol one-hot to a 23-dim market vector."""
        market = [float(v) for v in list(market_features)[:N_FEATURES]]
        if len(market) < N_FEATURES:
            market += [0.0] * (N_FEATURES - len(market))
        return market + _one_hot(symbol, self.symbol_ids)

    def online_learn_market_features(self, market_features: Sequence[float],
                                     realised_return: float, *, symbol: str = ""
                                     ) -> Dict[str, Any]:
        """Online gradient step from a precomputed causal market vector."""
        feats = self.unified_features_from_market(market_features, symbol)
        return self.online_learn_features(feats, realised_return, symbol=symbol)

    def _fallback_unified(self, symbol: str, regime: str):
        # Deterministic rule edge (same as NexusBrain._fallback) but routed
        # through the unified feature builder so behaviour is consistent.
        try:
            feats = build_unified_feature_vector(symbol=symbol, regime=regime,
                                                 universe=self.symbol_ids)
            f = np.asarray(feats, dtype=float)
            edge = 0.3 * float(f[2]) + 0.3 * float(f[4]) + \
                0.2 * float(f[10]) + 0.2 * float(f[18])
            edge = float(max(-1.0, min(1.0, edge)))
            conviction = min(1.0, abs(edge))
            direction = "BUY" if edge >= 0 else "SELL"
            blocked, guard_reason = self.loss_guard.should_block(
                symbol=symbol, direction=direction, regime=regime,
                features=feats)
            from .brain import BrainProposal
            if blocked:
                return BrainProposal(
                    symbol=symbol, direction="HOLD", conviction=0.0,
                    confidence=0.0, uncertainty=1.0, regime=regime,
                    position_size=0.0, blockers=[guard_reason],
                    rationale="LOSS_PATTERN_REPEAT: fallback vetoed a repeat loss setup",
                    brain_version=self.VERSION, trained=False,
                    features=list(feats))
            return BrainProposal(
                symbol=symbol, direction=direction, conviction=conviction,
                confidence=0.5 + 0.4 * conviction,
                uncertainty=1.0 - conviction, regime=regime,
                position_size=float(min(self.size_cap,
                                        0.25 + 0.75 * conviction)),
                rationale=f"unified_fallback edge={edge:.3f} sym={symbol}",
                brain_version=self.VERSION, trained=False)
        except Exception:
            from .brain import BrainProposal
            return BrainProposal(
                symbol=symbol, direction="BUY", conviction=0.1,
                confidence=0.0, uncertainty=1.0, regime=regime,
                position_size=0.1,
                rationale="unified_fallback_error",
                brain_version=self.VERSION, trained=False)

    # -- persistence ----------------------------------------------------
    def state_dict(self) -> Dict[str, Any]:
        d = super().state_dict()
        d["symbol_ids"] = list(self.symbol_ids)
        d["unified"] = True
        return d


# ---------------------------------------------------------------------------
# Unified sample preparation + training
# ---------------------------------------------------------------------------
def _symbol_samples(closes, highs, lows, *, orderflow=None, footprint=None,
                    regimes=None, agent_scores=None, horizon=1,
                    symbol: str, universe=SYMBOL_UNIVERSE
                    ) -> List[TrainingSample]:
    """Per-symbol samples, each augmented with the symbol one-hot."""
    from .features import returns_from_closes
    highs = list(highs or closes)
    lows = list(lows or closes)
    of = list(orderflow or [{} for _ in closes])
    fp = list(footprint or [{} for _ in closes])
    regs = list(regimes or ["" for _ in closes])
    scores = list(agent_scores or [{} for _ in closes])
    labels = _make_labels(closes, horizon)
    onehot = _one_hot(symbol, universe)
    out: List[TrainingSample] = []
    window_feats: List[List[float]] = []
    for i in range(len(closes)):
        rets = returns_from_closes(closes[: i + 1])
        f = build_feature_vector(
            closes=closes[: i + 1], highs=highs[: i + 1],
            lows=lows[: i + 1], orderflow=of[i], footprint=fp[i],
            regime=regs[i], agent_scores=scores[i], **rets)
        window_feats.append(f)
    for i, y in enumerate(labels):
        out.append(TrainingSample(features=window_feats[i] + onehot,
                                  label=y))
    return out


def prepare_unified_samples(
    symbol_data: Dict[str, Dict[str, Sequence]],
    *, horizon: int = 1, universe: Sequence[str] = SYMBOL_UNIVERSE,
) -> List[TrainingSample]:
    """Pool samples from every symbol, each with its symbol one-hot.

    ``symbol_data`` maps symbol -> {"closes", "highs", "lows", ...}.
    """
    samples: List[TrainingSample] = []
    for sym, d in symbol_data.items():
        market_features = d.get("market_features")
        if market_features:
            # Generated by brain.signals.build_signal_bundle: causal, complete
            # signal slots, and O(n) instead of rebuilding a full window per bar.
            onehot = _one_hot(sym, universe)
            labels = _make_labels(d["closes"], horizon)
            for feats, label in zip(market_features, labels):
                samples.append(TrainingSample(
                    features=list(feats)[:N_FEATURES] + onehot, label=label))
        else:
            samples.extend(_symbol_samples(
                d["closes"], d.get("highs", d["closes"]),
                d.get("lows", d["closes"]),
                orderflow=d.get("orderflow"), footprint=d.get("footprint"),
                regimes=d.get("regimes"), agent_scores=d.get("agent_scores"),
                horizon=horizon, symbol=sym, universe=universe))
    return samples


def train_unified(
    ensemble: BrainEnsemble,
    symbol_samples_by_symbol: Dict[str, List[TrainingSample]],
    *, epochs: int = 120, batch_size: int = 32, val_fraction: float = 0.2,
    seed: int = 0, min_train: int = 40,
) -> TrainReport:
    """Train the unified ensemble with per-symbol walk-forward validation.

    Each symbol's series is split 80/20 by time (train = earlier bars,
    val = held-out later bars), so the reported accuracy is genuinely
    out-of-sample per instrument — no cross-symbol leakage.
    """
    rng = np.random.default_rng(seed)
    Xtr_list: List[np.ndarray] = []
    ytr_list: List[np.ndarray] = []
    Xva_list: List[np.ndarray] = []
    yva_list: List[np.ndarray] = []
    for sym, samples in symbol_samples_by_symbol.items():
        if len(samples) < min_train + 8:
            continue
        X = np.asarray([s.features for s in samples], dtype=float)
        y = np.asarray([s.label for s in samples], dtype=float)
        n_val = max(8, int(len(X) * val_fraction))
        Xtr_list.append(X[: len(X) - n_val])
        ytr_list.append(y[: len(y) - n_val])
        Xva_list.append(X[len(X) - n_val:])
        yva_list.append(y[len(y) - n_val:])
    if not Xtr_list:
        return TrainReport(notes=["INSUFFICIENT_DATA"])
    Xtr = np.concatenate(Xtr_list, axis=0)
    ytr = np.concatenate(ytr_list, axis=0)
    Xva = np.concatenate(Xva_list, axis=0)
    yva = np.concatenate(yva_list, axis=0)

    last_loss = 0.0
    for epoch in range(epochs):
        order = rng.permutation(len(Xtr))
        for start in range(0, len(Xtr), batch_size):
            idx = order[start:start + batch_size]
            for m in ensemble.members:
                last_loss = m.model.train_step(Xtr[idx], ytr[idx], rng=rng)

    # held-out per-symbol validation
    val_preds = np.asarray([m.predict(Xva) for m in ensemble.members])
    blend = np.mean(val_preds, axis=0)
    buy_idx = 1 if blend.shape[-1] == 2 else 2
    edge = blend[..., buy_idx] - (1.0 - blend[..., buy_idx])
    pred_dir = np.where(edge > 0, 1, 0)
    correct = float(np.mean(pred_dir == yva))
    raw_conf = np.maximum(blend[..., buy_idx], blend[..., 1 - buy_idx])
    ensemble.fit_calibration([float(c) for c in raw_conf],
                            [int(d == y) for d, y in zip(pred_dir, yva)])
    # per-symbol accuracy breakdown
    per_sym: Dict[str, float] = {}
    ptr = 0
    for sym, samples in symbol_samples_by_symbol.items():
        if len(samples) < min_train + 8:
            continue
        n_val = max(8, int(len(samples) * val_fraction))
        seg = pred_dir[ptr:ptr + n_val]
        sy = yva[ptr:ptr + n_val]
        ptr += n_val
        if len(seg):
            per_sym[sym] = round(float(np.mean(seg == sy)), 4)
    report = TrainReport(
        n_samples=int(len(Xtr) + len(Xva)), n_train=int(len(Xtr)),
        n_val=int(len(Xva)), final_loss=last_loss,
        val_accuracy=correct, val_direction_accuracy=correct,
        calibrated=True,
        notes=[f"epochs={epochs}", f"unified_symbols={len(Xtr_list)}"],
    )
    report.__dict__["per_symbol_accuracy"] = per_sym
    return report


def save_unified(brain_dir: str, brain: UnifiedBrain,
                 report: Optional[TrainReport] = None) -> str:
    """Persist the unified brain (ensemble + symbol universe + report)."""
    os.makedirs(brain_dir, exist_ok=True)
    path = os.path.join(brain_dir, "nexus_brain.json")
    previous: Dict[str, Any] = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                previous = json.load(fh)
        except (OSError, ValueError):
            previous = {}
    payload = {
        "unified": True,
        "feature_version": FEATURE_VERSION,
        "signal_data_version": SIGNAL_DATA_VERSION,
        "n_features": n_unified_features(brain.symbol_ids),
        "market_features": N_FEATURES,
        "symbol_ids": list(brain.symbol_ids),
        "signal_sources": brain.signal_sources or previous.get("signal_sources", {}),
        "feature_audit": brain.feature_audit or previous.get("feature_audit", {}),
        "members": [{"name": m.name, "kind": m.kind, "weight": m.weight,
                     "state": m.model.state_dict()}
                    for m in brain.ensemble.members],
        "calibration": {"a": brain.ensemble._cal_a,
                        "b": brain.ensemble._cal_b,
                        "fitted": brain.ensemble._fitted},
        "report": report.to_dict() if report else previous.get("report"),
        "online": {"updates": brain.online_updates,
                   "enabled": brain.online_enabled,
                   "nn_lr": brain.nn_online_lr},
        "trade_memory": brain.trade_memory.state_dict(),
        "loss_guard": brain.loss_guard.state_dict(),
    }
    with open(path, "w") as fh:
        json.dump(payload, fh)
    return path


def load_unified(brain_dir: str, *, mc_samples: int = 16
                 ) -> Optional[UnifiedBrain]:
    """Load the unified brain. Returns None if absent / not unified."""
    path = os.path.join(brain_dir, "nexus_brain.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            payload = json.load(fh)
        if not payload.get("unified"):
            return None
        symbol_ids = payload.get("symbol_ids") or list(SYMBOL_UNIVERSE)
        rng = np.random.default_rng(0)
        members: List[EnsembleMember] = []
        for m in payload["members"]:
            model = MLP(m["state"]["sizes"], p_drop=m["state"]["p_drop"],
                        out_act=m["state"]["out_act"], rng=rng)
            model.load_state_dict(m["state"])
            members.append(EnsembleMember(name=m["name"], model=model,
                                          kind=m["kind"], weight=m["weight"]))
        ens = BrainEnsemble(members, mc_samples=mc_samples)
        cal = payload.get("calibration", {})
        ens._cal_a = cal.get("a", 1.0)
        ens._cal_b = cal.get("b", 0.0)
        ens._fitted = cal.get("fitted", False)
        _online = payload.get("online", {})
        brain = UnifiedBrain(
            ensemble=ens, symbol_ids=symbol_ids,
            online_enabled=_online.get("enabled", True),
            nn_online_lr=_online.get("nn_lr", 1e-3),
            signal_sources=payload.get("signal_sources") or {},
            feature_audit=payload.get("feature_audit") or {})
        brain.online_updates = int(_online.get("updates", 0))
        brain.load_memory_state({
            "trade_memory": payload.get("trade_memory") or {},
            "loss_guard": payload.get("loss_guard") or {},
        })
        return brain
    except Exception:
        return None


def unified_brain_dir(brain_root: Optional[str] = None) -> str:
    """The unified brain directory (``data/brains/unified_m5``)."""
    from ..config import DATA_ROOT
    root = brain_root or os.path.join(DATA_ROOT, "brains")
    return os.path.join(root, "unified_m5")
