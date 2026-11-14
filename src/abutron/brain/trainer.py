"""NexusBrain — offline walk-forward training pipeline.

Trains the ensemble on historical feature windows, calibrates confidence on
a held-out slice, and reports out-of-sample metrics. The training is
**walk-forward**: train on an expanding window, validate on the next
unseen block, so the reported numbers reflect generalisation rather than
in-sample optimism.

The trainer only produces a *brain model* (weights + calibration). It never
touches brokers, orders, or execution. A trained model is loaded back into a
``NexusBrain`` for live decisioning.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .ensemble import BrainEnsemble, EnsembleMember
from .features import N_FEATURES, FEATURE_VERSION
from .neural import MLP


@dataclass
class TrainingSample:
    features: List[float]
    label: int                 # 1 = next-bar up, 0 = next-bar down


@dataclass
class TrainReport:
    n_samples: int = 0
    n_train: int = 0
    n_val: int = 0
    final_loss: float = 0.0
    val_accuracy: float = 0.0
    val_direction_accuracy: float = 0.0
    calibrated: bool = False
    feature_version: str = FEATURE_VERSION
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _make_labels(closes: Sequence[float], horizon: int = 1) -> List[int]:
    labels: List[int] = []
    for i in range(len(closes) - horizon):
        labels.append(1 if closes[i + horizon] > closes[i] else 0)
    return labels


def prepare_samples(
    closes: Sequence[float],
    highs: Optional[Sequence[float]] = None,
    lows: Optional[Sequence[float]] = None,
    *,
    orderflow: Optional[Sequence[Dict[str, float]]] = None,
    footprint: Optional[Sequence[Dict[str, float]]] = None,
    regimes: Optional[Sequence[str]] = None,
    agent_scores: Optional[Sequence[Dict[str, float]]] = None,
    horizon: int = 1,
) -> List[TrainingSample]:
    """Build labelled training samples from a price series.

    Each sample uses the trailing window ending at bar ``i`` to predict the
    direction of bar ``i+horizon`` (the classic supervised edge task).
    """
    from .features import build_feature_vector, returns_from_closes
    highs = list(highs or closes)
    lows = list(lows or closes)
    of = list(orderflow or [{} for _ in closes])
    fp = list(footprint or [{} for _ in closes])
    regs = list(regimes or ["" for _ in closes])
    scores = list(agent_scores or [{} for _ in closes])

    labels = _make_labels(closes, horizon)
    samples: List[TrainingSample] = []
    # window of per-bar feature vectors, built incrementally
    window_feats: List[List[float]] = []
    for i in range(len(closes)):
        rets = returns_from_closes(closes[: i + 1])
        f = build_feature_vector(
            closes=closes[: i + 1], highs=highs[: i + 1],
            lows=lows[: i + 1], orderflow=of[i], footprint=fp[i],
            regime=regs[i], agent_scores=scores[i], **rets)
        window_feats.append(f)
    # pair feature at i with label at i
    for i, y in enumerate(labels):
        samples.append(TrainingSample(features=window_feats[i], label=y))
    return samples


def _to_arrays(samples: Sequence[TrainingSample]) -> Tuple[np.ndarray, np.ndarray]:
    X = np.asarray([s.features for s in samples], dtype=float)
    y = np.asarray([s.label for s in samples], dtype=float)
    return X, y


def walk_forward_train(
    ensemble: BrainEnsemble,
    samples: Sequence[TrainingSample],
    *,
    epochs: int = 60,
    batch_size: int = 32,
    val_fraction: float = 0.2,
    seed: int = 0,
    min_train: int = 40,
) -> TrainReport:
    """Train the ensemble with a single walk-forward split and calibrate."""
    rng = np.random.default_rng(seed)
    X, y = _to_arrays(samples)
    n = len(X)
    if n < min_train + 8:
        report = TrainReport(n_samples=n, notes=["INSUFFICIENT_DATA"])
        return report

    n_val = max(8, int(n * val_fraction))
    Xtr, ytr = X[: n - n_val], y[: n - n_val]
    Xva, yva = X[n - n_val:], y[n - n_val:]

    last_loss = 0.0
    for epoch in range(epochs):
        order = rng.permutation(len(Xtr))
        for start in range(0, len(Xtr), batch_size):
            idx = order[start:start + batch_size]
            for m in ensemble.members:
                last_loss = m.model.train_step(Xtr[idx], ytr[idx], rng=rng)

    # validation metrics
    val_preds = np.asarray([m.predict(Xva) for m in ensemble.members])
    blend = np.mean(val_preds, axis=0)
    buy_idx = 1 if blend.shape[-1] == 2 else 2
    edge = blend[..., buy_idx] - (1.0 - blend[..., buy_idx])
    pred_dir = np.where(edge > 0, 1, 0)
    correct = float(np.mean(pred_dir == yva))
    raw_conf = np.maximum(blend[..., buy_idx], blend[..., 1 - buy_idx])

    # calibration on the validation split
    ensemble.fit_calibration(
        [float(c) for c in raw_conf],
        [int(d == y) for d, y in zip(pred_dir, yva)])

    # re-evaluate direction accuracy after calibration-independent blend
    report = TrainReport(
        n_samples=n, n_train=len(Xtr), n_val=len(Xva),
        final_loss=last_loss,
        val_accuracy=correct,
        val_direction_accuracy=correct,
        calibrated=True,
        notes=[f"epochs={epochs}", f"val_split={len(Xva)}"],
    )
    return report


def save_brain(brain_dir: str, ensemble: BrainEnsemble,
               report: Optional[TrainReport] = None) -> str:
    """Persist a trained ensemble + calibration to ``brain_dir``."""
    os.makedirs(brain_dir, exist_ok=True)
    payload = {
        "feature_version": FEATURE_VERSION,
        "n_features": N_FEATURES,
        "members": [{"name": m.name, "kind": m.kind, "weight": m.weight,
                     "state": m.model.state_dict()}
                    for m in ensemble.members],
        "calibration": {"a": ensemble._cal_a, "b": ensemble._cal_b,
                        "fitted": ensemble._fitted},
        "report": report.to_dict() if report else None,
    }
    path = os.path.join(brain_dir, "nexus_brain.json")
    with open(path, "w") as fh:
        json.dump(payload, fh)
    return path


def load_brain(brain_dir: str, *, mc_samples: int = 16) -> BrainEnsemble:
    """Load a trained ensemble from ``brain_dir``."""
    with open(os.path.join(brain_dir, "nexus_brain.json")) as fh:
        payload = json.load(fh)
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
    return ens
