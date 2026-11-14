"""NexusBrain — feature engineering for the decision brain.

Builds a rich, normalised feature vector per (symbol, bar) that the brain's
neural encoders consume. It deliberately combines the three evidence families
the platform already collects:

  * **price-sequence context** — returns, momentum z, realised-vol z, EMA
    cross, RSI, ATR, Bollinger width (the market microstructure of the path)
  * **order-flow** — imbalance, delta, absorption bias
  * **footprint** — volume-at-price imbalance, POC volume share

plus regime one-hots and the legacy agent scores (kept as *features*, not as
the decision authority). Feature order is index-stable and versioned.

The brain's encoders normalise internally, so raw scales are acceptable, but
we clip outliers to keep gradients well-behaved.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

# Canonic, versioned feature layout. The trained models are tied to this
# order; changing it requires retraining.
FEATURE_NAMES: List[str] = [
    # price-sequence context (window summary)
    "ret_1", "ret_3", "ret_5", "ret_10",
    "momentum_z", "realised_vol_z",
    "ema_fast_slow", "rsi_norm", "atr_pct", "bb_width",
    # order flow
    "of_imbalance", "of_delta_pct", "of_absorption_bias",
    # footprint
    "fp_va_imbalance", "fp_poc_vol_pct",
    # regime (one-hot, 4 classes)
    "regime_trend_up", "regime_trend_down", "regime_high_vol", "regime_range",
    # legacy agent scores (kept as features)
    "trend_score", "meanrev_score", "breakout_score", "carry_score",
]

N_FEATURES = len(FEATURE_NAMES)
FEATURE_VERSION = "nexus_v1"


def _clip(x: float, lo: float = -8.0, hi: float = 8.0) -> float:
    if not math.isfinite(x):
        return 0.0
    return float(max(lo, min(hi, x)))


def _ema(values: Sequence[float], span: int) -> List[float]:
    if not values:
        return []
    out: List[float] = []
    a = 0.0
    k = 2.0 / (span + 1.0)
    first = True
    for v in values:
        if first:
            a = v
            first = False
        else:
            a = k * v + (1 - k) * a
        out.append(a)
    return out


def _rsi(closes: Sequence[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag = gains / period
    al = losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(highs: Sequence[float], lows: Sequence[float],
         closes: Sequence[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return float(np.mean(trs[-period:]))


def build_feature_vector(
    *,
    closes: Optional[Sequence[float]] = None,
    highs: Optional[Sequence[float]] = None,
    lows: Optional[Sequence[float]] = None,
    orderflow: Optional[Dict[str, float]] = None,
    footprint: Optional[Dict[str, float]] = None,
    regime: str = "",
    agent_scores: Optional[Dict[str, float]] = None,
    ret_1: float = 0.0,
    ret_3: float = 0.0,
    ret_5: float = 0.0,
    ret_10: float = 0.0,
    momentum_z: float = 0.0,
    realised_vol_z: float = 0.0,
) -> List[float]:
    """Assemble the feature vector in canonical order.

    ``closes``/``highs``/``lows`` are the trailing window; when provided the
    indicator features are computed from them. Optional explicit returns /
    z-scores override the computed values when supplied.
    """
    closes = list(closes or [])
    highs = list(highs or [])
    lows = list(lows or [])
    of = orderflow or {}
    fp = footprint or {}
    scores = agent_scores or {}

    # indicators from the window
    rsi = 50.0
    atr = 0.0
    ema_fast = ema_slow = 0.0
    bb_width = 0.0
    if closes:
        rsi = _rsi(closes)
        atr = _atr(highs or closes, lows or closes, closes)
        ef = _ema(closes, 9)
        es = _ema(closes, 21)
        if ef and es:
            ema_fast, ema_slow = ef[-1], es[-1]
        mid = float(np.mean(closes[-20:])) if closes else 0.0
        sd = float(np.std(closes[-20:])) if len(closes) > 1 else 0.0
        bb_width = (sd / mid) if mid else 0.0

    price = closes[-1] if closes else 0.0
    atr_pct = (atr / price) if (atr and price) else 0.0

    def _num(d: Dict[str, float], key: str, default: float = 0.0) -> float:
        v = d.get(key, default)
        try:
            return _clip(float(v))
        except (TypeError, ValueError):
            return default

    feats = [
        _clip(ret_1), _clip(ret_3), _clip(ret_5), _clip(ret_10),
        _clip(momentum_z), _clip(realised_vol_z),
        _clip(ema_fast - ema_slow), _clip((rsi - 50.0) / 25.0),
        _clip(atr_pct * 100.0), _clip(bb_width),
        _num(of, "imbalance"), _num(of, "delta_pct"),
        _num(of, "absorption_bias"),
        _num(fp, "va_imbalance"), _num(fp, "poc_vol_pct"),
        float(regime == "trend_up"), float(regime == "trend_down"),
        float(regime == "high_vol"), float(regime == "range"),
        _num(scores, "trend"), _num(scores, "meanrev"),
        _num(scores, "breakout"), _num(scores, "carry"),
    ]
    assert len(feats) == N_FEATURES, f"feature layout drift: {len(feats)}"
    return feats


def build_sequence_matrix(window_features: Sequence[Sequence[float]]) -> np.ndarray:
    """Stack a trailing window of per-bar feature vectors into an (T, d) matrix."""
    if not window_features:
        return np.zeros((0, N_FEATURES), dtype=float)
    return np.asarray(window_features, dtype=float)


def returns_from_closes(closes: Sequence[float], periods=(1, 3, 5, 10)
                        ) -> Dict[str, float]:
    out: Dict[str, float] = {}
    n = len(closes)
    for p in periods:
        if n >= p + 1 and closes[-(p + 1)] != 0:
            out[f"ret_{p}"] = closes[-1] / closes[-(p + 1)] - 1.0
        else:
            out[f"ret_{p}"] = 0.0
    return out
