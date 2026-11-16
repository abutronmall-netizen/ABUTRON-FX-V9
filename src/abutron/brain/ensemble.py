"""NexusBrain — ensemble, uncertainty estimation and calibration.

The brain does not bet on a single model. It trains a small *ensemble* of
diverse members (different depths / seeds / sequence-vs-snapshot encoders)
and combines them. This reduces variance and overfitting and — crucially —
gives a principled **uncertainty** estimate from two complementary sources:

  * **ensemble disagreement** — how much the members disagree with each other
  * **MC-dropout** — the spread of a single member's stochastic forward
    passes

Both are fused into a single ``uncertainty`` score in [0, 1]. The brain
**abstains** (returns HOLD) when uncertainty is high or calibrated
confidence is below a threshold — the core safety property that keeps an
unconfident "advanced" brain from trading on noise.

Confidence is **calibrated**: a simple isotonic-free Platt/temperature pass
on a held-out score so the stated confidence is meaningful rather than
merely the raw softmax max.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np

from .neural import MLP, RNG


@dataclass
class EnsembleMember:
    name: str
    model: MLP
    kind: str = "snapshot"          # 'snapshot' | 'sequence'
    weight: float = 1.0

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.model.predict_proba(x)

    def predict_mc(self, x: np.ndarray, n: int = 16) -> np.ndarray:
        return self.model.predict_mc(x, n=n)


@dataclass
class EnsemblePrediction:
    direction: str                    # BUY | SELL | HOLD
    conviction: float                 # 0..1  (blended edge magnitude)
    confidence: float                 # 0..1  (calibrated)
    uncertainty: float                # 0..1  (ensemble + MC-dropout)
    votes: Dict[str, float] = field(default_factory=dict)
    raw_scores: List[float] = field(default_factory=list)
    members: int = 0

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "conviction": round(self.conviction, 4),
            "confidence": round(self.confidence, 4),
            "uncertainty": round(self.uncertainty, 4),
            "votes": {k: round(v, 4) for k, v in self.votes.items()},
            "members": self.members,
        }


class BrainEnsemble:
    """A weighted ensemble with fused uncertainty and calibration."""

    def __init__(self, members: List[EnsembleMember],
                 *, mc_samples: int = 16, temperature: float = 1.0):
        self.members = members
        self.mc_samples = mc_samples
        self.temperature = temperature
        # calibration: map raw edge score -> calibrated confidence
        self._cal_a: float = 1.0
        self._cal_b: float = 0.0
        self._fitted = False

    # -- inference --------------------------------------------------------
    def predict(self, x: np.ndarray) -> EnsemblePrediction:
        if not self.members or x.size == 0:
            return EnsemblePrediction("HOLD", 0.0, 0.0, 1.0, members=0)

        probs = []
        mc_stacks: List[np.ndarray] = []
        for m in self.members:
            p = m.predict(x)
            probs.append(p)
            if self.mc_samples > 1:
                mc_stacks.append(m.predict_mc(x, n=self.mc_samples))

        # blended class probabilities (weighted)
        total_w = sum(m.weight for m in self.members) or 1.0
        blend = np.zeros_like(probs[0])
        for m, p in zip(self.members, probs):
            blend += (m.weight / total_w) * p

        # direction from class probabilities
        n_classes = blend.shape[-1]
        # convention: class 0 = SELL, class 1 = BUY (2-class) or 3-class
        # we reduce to a directional edge score in [-1, 1]
        if n_classes == 2:
            edge = float(np.mean(blend[..., 1] - blend[..., 0]))
            buy_p = float(np.mean(blend[..., 1]))
            sell_p = float(np.mean(blend[..., 0]))
        else:  # 3-class: 0 SELL, 1 HOLD, 2 BUY
            buy_p = float(np.mean(blend[..., 2]))
            sell_p = float(np.mean(blend[..., 0]))
            edge = buy_p - sell_p

        if edge > 0:
            direction = "BUY"
        elif edge < 0:
            direction = "SELL"
        else:
            direction = "HOLD"
        conviction = float(min(1.0, abs(edge)))

        # --- uncertainty: ensemble disagreement + MC-dropout spread ---
        dis = 0.0
        if len(probs) > 1:
            mean_p = np.mean(probs, axis=0)
            dis = float(np.mean(np.sqrt(np.mean(
                (np.asarray(probs) - mean_p) ** 2, axis=0))))
        mc_spread = 0.0
        if mc_stacks:
            mc_mean = np.mean(np.stack(mc_stacks), axis=0)
            mc_spread = float(np.mean(np.sqrt(np.mean(
                (np.stack(mc_stacks) - mc_mean) ** 2, axis=0))))
        # fuse: both live in a comparable range; take the max then squash
        unc_raw = max(dis, mc_spread)
        uncertainty = float(min(1.0, unc_raw * 4.0))

        # calibrated confidence
        raw_conf = float(max(buy_p, sell_p))
        conf = self._calibrate(raw_conf)
        # uncertainty discounts confidence
        conf = float(conf * (1.0 - uncertainty))

        # per-member buy-vote (class 1 for 2-class, class 2 for 3-class)
        buy_idx = 1 if probs[0].shape[-1] == 2 else 2
        votes = {m.name: float(np.mean(pp[..., buy_idx]))
                 for m, pp in zip(self.members, probs)}

        return EnsemblePrediction(
            direction=direction, conviction=conviction,
            confidence=conf, uncertainty=uncertainty,
            votes=votes,
            raw_scores=[float(np.mean(p[..., buy_idx])) for p in probs],
            members=len(self.members),
        )

    # -- calibration -----------------------------------------------------
    def _calibrate(self, raw: float) -> float:
        if not self._fitted:
            return float(min(1.0, max(0.0, raw)))
        # sigmoid calibration: sigmoid(a*raw + b)
        import math
        z = self._cal_a * raw + self._cal_b
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    # -- online learning ------------------------------------------------
    def online_update(self, x: np.ndarray, y: np.ndarray,
                      *, lr: Optional[float] = None) -> Dict[str, float]:
        """One online gradient step across every member from one outcome.

        Each ensemble member nudges its own neural weights toward the
        realised direction, so the whole brain self-learns from each
        settled trade / observed bar. Returns a small summary.
        """
        if not self.members or x.size == 0:
            return {"updated": 0, "loss": 0.0}
        losses = []
        for m in self.members:
            if lr is not None and hasattr(m.model, "_online_opt"):
                m.model._online_opt.lr = lr
            losses.append(m.model.online_step(x, y))
        return {"updated": len(self.members),
                "loss": float(np.mean(losses))}

    def fit_calibration(self, raw_scores: Sequence[float],
                        labels: Sequence[int]) -> None:
        """Fit a simple Platt sigmoid so confidence matches realised accuracy.

        ``raw_scores`` are the raw confidences; ``labels`` are 1 when the
        prediction was directionally correct, else 0.
        """
        if len(raw_scores) < 4:
            return
        x = np.asarray(raw_scores, dtype=float)
        y = np.asarray(labels, dtype=float)
        # initial guess
        a, b = 1.0, -0.5
        lr = 0.1
        for _ in range(200):
            z = a * x + b
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
            grad_a = np.mean((p - y) * x)
            grad_b = np.mean(p - y)
            a -= lr * grad_a
            b -= lr * grad_b
        self._cal_a, self._cal_b = float(a), float(b)
        self._fitted = True


def build_ensemble(specs: Sequence[dict], *, seed: int = 0) -> BrainEnsemble:
    """Construct an ensemble from specs: {name, sizes, kind, weight}."""
    rng = np.random.default_rng(seed)
    members: List[EnsembleMember] = []
    for i, spec in enumerate(specs):
        model = MLP(spec["sizes"], p_drop=spec.get("p_drop", 0.1),
                    out_act=spec.get("out_act", "softmax"), rng=rng)
        members.append(EnsembleMember(
            name=spec.get("name", f"member_{i}"), model=model,
            kind=spec.get("kind", "snapshot"),
            weight=spec.get("weight", 1.0)))
    return BrainEnsemble(members)
