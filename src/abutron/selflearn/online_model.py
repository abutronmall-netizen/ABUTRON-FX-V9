"""Online Gradient-Descent edge-prediction model.

This is the core of the self-learning AI: a linear model
    y_hat = w . x + b
that predicts the *next-bar* directional edge of an instrument from a
feature vector (order-flow imbalance, footprint bias, regime, agent
scores, vol, momentum). After each bar closes it sees the realised
return and updates its own weights via stochastic gradient descent:

    w <- w + lr * (y - y_hat) * x  -  l2 * w

so the model learns from live outcomes, one bar at a time, without any
offline retraining. Feature vectors are normalised online (running
std-dev) so the gradient is well-scaled as the market regime shifts.

GUARDRAIL: this model only produces a scalar *edge prediction*. It does
NOT place orders, does NOT set stops, and does NOT touch execution. Its
output is consumed only by the bounded weight-overlay layer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np


@dataclass
class OnlineLinearModel:
    """Online gradient-descent linear regressor with online feature scaling."""
    n_features: int
    lr: float = 0.05                 # learning rate (per-step)
    l2: float = 1e-5                 # L2 regularisation
    init_w: float = 0.0
    init_b: float = 0.0
    min_lr: float = 1e-4
    max_lr: float = 0.5
    ema_lr: float = 0.95             # smooth the effective learning rate
    _lr_state: float = 0.05

    w: np.ndarray = field(init=False)
    b: float = field(init=False)
    # online feature normalisation
    _feat_mean: Optional[np.ndarray] = field(default=None, init=False)
    _feat_var: Optional[np.ndarray] = field(default=None, init=False)
    _feat_n: int = field(default=0, init=False)
    # tracking
    updates: int = field(default=0, init=False)
    mse_ema: float = field(default=math.nan, init=False)
    _mse_ema_valid: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.w = np.full(self.n_features, self.init_w, dtype=float)
        self.b = self.init_b
        self._lr_state = self.lr
        self._feat_mean = np.zeros(self.n_features, dtype=float)
        self._feat_var = np.ones(self.n_features, dtype=float)

    # ------------------------------------------------------------------
    def _normalize(self, x: np.ndarray) -> np.ndarray:
        return (x - self._feat_mean) / np.sqrt(self._feat_var + 1e-12)

    def _update_stats(self, x: np.ndarray) -> None:
        self._feat_n += 1
        # online mean/variance
        delta = x - self._feat_mean
        self._feat_mean += delta / self._feat_n
        m2 = delta * (x - self._feat_mean)
        self._feat_var += (m2 - self._feat_var) / self._feat_n
        # clip variance to avoid blow-up on near-constant features
        np.clip(self._feat_var, 1e-8, None, out=self._feat_var)

    # ------------------------------------------------------------------
    def predict(self, features: np.ndarray) -> float:
        """Predict normalised next-bar return (edge) from raw features."""
        x = self._normalize(np.asarray(features, dtype=float))
        return float(np.dot(self.w, x) + self.b)

    def update(self, features: np.ndarray, target: float) -> float:
        """Observe the realised target and update weights (one SGD step).

        Returns the prediction error (y - y_hat) for monitoring.
        """
        x = np.asarray(features, dtype=float)
        self._update_stats(x)
        xn = self._normalize(x)
        y_hat = float(np.dot(self.w, xn) + self.b)
        error = target - y_hat
        # smoothed, adaptive learning rate (slow down as it settles)
        self._lr_state = self.ema_lr * self._lr_state + (1 - self.ema_lr) * self.lr
        lr = self._lr_state
        # gradient step with L2
        self.w += lr * error * xn - self.l2 * lr * self.w
        self.b += lr * error
        # track EMA of squared error for health monitoring
        se = error * error
        if self._mse_ema_valid:
            self.mse_ema = 0.9 * self.mse_ema + 0.1 * se
        else:
            self.mse_ema = se
            self._mse_ema_valid = True
        self.updates += 1
        return error

    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, object]:
        return {
            "n_features": self.n_features, "lr": self.lr, "l2": self.l2,
            "w": [round(float(v), 6) for v in self.w],
            "b": round(self.b, 6),
            "feat_mean": [round(float(v), 6) for v in self._feat_mean],
            "feat_var": [round(float(v), 6) for v in self._feat_var],
            "updates": self.updates,
            "mse_ema": None if not self._mse_ema_valid else round(self.mse_ema, 6),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "OnlineLinearModel":
        m = cls(n_features=int(d["n_features"]), lr=float(d.get("lr", 0.05)),
                l2=float(d.get("l2", 1e-5)))
        m.w = np.asarray(d["w"], dtype=float)
        m.b = float(d["b"])
        m._feat_mean = np.asarray(d.get("feat_mean", np.zeros(m.n_features)),
                                  dtype=float)
        m._feat_var = np.asarray(d.get("feat_var", np.ones(m.n_features)),
                                 dtype=float)
        m.updates = int(d.get("updates", 0))
        mm = d.get("mse_ema")
        if mm is not None:
            m.mse_ema = float(mm)
            m._mse_ema_valid = True
        return m
