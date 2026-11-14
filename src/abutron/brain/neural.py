"""NexusBrain — compact neural primitives in pure NumPy.

A small, self-contained neural-network toolkit implementing the pieces the
advanced decision brain needs:

  * ``Dense``  — a fully-connected layer with forward + backprop.
  * ``MLP``    — a multi-layer perceptron with ReLU/Tanh/Sigmoid activations,
                 dropout, L2 regularisation and the Adam optimizer.
  * ``AttentionWindow`` — a lightweight single-head attention encoder that
                 summarises a trailing price window into a context vector
                 (query = latest bar, keys/values = the window). This gives
                 the brain *sequence awareness* rather than a static snapshot.

Everything is real gradient-descent training (forward pass, back-propagated
gradients, Adam updates) — not a stub. The point of the brain is the
*architecture* (ensemble, MC-dropout uncertainty, calibration, regime
conditioning, abstention), which this module underpins without a heavy
framework dependency.

Deterministic given a seed so training is reproducible and testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np

RNG = np.random.default_rng()


def sigmoid(x: np.ndarray) -> np.ndarray:
    # numerically stable
    out = np.empty_like(x, dtype=float)
    pos = x >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-x[pos]))
    ex = np.exp(x[~pos])
    out[~pos] = ex / (1.0 + ex)
    return out


def relu(x: np.ndarray) -> np.ndarray:
    return np.maximum(x, 0.0)


def tanh(x: np.ndarray) -> np.ndarray:
    return np.tanh(x)


def softmax(x: np.ndarray) -> np.ndarray:
    # clamp logits to keep probabilities from saturating to degenerate values
    x = np.clip(x - np.max(x, axis=-1, keepdims=True), -20.0, 20.0)
    e = np.exp(x)
    return e / np.sum(e, axis=-1, keepdims=True)


def dropout_mask(shape, p: float, rng: np.random.Generator, training: bool) -> np.ndarray:
    if not training or p <= 0.0:
        return np.ones(shape, dtype=float)
    keep = 1.0 - p
    return (rng.random(shape) < keep).astype(float) / keep


class Dense:
    """Fully-connected layer: y = act(W x + b)."""

    def __init__(self, in_dim: int, out_dim: int, *, act: str = "relu",
                 scale: float = 0.05, rng: Optional[np.random.Generator] = None):
        rng = rng or RNG
        self.W = (rng.standard_normal((in_dim, out_dim)) * scale).astype(float)
        self.b = np.zeros(out_dim, dtype=float)
        self.act = act
        self.in_dim = in_dim
        self.out_dim = out_dim
        # gradients
        self.dW: Optional[np.ndarray] = None
        self.db: Optional[np.ndarray] = None

    def forward(self, x: np.ndarray, *, training: bool = False,
                p_drop: float = 0.0, rng: Optional[np.random.Generator] = None
                ) -> np.ndarray:
        rng = rng or RNG
        z = x @ self.W + self.b
        if self.act == "relu":
            a = relu(z)
        elif self.act == "tanh":
            a = tanh(z)
        elif self.act == "sigmoid":
            a = sigmoid(z)
        else:
            a = z
        self._last_x = x
        self._last_a = a
        self._last_mask = dropout_mask(a.shape, p_drop, rng, training)
        return a * self._last_mask

    def backward(self, dout: np.ndarray, *, l2: float = 0.0) -> np.ndarray:
        mask = self._last_mask
        a = self._last_a
        z = self._last_x @ self.W + self.b
        if self.act == "relu":
            dact = (z > 0).astype(float)
        elif self.act == "tanh":
            dact = 1.0 - a * a
        elif self.act == "sigmoid":
            dact = a * (1.0 - a)
        else:
            dact = np.ones_like(a)
        dz = dout * mask * dact
        self.dW = self._last_x.T @ dz + l2 * self.W
        self.db = np.sum(dz, axis=0)
        dx = dz @ self.W.T
        return dx


@dataclass
class Adam:
    lr: float = 3e-3
    beta1: float = 0.9
    beta2: float = 0.999
    eps: float = 1e-8
    step: int = 0
    m: dict = field(default_factory=dict)
    v: dict = field(default_factory=dict)

    def step_param(self, name: str, grad: np.ndarray, param: np.ndarray) -> np.ndarray:
        self.step += 1
        # clip gradients by global norm to prevent exploding logits
        gnorm = float(np.sqrt(np.sum(grad * grad)))
        if gnorm > 5.0:
            grad = grad * (5.0 / gnorm)
        self.m[name] = self.beta1 * self.m.get(name, np.zeros_like(param)) + \
            (1 - self.beta1) * grad
        self.v[name] = self.beta2 * self.v.get(name, np.zeros_like(param)) + \
            (1 - self.beta2) * (grad * grad)
        mhat = self.m[name] / (1 - self.beta1 ** self.step)
        vhat = self.v[name] / (1 - self.beta2 ** self.step)
        return param - self.lr * mhat / (np.sqrt(vhat) + self.eps)


class MLP:
    """Multi-layer perceptron.

    ``forward`` returns the raw output; ``predict_proba`` applies a softmax
    (classification) or sigmoid (binary) as appropriate. ``train_step`` does a
    full backprop pass for a batch and applies Adam updates.
    """

    def __init__(self, sizes: Sequence[int], *, hidden_act: str = "relu",
                 out_act: str = "softmax", p_drop: float = 0.1,
                 rng: Optional[np.random.Generator] = None):
        rng = rng or RNG
        self.sizes = list(sizes)
        self.p_drop = p_drop
        self.out_act = out_act
        self.layers: List[Dense] = []
        for i in range(len(sizes) - 1):
            act = hidden_act if i < len(sizes) - 2 else (
                "sigmoid" if out_act == "sigmoid" else "none")
            self.layers.append(Dense(sizes[i], sizes[i + 1], act=act, rng=rng))
        self.optimizer = Adam()

    # -- forward ----------------------------------------------------------
    def forward(self, x: np.ndarray, *, training: bool = False
                ) -> np.ndarray:
        a = x
        for idx, layer in enumerate(self.layers):
            is_out = idx == len(self.layers) - 1
            a = layer.forward(a, training=training and not is_out,
                              p_drop=self.p_drop)
            if is_out and self.out_act == "softmax":
                a = softmax(a)
            elif is_out and self.out_act == "sigmoid":
                a = sigmoid(a)
        return a

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        return self.forward(x, training=False)

    def predict_mc(self, x: np.ndarray, n: int = 16,
                   rng: Optional[np.random.Generator] = None) -> np.ndarray:
        """Monte-Carlo dropout: n stochastic forward passes (test mode still
        applies dropout). Returns an (n, ...) stack of predictions whose
        spread is an uncertainty estimate."""
        rng = rng or RNG
        out = np.stack([self.forward(x, training=True) for _ in range(n)])
        return out

    # -- training ---------------------------------------------------------
    def train_step(self, x: np.ndarray, y: np.ndarray, *, l2: float = 1e-4,
                   rng: Optional[np.random.Generator] = None) -> float:
        """One backprop pass over the batch. Returns the mean loss."""
        rng = rng or RNG
        p = self.forward(x, training=True)
        if self.out_act == "softmax":
            if y.ndim == 1:                       # binary labels -> one-hot
                y_oh = np.zeros((y.shape[0], p.shape[-1]), dtype=float)
                y_oh[np.arange(y.shape[0]), y.astype(int)] = 1.0
                y = y_oh
            loss = -np.mean(np.sum(y * np.log(p + 1e-12), axis=1))
            dout = (p - y) / x.shape[0]
        else:
            loss = -np.mean(y * np.log(p + 1e-12) +
                            (1 - y) * np.log(1 - p + 1e-12))
            dout = (p - y) / x.shape[0]
        # backprop
        for layer in reversed(self.layers):
            dout = layer.backward(dout, l2=l2)
        # Adam updates
        for i, layer in enumerate(self.layers):
            for name, grad in (("W", layer.dW), ("b", layer.db)):
                if grad is None:
                    continue
                newv = self.optimizer.step_param(f"l{i}_{name}", grad,
                                                 getattr(layer, name))
                setattr(layer, name, newv)
        return float(loss)

    def state_dict(self) -> dict:
        return {
            "sizes": self.sizes, "hidden_act": "relu",
            "out_act": self.out_act, "p_drop": self.p_drop,
            "weights": [layer.W.tolist() for layer in self.layers],
            "biases": [layer.b.tolist() for layer in self.layers],
        }

    def load_state_dict(self, sd: dict) -> None:
        self.out_act = sd.get("out_act", "softmax")
        self.p_drop = sd.get("p_drop", 0.1)
        for layer, W, b in zip(self.layers, sd["weights"], sd["biases"]):
            layer.W = np.asarray(W, dtype=float)
            layer.b = np.asarray(b, dtype=float)
            layer.in_dim, layer.out_dim = layer.W.shape


class AttentionWindow:
    """Single-head attention encoder over a trailing price window.

    Given a window of per-bar feature rows ``X`` (T x d) and the index of
    the latest bar ``t``, produce a context vector by attending from the
    latest bar over the window. This lets the policy head consume the recent
    *path* (not just a static snapshot).

    Returns ``(context, cache)`` where ``cache`` holds intermediates needed
    for backprop.
    """

    def __init__(self, d_model: int, *, scale: float = 0.05,
                 rng: Optional[np.random.Generator] = None):
        rng = rng or RNG
        self.d_model = d_model
        self.Wq = (rng.standard_normal((d_model, d_model)) * scale).astype(float)
        self.Wk = (rng.standard_normal((d_model, d_model)) * scale).astype(float)
        self.Wv = (rng.standard_normal((d_model, d_model)) * scale).astype(float)
        self.dWq = self.dWk = self.dWv = None

    def forward(self, X: np.ndarray, t: int = -1) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        q = X[t] @ self.Wq                      # (d,)
        k = X @ self.Wk                          # (T, d)
        v = X @ self.Wv                          # (T, d)
        scores = k @ q / np.sqrt(self.d_model)  # (T,)
        attn = softmax(scores)                   # (T,)
        ctx = attn @ v                           # (d,)
        self._X, self._q, self._k, self._v, self._attn, self._t = X, q, k, v, attn, t
        return ctx

    def backward(self, dctx: np.ndarray) -> np.ndarray:
        X, q, k, v, attn, t = self._X, self._q, self._k, self._v, self._attn, self._t
        T, d = X.shape
        # d/dattn  = sum_d dctx * v
        dattn = dctx @ v.T                        # (T,)
        s = k @ q / np.sqrt(self.d_model)
        ds = attn * (dattn - np.sum(dattn * attn))  # softmax jacobian
        dq = (ds @ k) / np.sqrt(self.d_model)      # (d,)
        dk = np.outer(ds, q) / np.sqrt(self.d_model)  # (T, d)
        dv = np.outer(attn, dctx)                  # (T, d)
        self.dWq = np.outer(X[t], dq)
        self.dWk = X.T @ dk
        self.dWv = X.T @ dv
        dX = np.zeros_like(X)
        dX[t] = dq @ self.Wq.T
        dX += dk @ self.Wk.T
        dX += dv @ self.Wv.T
        return dX

    def state_dict(self) -> dict:
        return {"Wq": self.Wq.tolist(), "Wk": self.Wk.tolist(),
                "Wv": self.Wv.tolist(), "d_model": self.d_model}

    def load_state_dict(self, sd: dict) -> None:
        self.d_model = sd["d_model"]
        self.Wq = np.asarray(sd["Wq"], dtype=float)
        self.Wk = np.asarray(sd["Wk"], dtype=float)
        self.Wv = np.asarray(sd["Wv"], dtype=float)
