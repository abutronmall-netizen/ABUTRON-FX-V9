"""Feature vector builder for the self-learning edge model.

Builds a normalised-ready feature vector for one instrument per bar,
drawing from: order-flow imbalance, footprint bias, regime, the signal
agents' scores, realised volatility, and short momentum. The online model
normalises these internally, so raw scales are fine.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


# Canonical feature order (index-stable; the model is tied to this layout)
FEATURE_NAMES: List[str] = [
    "orderflow_imbalance",
    "orderflow_delta_pct",
    "orderflow_absorption_bias",
    "footprint_va_imbalance",
    "footprint_poc_vol_pct",
    "regime_trend_up",
    "regime_trend_down",
    "regime_high_vol",
    "regime_range",
    "trend_score",
    "meanrev_score",
    "breakout_score",
    "carry_score",
    "momentum_z",
    "realised_vol_z",
]

N_FEATURES = len(FEATURE_NAMES)


def build_feature_vector(*,
                         orderflow: Optional[Dict[str, float]] = None,
                         footprint: Optional[Dict[str, float]] = None,
                         regime: str = "",
                         agent_scores: Optional[Dict[str, float]] = None,
                         momentum_z: float = 0.0,
                         realised_vol_z: float = 0.0) -> List[float]:
    """Assemble the feature vector in canonical order."""
    of = orderflow or {}
    fp = footprint or {}
    scores = agent_scores or {}

    # regime one-hot
    regime_feats = {
        "trend_up": 0.0, "trend_down": 0.0, "high_vol": 0.0, "range": 0.0}
    regime_feats[regime] = 1.0

    return [
        of.get("imbalance", 0.0),
        of.get("delta_pct", 0.0),
        of.get("absorption_bias", 0.0),
        fp.get("va_imbalance", 0.0),
        fp.get("poc_volume_pct", 0.0),
        regime_feats["trend_up"],
        regime_feats["trend_down"],
        regime_feats["high_vol"],
        regime_feats["range"],
        scores.get("trend", 0.0),
        scores.get("meanrev", 0.0),
        scores.get("breakout", 0.0),
        scores.get("carry", 0.0),
        momentum_z,
        realised_vol_z,
    ]


def feature_vector_for_candle(candle: Dict[str, Any], *,
                              orderflow: Optional[Dict[str, float]] = None,
                              footprint: Optional[Dict[str, float]] = None,
                              regime: str = "",
                              agent_scores: Optional[Dict[str, float]] = None
                              ) -> List[float]:
    """Convenience: build features from a candle dict + overlay context.

    Computes a short momentum z from the candle's own OHLC as a fallback
    when no deeper history is supplied.
    """
    h = float(candle.get("high", 0.0))
    l = float(candle.get("low", 0.0))
    c = float(candle.get("close", 0.0))
    range_ = h - l
    momentum_z = 0.0
    if range_ > 0 and c > 0:
        # position of close within the bar, scaled
        momentum_z = ((c - l) / range_ - 0.5) * 4.0
    return build_feature_vector(orderflow=orderflow, footprint=footprint,
                                regime=regime, agent_scores=agent_scores,
                                momentum_z=momentum_z,
                                realised_vol_z=0.0)
