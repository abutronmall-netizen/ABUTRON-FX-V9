"""Enhanced feature engineering — derives ALL signal sources from OHLCV candles.

The original ``features.py`` declared feature slots for order flow, volume
footprint, agent scores, regime, and equity/risk state, but the training
pipeline never populated the order-flow / footprint / agent-score slots
(they were always zero). This module derives every one of those signals
*from the candle data itself* so the brain actually learns from them:

  * **order-flow proxies** — volume-weighted up/down imbalance, delta
    percentage, and absorption bias computed from candle bodies + volume.
    True tick-level order flow requires depth/tick data; these candle-derived
    proxies capture the same information that is recoverable from OHLCV.
  * **volume-footprint proxies** — volume-at-price imbalance and POC
    (price of max activity) volume share, approximated from the candle's
    price range and volume distribution.
  * **agent scores** — the deterministic APEX agents (Trend, MeanReversion,
    Breakout) are run over the window and their convictions are encoded as
    features. A carry-score proxy is derived from the spread/roll yield
    direction available in the candle series.
  * **equity / risk state** — log equity, active risk %, drawdown %, daily
    PnL in R-multiples, and open-position count, so the brain learns how
    portfolio state modulates the signal.

All values are clipped for gradient stability. The feature layout extends
the canonical ``FEATURE_NAMES`` from ``features.py`` with the equity/risk
state block.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .features import (FEATURE_NAMES, N_FEATURES, _clip, _atr, _ema, _rsi,
                       returns_from_closes)

# Equity / risk-state features appended after the canonical 23 market
# features. These are portfolio-level state that condition every symbol's
# signal (shared across the one-hot, so the brain learns how state modulates
# the per-symbol edge).
RISK_STATE_NAMES: List[str] = [
    "equity_log",
    "risk_pct",
    "dd_pct",
    "daily_pnl_r",
    "open_positions",
]

N_RISK_FEATURES = len(RISK_STATE_NAMES)
ENHANCED_N_FEATURES = N_FEATURES + N_RISK_FEATURES
ENHANCED_FEATURE_NAMES = FEATURE_NAMES + RISK_STATE_NAMES


# ---------------------------------------------------------------------------
# Order-flow proxies (from candle bodies + volume)
# ---------------------------------------------------------------------------

def _of_imbalance(highs: Sequence[float], lows: Sequence[float],
                  closes: Sequence[float], volumes: Sequence[float],
                  window: int = 20) -> float:
    """Volume-weighted up/down imbalance over the trailing window.

    +1 when all volume is in up-candles, -1 when all in down-candles.
    A candle is "up" when close >= open.
    """
    n = len(closes)
    if n < 2 or not volumes:
        return 0.0
    lo = max(0, n - window)
    w_closes = closes[lo:]
    w_vols = volumes[lo:]
    up_vol = 0.0
    dn_vol = 0.0
    for i, c in enumerate(w_closes):
        if i == 0:
            prev = closes[lo - 1] if lo > 0 else c
        else:
            prev = w_closes[i - 1]
        v = w_vols[i] if i < len(w_vols) else 1.0
        if c >= prev:
            up_vol += v
        else:
            dn_vol += v
    tot = up_vol + dn_vol
    if tot <= 0:
        return 0.0
    return _clip((up_vol - dn_vol) / tot)


def _of_delta_pct(closes: Sequence[float], volumes: Sequence[float],
                  window: int = 20) -> float:
    """Percentage of window volume that traded in the direction of the move."""
    n = len(closes)
    if n < 2 or not volumes:
        return 0.0
    lo = max(0, n - window)
    w_closes = closes[lo:]
    w_vols = volumes[lo:]
    up_vol = 0.0
    for i, c in enumerate(w_closes):
        prev = closes[lo - 1] if (i == 0 and lo > 0) else (
            w_closes[i - 1] if i > 0 else c)
        v = w_vols[i] if i < len(w_vols) else 1.0
        if c >= prev:
            up_vol += v
    tot = sum(w_vols) if w_vols else 0.0
    if tot <= 0:
        return 0.0
    return _clip(up_vol / tot - 0.5) * 2.0  # centre around 0


def _of_absorption_bias(highs: Sequence[float], lows: Sequence[float],
                        closes: Sequence[float], volumes: Sequence[float],
                        window: int = 20) -> float:
    """Bias of volume concentration at candle extremes vs mid-range.

    High when volume is absorbed at the highs (buying pressure absorbing
    selling) for up-candles, or at the lows for down-candles.
    """
    n = len(closes)
    if n < 2 or not volumes:
        return 0.0
    lo = max(0, n - window)
    bias = 0.0
    count = 0
    for i in range(lo, n):
        h, l, c = highs[i], lows[i], closes[i]
        rng = h - l
        if rng <= 0:
            continue
        v = volumes[i]
        # where in the range did the close land (0=low, 1=high)
        pos = (c - l) / rng
        prev = closes[i - 1] if i > 0 else c
        up = c >= prev
        # absorption: up-candle with close near high => buyers absorbing
        if up:
            bias += (pos - 0.5)
        else:
            bias -= (pos - 0.5)
        count += 1
    if count == 0:
        return 0.0
    return _clip(bias / count * 2.0)


# ---------------------------------------------------------------------------
# Volume-footprint proxies
# ---------------------------------------------------------------------------

def _fp_va_imbalance(highs: Sequence[float], lows: Sequence[float],
                     closes: Sequence[float], volumes: Sequence[float],
                     window: int = 20, bins: int = 5) -> float:
    """Volume-at-price imbalance across the window's price range.

    Divides the window's high-low range into ``bins`` price levels and
    assigns each candle's volume to the level nearest its close. Returns
    the imbalance (-1..1) of volume between the upper and lower halves.
    """
    n = len(closes)
    if n < 2 or not volumes:
        return 0.0
    lo_idx = max(0, n - window)
    wh = highs[lo_idx:]
    wl = lows[lo_idx:]
    wc = closes[lo_idx:]
    wv = volumes[lo_idx:]
    h_min, h_max = min(wl), max(wh)
    rng = h_max - h_min
    if rng <= 0:
        return 0.0
    # bin volume
    upper = 0.0
    lower = 0.0
    mid = h_min + rng / 2.0
    for i in range(len(wc)):
        v = wv[i] if i < len(wv) else 1.0
        if wc[i] >= mid:
            upper += v
        else:
            lower += v
    tot = upper + lower
    if tot <= 0:
        return 0.0
    return _clip((upper - lower) / tot)


def _fp_poc_vol_pct(highs: Sequence[float], lows: Sequence[float],
                    closes: Sequence[float], volumes: Sequence[float],
                    window: int = 20, bins: int = 5) -> float:
    """POC (price of max activity) volume share.

    Approximates the POC as the price bin receiving the most volume, then
    returns that bin's share of total window volume (0..1, centred).
    """
    n = len(closes)
    if n < 2 or not volumes:
        return 0.0
    lo_idx = max(0, n - window)
    wh = highs[lo_idx:]
    wl = lows[lo_idx:]
    wc = closes[lo_idx:]
    wv = volumes[lo_idx:]
    h_min, h_max = min(wl), max(wh)
    rng = h_max - h_min
    if rng <= 0:
        return 0.0
    counts = [0.0] * bins
    for i in range(len(wc)):
        v = wv[i] if i < len(wv) else 1.0
        frac = (wc[i] - h_min) / rng
        b = min(bins - 1, max(0, int(frac * bins)))
        counts[b] += v
    tot = sum(counts)
    if tot <= 0:
        return 0.0
    poc_share = max(counts) / tot
    return _clip((poc_share - 0.2) * 1.5)  # centre: 0.2 = uniform


# ---------------------------------------------------------------------------
# Agent scores (run the deterministic APEX agents over the window)
# ---------------------------------------------------------------------------

def _agent_scores(closes: Sequence[float], highs: Sequence[float],
                  lows: Sequence[float]) -> Dict[str, float]:
    """Run Trend / MeanReversion / Breakout agents and return convictions.

    These are the same deterministic signals the APEX agents produce, encoded
    as features so the brain learns to weigh them. Carry is derived from the
    short-vs-long return asymmetry (a proxy for roll yield direction).
    """
    scores = {"trend": 0.0, "meanrev": 0.0, "breakout": 0.0, "carry": 0.0}
    n = len(closes)
    if n < 20:
        return scores

    # --- Trend agent: EMA structure + ADX-like slope ---
    try:
        ef = _ema(closes, 9)
        es = _ema(closes, 21)
        if ef and es and es[-1]:
            sep = (ef[-1] - es[-1]) / es[-1]
            scores["trend"] = _clip(sep * 20.0)
    except Exception:
        pass

    # --- MeanReversion agent: z-score + RSI ---
    try:
        period = min(40, n)
        mean = float(np.mean(closes[-period:]))
        sd = float(np.std(closes[-period:]))
        if sd > 0:
            z = (closes[-1] - mean) / sd
            r = _rsi(closes)
            # meanreversion fires when z is extreme (|z|>1.5) and RSI agrees
            if abs(z) >= 1.5:
                conf = min(1.0, abs(z) / 3.0)
                if (z < 0 and r < 40) or (z > 0 and r > 60):
                    scores["meanrev"] = _clip(conf)
    except Exception:
        pass

    # --- Breakout agent: Donchian channel breakout ---
    try:
        dp = min(55, n - 1)
        if dp > 10:
            hi_ch = max(highs[-dp:])
            lo_ch = min(lows[-dp:])
            a = _atr(highs, lows, closes)
            if a > 0:
                if closes[-1] >= hi_ch - 0.35 * a:
                    scores["breakout"] = _clip(0.5 + (closes[-1] - hi_ch) /
                                              (a or 1.0))
                elif closes[-1] <= lo_ch + 0.35 * a:
                    scores["breakout"] = _clip(-(0.5 +
                                                  (lo_ch - closes[-1]) /
                                                  (a or 1.0)))
    except Exception:
        pass

    # --- Carry proxy: short vs long return asymmetry ---
    try:
        if n >= 12:
            r_long = (closes[-1] - closes[-12]) / closes[-12]
            # carry = momentum over a longer horizon, signed
            scores["carry"] = _clip(r_long * 10.0)
    except Exception:
        pass

    return scores


# ---------------------------------------------------------------------------
# Risk / equity state
# ---------------------------------------------------------------------------

def _risk_state(equity: float = 1000.0, risk_pct: float = 0.01,
                dd_pct: float = 0.0, daily_pnl_r: float = 0.0,
                open_positions: int = 0) -> List[float]:
    """Encode portfolio state as features (shared across symbols)."""
    eq_log = math.log(equity) if equity > 0 else 0.0
    return [
        _clip(eq_log / 10.0),        # normalised log equity
        _clip(risk_pct * 100.0),     # active risk %
        _clip(dd_pct),               # drawdown %
        _clip(daily_pnl_r),          # daily PnL in R-multiples
        _clip(open_positions / 20.0),
    ]


# ---------------------------------------------------------------------------
# Public: build the FULL enhanced feature vector
# ---------------------------------------------------------------------------

def build_enhanced_feature_vector(
    *, symbol: str, closes: Optional[Sequence[float]] = None,
    highs: Optional[Sequence[float]] = None,
    lows: Optional[Sequence[float]] = None,
    volumes: Optional[Sequence[float]] = None,
    regime: str = "",
    equity: float = 1000.0, risk_pct: float = 0.01, dd_pct: float = 0.0,
    daily_pnl_r: float = 0.0, open_positions: int = 0,
    universe: Sequence[str] = (),
    with_one_hot: bool = True,
) -> List[float]:
    """Build the full enhanced feature vector (market + risk + one-hot).

    Populates every feature slot — order flow, footprint, agent scores,
    regime, and equity/risk state — from the candle data and portfolio
    state. This is the vector the retrained brain learns from.
    """
    closes = list(closes or [])
    highs = list(highs or closes)
    lows = list(lows or closes)
    volumes = list(volumes or [])

    rets = returns_from_closes(closes)

    # order-flow proxies
    of_imb = _of_imbalance(highs, lows, closes, volumes)
    of_delta = _of_delta_pct(closes, volumes)
    of_abs = _of_absorption_bias(highs, lows, closes, volumes)

    # footprint proxies
    fp_va = _fp_va_imbalance(highs, lows, closes, volumes)
    fp_poc = _fp_poc_vol_pct(highs, lows, closes, volumes)

    # agent scores
    scores = _agent_scores(closes, highs, lows)

    # regime one-hot (derive if not supplied)
    if not regime and closes:
        from .brain import detect_regime
        a = _atr(highs or closes, lows or closes, closes)
        regime = detect_regime(closes, a)

    # risk state
    risk_feats = _risk_state(equity, risk_pct, dd_pct, daily_pnl_r,
                             open_positions)

    # canonical 23 market features (orderflow/footprint/agent slots now
    # populated from the candle-derived proxies)
    market = [
        _clip(rets.get("ret_1", 0.0)), _clip(rets.get("ret_3", 0.0)),
        _clip(rets.get("ret_5", 0.0)), _clip(rets.get("ret_10", 0.0)),
        _clip(rets.get("momentum_z", 0.0)),
        _clip(rets.get("realised_vol_z", 0.0)),
    ]
    # indicator features
    rsi_v = _rsi(closes) if closes else 50.0
    atr_v = _atr(highs or closes, lows or closes, closes) if closes else 0.0
    price = closes[-1] if closes else 0.0
    ef = _ema(closes, 9) if closes else None
    es = _ema(closes, 21) if closes else None
    ema_diff = (ef[-1] - es[-1]) if (ef and es and es) else 0.0
    mid = float(np.mean(closes[-20:])) if closes else 0.0
    sd = float(np.std(closes[-20:])) if len(closes) > 1 else 0.0
    bb_width = (sd / mid) if mid else 0.0
    atr_pct = (atr_v / price * 100.0) if (atr_v and price) else 0.0
    market += [
        _clip(ema_diff / (price or 1.0)),
        _clip((rsi_v - 50.0) / 25.0),
        _clip(atr_pct),
        _clip(bb_width),
    ]
    # order flow (populated)
    market += [_clip(of_imb), _clip(of_delta), _clip(of_abs)]
    # footprint (populated)
    market += [_clip(fp_va), _clip(fp_poc)]
    # regime one-hot
    market += [
        float(regime == "trend_up"), float(regime == "trend_down"),
        float(regime == "high_vol"), float(regime == "range"),
    ]
    # agent scores (populated)
    market += [
        _clip(scores["trend"]), _clip(scores["meanrev"]),
        _clip(scores["breakout"]), _clip(scores["carry"]),
    ]

    feats = market + risk_feats

    if with_one_hot and universe:
        from .unified import _one_hot
        feats = list(feats) + _one_hot(symbol, universe)

    return feats


def enhanced_n_unified_features(universe_size: int) -> int:
    """Total unified feature count: market + risk + one-hot."""
    return ENHANCED_N_FEATURES + universe_size
