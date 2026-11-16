"""Causal signal data for the NexusBrain.

The original training scripts passed ``{}`` for order flow, footprint and
Gemma/agent scores, which left nine feature slots permanently at zero. This
module makes those slots real and keeps **training and inference identical**.

Two evidence modes are supported:

* **True market microstructure** — ``trades.csv.gz`` tick prints with an
  aggressor side, and optional ``depth.csv.gz`` snapshots. These drive the
  existing ``OrderFlowEngine`` / ``VolumeFootprintBuilder``.
* **OHLCV proxies** — when no tick/depth evidence exists, deterministic
  point-in-time proxies are derived from candle volume (or range when volume
  is absent), close location value, wicks, and a volume-at-price histogram.

The proxies are *not* claimed to be exchange depth or real footprint. Their
source is recorded in every generated bundle and in the brain provenance. A
Gemma gateway can optionally supply agent trust weights; when it is disabled
or unavailable, the four agent scores are produced by deterministic APEX-style
technical models. No network call is made by default.

Every value at bar ``i`` is computed only from bars ``0..i``.
"""
from __future__ import annotations

import csv
import gzip
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .features import FEATURE_NAMES, N_FEATURES, build_feature_vector, returns_from_closes

SIGNAL_DATA_VERSION = "signals_v2_causal_proxy_v1"
SIGNAL_SLOT_NAMES = (
    "of_imbalance", "of_delta_pct", "of_absorption_bias",
    "fp_va_imbalance", "fp_poc_vol_pct",
    "trend_score", "meanrev_score", "breakout_score", "carry_score",
)
SIGNAL_SLOT_INDICES = tuple(FEATURE_NAMES.index(n) for n in SIGNAL_SLOT_NAMES)
AGENT_KEYS = ("trend", "meanrev", "breakout", "carry")


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    return float(max(lo, min(hi, v)))


def _field(obj: Any, name: str, default: Any = 0.0) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _float_field(obj: Any, name: str, default: float = 0.0) -> float:
    try:
        return float(_field(obj, name, default) or 0.0)
    except (TypeError, ValueError):
        return default


def _epoch_to_iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(epoch)))


def _bar_key(value: Any) -> str:
    """Normalize a bar-open time to the loader's ISO-UTC key."""
    s = str(value or "").strip()
    if not s:
        return ""
    try:
        v = float(s)
        if v > 1e11:
            v /= 1000.0
        return _epoch_to_iso(v)
    except ValueError:
        return s


def _open_csv(path: str):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return open(path, "rt", encoding="utf-8-sig", newline="")


def _side(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in ("buy", "b", "bid", "1", "long", "aggressor_buy"):
        return "buy"
    if s in ("sell", "s", "ask", "0", "-1", "short", "aggressor_sell"):
        return "sell"
    return "buy" if s.startswith("b") else "sell"


def load_tick_trades(path: str, *, symbol: str = "") -> Dict[str, List[Any]]:
    """Load optional true tick prints, grouped by bar-open time.

    Accepted columns (aliases in parentheses): ``symbol``, ``bar_open_utc``
    (``bar_open``/``bar_time``), ``price``, ``size`` (``volume``/``lots``),
    and ``side`` (``aggressor_side``). Times may be epoch seconds, epoch
    milliseconds, or ISO-UTC strings.
    """
    from ..analytics.orderflow import Trade

    out: Dict[str, List[Trade]] = {}
    if not path or not os.path.isfile(path):
        return out
    with _open_csv(path) as fh:
        reader = csv.DictReader(fh)
        cols = {str(k).strip().lower(): k for k in (reader.fieldnames or [])}

        def col(*names: str) -> str:
            for n in names:
                if n in cols:
                    return cols[n]
            return ""

        sym_c = col("symbol", "instrument")
        bar_c = col("bar_open_utc", "bar_open", "bar_time", "open_time")
        price_c = col("price", "px", "last")
        size_c = col("size", "volume", "lots", "qty", "quantity")
        side_c = col("side", "aggressor_side", "aggressor")
        time_c = col("time_utc", "time", "ts")
        if not bar_c or not price_c:
            return out
        for row in reader:
            row_sym = str(row.get(sym_c, symbol) or symbol).strip() if sym_c else symbol
            if symbol and row_sym and row_sym != symbol:
                continue
            key = _bar_key(row.get(bar_c, ""))
            if not key:
                continue
            try:
                price = float(row.get(price_c, 0) or 0)
                size = float(row.get(size_c, 0) or 0) if size_c else 1.0
            except (TypeError, ValueError):
                continue
            if price <= 0 or size <= 0:
                continue
            out.setdefault(key, []).append(Trade(
                price=price, size=size,
                side=_side(row.get(side_c, "buy") if side_c else "buy"),
                time=str(row.get(time_c, "") if time_c else ""),
            ))
    return out


def load_depth_signals(path: str, *, symbol: str = "") -> Dict[str, Dict[str, float]]:
    """Load optional depth snapshots into per-bar order-flow features.

    Accepted columns: ``symbol``, ``bar_open_utc``, ``bid_volume``,
    ``ask_volume`` and optionally ``price_change`` / ``best_bid`` /
    ``best_ask``. Depth gives real queue imbalance but not a true trade
    footprint; footprint proxies remain in use unless tick trades are given.
    """
    out: Dict[str, Dict[str, float]] = {}
    if not path or not os.path.isfile(path):
        return out
    with _open_csv(path) as fh:
        reader = csv.DictReader(fh)
        cols = {str(k).strip().lower(): k for k in (reader.fieldnames or [])}

        def col(*names: str) -> str:
            for n in names:
                if n in cols:
                    return cols[n]
            return ""

        sym_c = col("symbol", "instrument")
        bar_c = col("bar_open_utc", "bar_open", "bar_time", "open_time")
        bid_c = col("bid_volume", "bid_size", "bid_qty")
        ask_c = col("ask_volume", "ask_size", "ask_qty")
        chg_c = col("price_change", "delta_price", "close_change")
        if not bar_c or not bid_c or not ask_c:
            return out
        for row in reader:
            row_sym = str(row.get(sym_c, symbol) or symbol).strip() if sym_c else symbol
            if symbol and row_sym and row_sym != symbol:
                continue
            key = _bar_key(row.get(bar_c, ""))
            if not key:
                continue
            try:
                bid = max(0.0, float(row.get(bid_c, 0) or 0))
                ask = max(0.0, float(row.get(ask_c, 0) or 0))
                chg = float(row.get(chg_c, 0) or 0) if chg_c else 0.0
            except (TypeError, ValueError):
                continue
            total = bid + ask
            imbalance = (ask - bid) / total if total else 0.0
            # Absorption: heavy resting liquidity opposed to the realised
            # price change is a conservative absorption proxy.
            absorption = -imbalance * _clip(1.0 - abs(chg), 0.0, 1.0) if chg_c else 0.0
            out[key] = {
                "imbalance": _clip(imbalance),
                "delta_pct": _clip(imbalance),
                "absorption_bias": _clip(absorption),
                "source": "depth",
            }
    return out


def _proxy_volume(o: float, h: float, l: float, c: float,
                  volume: float, atr: float, price_scale: float) -> float:
    """Use broker volume if present; otherwise a bounded range/body proxy."""
    if volume and volume > 0 and math.isfinite(volume):
        return float(min(1e9, max(1e-9, volume)))
    ref = max(atr, abs(c) * 1e-5, price_scale, 1e-9)
    return float(1.0 + min(20.0, ((h - l) + abs(c - o)) / ref))


def _proxy_orderflow(*, open_: float, high: float, low: float, close: float,
                     prev_close: float, volume: float, atr: float,
                     price_scale: float,
                     rolling_volume: float) -> Dict[str, float]:
    """Close-location-volume + wick absorption proxy for one closed bar."""
    rng = max(high - low, 1e-12)
    clv = ((close - low) - (high - close)) / rng
    # A perfectly symmetric doji still carries the close-to-close auction
    # direction. Without this residual the order-flow slots can stay zero.
    drift = (close - prev_close) / max(atr, rng, price_scale, 1e-12)
    clv = _clip(0.70 * _clip(clv) + 0.30 * _clip(2.0 * drift))
    buy = volume * (1.0 + clv) * 0.5
    sell = volume * (1.0 - clv) * 0.5
    total = max(buy + sell, 1e-12)
    delta = buy - sell
    denom = max(rolling_volume, volume, 1e-9)
    lower_wick = min(close, open_) - low
    upper_wick = high - max(close, open_)
    wick_total = max(lower_wick + upper_wick, 1e-12)
    body_frac = min(1.0, abs(close - open_) / rng)
    absorption = (((lower_wick - upper_wick) / wick_total) *
                  (1.0 - body_frac) + 0.15 * clv)
    return {
        "imbalance": _clip(delta / total),
        "delta_pct": _clip(delta / denom),
        "absorption_bias": _clip(absorption),
        "buy_volume": buy,
        "sell_volume": sell,
        "source": "ohlcv_proxy",
    }


def _proxy_footprint(*, open_: float, high: float, low: float, close: float,
                     volume: float, flow_imbalance: float = 0.0,
                     bins: int = 12) -> Dict[str, float]:
    """Triangular volume-at-price histogram, POC share and VA imbalance."""
    if high <= low:
        high = low + max(abs(close) * 1e-6, 1e-9)
    mids = np.linspace(low, high, bins, dtype=float)
    center = (high + low) * 0.5
    span = max(high - low, 1e-12)
    weights = np.clip(1.0 - np.abs(mids - center) / (span * 0.5 + 1e-12), 0.02, 1.0)
    # Tilt the profile toward the close (where auction acceptance happened).
    weights *= 0.75 + 0.25 * np.clip(1.0 - np.abs(mids - close) / span, 0.0, 1.0)
    wsum = float(weights.sum()) or 1.0
    cell_vol = volume * weights / wsum
    rng = max(high - low, 1e-12)
    clv = _clip(0.70 * _clip(((close - low) - (high - close)) / rng) +
                0.30 * _clip(flow_imbalance))
    buy_share = 0.5 + 0.5 * clv
    buy = cell_vol * buy_share
    sell = cell_vol * (1.0 - buy_share)
    total = float(cell_vol.sum()) or 1.0
    poc = int(np.argmax(cell_vol))
    poc_pct = float(cell_vol[poc] / total)
    # Value area: highest-volume cells covering 70% of the bar's volume.
    order = np.argsort(cell_vol)[::-1]
    target = 0.70 * total
    acc = 0.0
    va_idx: List[int] = []
    for idx in order:
        va_idx.append(int(idx))
        acc += float(cell_vol[idx])
        if acc >= target:
            break
    va_buy = float(buy[va_idx].sum())
    va_sell = float(sell[va_idx].sum())
    va_total = max(va_buy + va_sell, 1e-12)
    return {
        "va_imbalance": _clip((va_buy - va_sell) / va_total),
        "poc_vol_pct": _clip(poc_pct, 0.0, 1.0),
        "poc_price": float(mids[poc]),
        "source": "ohlcv_proxy",
    }


def _rolling_mean(x: np.ndarray, period: int) -> np.ndarray:
    out = np.zeros_like(x, dtype=float)
    cs = np.concatenate(([0.0], np.cumsum(x, dtype=float)))
    if len(x) >= period:
        out[period - 1:] = (cs[period:] - cs[:-period]) / float(period)
    return out


def _rolling_z(x: np.ndarray, period: int) -> np.ndarray:
    out = np.zeros_like(x, dtype=float)
    n = len(x)
    if n < period:
        return out
    cs = np.concatenate(([0.0], np.cumsum(x, dtype=float)))
    css = np.concatenate(([0.0], np.cumsum(x * x, dtype=float)))
    idx = np.arange(period, n + 1, dtype=int)
    s = cs[idx] - cs[idx - period]
    ss = css[idx] - css[idx - period]
    mean = s / period
    var = np.maximum(ss / period - mean * mean, 0.0)
    std = np.sqrt(var) + 1e-12
    vals = (x[period - 1:] - mean) / std
    out[period - 1:] = np.nan_to_num(vals, nan=0.0, posinf=8.0, neginf=-8.0)
    return np.clip(out, -8.0, 8.0)


def _ema_series(x: np.ndarray, period: int) -> np.ndarray:
    out = np.zeros_like(x, dtype=float)
    if len(x) < period:
        return out
    k = 2.0 / (period + 1.0)
    e = float(np.mean(x[:period]))
    out[period - 1] = e
    for i in range(period, len(x)):
        e = k * float(x[i]) + (1.0 - k) * e
        out[i] = e
    return out


def _rsi_series(x: np.ndarray, period: int = 14) -> np.ndarray:
    out = np.full(len(x), 50.0, dtype=float)
    if len(x) <= period:
        return out
    gains = np.maximum(np.diff(x), 0.0)
    losses = np.maximum(-np.diff(x), 0.0)
    ag = float(np.mean(gains[:period]))
    al = float(np.mean(losses[:period]))
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + float(gains[i])) / period
        al = (al * (period - 1) + float(losses[i])) / period
        idx = i + 1
        out[idx] = 100.0 if al <= 1e-12 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def _atr_series(h: np.ndarray, l: np.ndarray, c: np.ndarray,
                period: int = 14) -> np.ndarray:
    n = len(c)
    tr = np.zeros(n, dtype=float)
    tr[0] = max(0.0, float(h[0] - l[0]))
    if n > 1:
        tr[1:] = np.maximum.reduce((
            h[1:] - l[1:],
            np.abs(h[1:] - c[:-1]),
            np.abs(l[1:] - c[:-1]),
        ))
    return _rolling_mean(tr, period)


def _adx_series(h: np.ndarray, l: np.ndarray, c: np.ndarray,
                tr: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(c)
    dx = np.zeros(n, dtype=float)
    if n <= period + 1:
        return dx
    pdm = np.maximum(h[1:] - h[:-1], 0.0)
    mdm = np.maximum(l[:-1] - l[1:], 0.0)
    plus = np.where((pdm > mdm) & (pdm > 0), pdm, 0.0)
    minus = np.where((mdm > pdm) & (mdm > 0), mdm, 0.0)
    str_ = float(np.mean(tr[1:period + 1]))
    sp = float(np.mean(plus[:period]))
    sm = float(np.mean(minus[:period]))
    for i in range(period, len(plus)):
        str_ = (float(tr[i + 1]) + (period - 1) * str_) / period
        sp = (float(plus[i]) + (period - 1) * sp) / period
        sm = (float(minus[i]) + (period - 1) * sm) / period
        pdi = 100.0 * sp / (str_ + 1e-12)
        mdi = 100.0 * sm / (str_ + 1e-12)
        dx[i + 1] = 100.0 * abs(pdi - mdi) / (pdi + mdi + 1e-12)
    adx = np.zeros(n, dtype=float)
    if n >= period * 2:
        adx[period * 2 - 1] = float(np.mean(dx[period:period * 2]))
        alpha = 1.0 / period
        for i in range(period * 2, n):
            adx[i] = alpha * dx[i] + (1.0 - alpha) * adx[i - 1]
    return adx


def _rolling_indicators(o: np.ndarray, h: np.ndarray, l: np.ndarray,
                        c: np.ndarray) -> Dict[str, np.ndarray]:
    """All indicators are causal: row i uses only bars 0..i."""
    n = len(c)
    tr = np.zeros(n, dtype=float)
    tr[0] = max(0.0, float(h[0] - l[0]))
    if n > 1:
        tr[1:] = np.maximum.reduce((
            h[1:] - l[1:], np.abs(h[1:] - c[:-1]),
            np.abs(l[1:] - c[:-1])))
    atr = _rolling_mean(tr, 14)
    logret = np.zeros(n, dtype=float)
    if n > 1:
        with np.errstate(divide="ignore", invalid="ignore"):
            logret[1:] = np.log(np.maximum(c[1:], 1e-12) /
                                np.maximum(c[:-1], 1e-12))
    rv = np.zeros(n, dtype=float)
    if n >= 21:
        sw = np.lib.stride_tricks.sliding_window_view(logret[1:], 20)
        rv[20:] = sw.std(axis=1)
    slope = np.zeros(n, dtype=float)
    if n >= 20:
        x = np.arange(20, dtype=float) - 9.5
        sw = np.lib.stride_tricks.sliding_window_view(c, 20)
        slope[19:] = (sw @ x) / float(x @ x) / np.maximum(c[19:], 1e-12)
    z40 = np.zeros(n, dtype=float)
    if n >= 40:
        cs = np.concatenate(([0.0], np.cumsum(c)))
        css = np.concatenate(([0.0], np.cumsum(c * c)))
        idx = np.arange(40, n + 1)
        s = cs[idx] - cs[idx - 40]
        ss = css[idx] - css[idx - 40]
        mean = s / 40.0
        std = np.sqrt(np.maximum(ss / 40.0 - mean * mean, 0.0)) + 1e-12
        z40[39:] = np.clip((c[39:] - mean) / std, -8.0, 8.0)
    upper = np.zeros(n, dtype=float)
    lower = np.zeros(n, dtype=float)
    if n >= 55:
        hu = np.lib.stride_tricks.sliding_window_view(h, 55).max(axis=1)
        ll = np.lib.stride_tricks.sliding_window_view(l, 55).min(axis=1)
        upper[54:] = hu
        lower[54:] = ll
        upper[:54] = hu[0] if len(hu) else 0.0
        lower[:54] = ll[0] if len(ll) else 0.0
    return {
        "atr": atr,
        "rsi": _rsi_series(c, 14),
        "ema20": _ema_series(c, 20),
        "ema55": _ema_series(c, 55),
        "adx": _adx_series(h, l, c, tr, 14),
        "vol": rv,
        "vol_z": _rolling_z(rv, 100),
        "slope": slope,
        "slope_z": _rolling_z(slope, 100),
        "z40": z40,
        "donchian_upper": upper,
        "donchian_lower": lower,
    }


def _regime_series(ind: Dict[str, np.ndarray]) -> List[str]:
    out: List[str] = []
    n = len(ind["atr"])
    for i in range(n):
        if i < 60:
            out.append("range")
        elif ind["vol_z"][i] > 1.0:
            out.append("high_vol")
        elif ind["adx"][i] >= 25.0 and ind["slope"][i] > 0.0008:
            out.append("trend_up")
        elif ind["adx"][i] >= 25.0 and ind["slope"][i] < -0.0008:
            out.append("trend_down")
        else:
            out.append("range")
    return out


def _deterministic_agent_scores(o: np.ndarray, h: np.ndarray,
                                l: np.ndarray, c: np.ndarray,
                                ind: Dict[str, np.ndarray],
                                regimes: Sequence[str]) -> List[Dict[str, float]]:
    """APEX-style trend / mean-reversion / breakout / carry proxies.

    Small residual terms keep every slot informative in quiet markets. The
    ``carry`` slot is a slow-drift proxy, not an interest-rate carry; true
    carry needs swap/rate data and is explicitly documented as unavailable.
    """
    n = len(c)
    e20, e55 = ind["ema20"], ind["ema55"]
    sep = (e20 - e55) / np.maximum(np.abs(e55), 1e-12)
    trend_strength = (0.6 * np.clip((ind["adx"] - 20.0) / 10.0, 0.0, 1.0) +
                      0.4 * np.clip(np.abs(sep) / 0.004, 0.0, 1.0))
    trend = np.zeros(n, dtype=float)
    for i in range(n):
        reg = regimes[i]
        sign = 0.0
        if reg in ("trend_up", "high_vol") and sep[i] > 0:
            sign = 1.0
        elif reg in ("trend_down", "high_vol") and sep[i] < 0:
            sign = -1.0
        trend[i] = _clip(sign * trend_strength[i] + 0.08 * math.tanh(sep[i] / 0.002))
    z = ind["z40"]
    rsi = ind["rsi"]
    upper, lower, atr = (ind["donchian_upper"], ind["donchian_lower"],
                         ind["atr"])
    span = np.maximum(upper - lower, np.maximum(atr, 1e-12))
    mid = (upper + lower) * 0.5
    meanrev = np.zeros(n, dtype=float)
    breakout = np.zeros(n, dtype=float)
    for i in range(n):
        reg = regimes[i]
        meanrev[i] = -0.15 * _clip(z[i] / 3.0)
        if reg == "range" and z[i] <= -2.0 and rsi[i] <= 25.0:
            meanrev[i] += min(1.0, abs(z[i]) / 3.0) * 0.75
        elif reg == "range" and z[i] >= 2.0 and rsi[i] >= 75.0:
            meanrev[i] -= min(1.0, abs(z[i]) / 3.0) * 0.75
        meanrev[i] = _clip(meanrev[i])
        base = 0.20 * _clip((c[i] - mid[i]) / (span[i] + 1e-12))
        buf = 0.35 * atr[i]
        up_dist = c[i] - (upper[i] + buf)
        dn_dist = (lower[i] - buf) - c[i]
        if upper[i] and up_dist > 0:
            base += min(1.0, up_dist / max(0.5 * atr[i], 1e-12))
        elif lower[i] and dn_dist > 0:
            base -= min(1.0, dn_dist / max(0.5 * atr[i], 1e-12))
        breakout[i] = _clip(base)
    carry = np.clip(ind["slope"] * 400.0, -1.0, 1.0)
    return [
        {"trend": float(trend[i]), "meanrev": float(meanrev[i]),
         "breakout": float(breakout[i]), "carry": float(carry[i])}
        for i in range(n)
    ]


def _apply_gemma_overlay(scores: List[Dict[str, float]], *,
                         symbol: str, closes: Sequence[float],
                         highs: Sequence[float], lows: Sequence[float],
                         regimes: Sequence[str]) -> Tuple[List[Dict[str, float]], Dict[str, Any]]:
    """Optionally ask a configured Gemma gateway for agent trust weights."""
    from ..apex.gemma import gemma_score_advisor

    compact = {
        "symbol": symbol,
        "bars": len(closes),
        "last_close": float(closes[-1]) if closes else 0.0,
        "recent_closes": [round(float(v), 6) for v in list(closes)[-40:]],
        "recent_highs": [round(float(v), 6) for v in list(highs)[-20:]],
        "recent_lows": [round(float(v), 6) for v in list(lows)[-20:]],
        "regime_counts": {r: regimes.count(r) for r in set(regimes)},
        "deterministic_mean_abs": {
            k: round(float(np.mean([abs(s[k]) for s in scores])), 4)
            for k in AGENT_KEYS
        },
    }
    overlay = gemma_score_advisor.score_window(compact)
    source = overlay.get("source", "deterministic_apex")
    if source != "gemma_gateway":
        return scores, {"source": "deterministic_apex",
                        "gemma_reason": overlay.get("reason", "GEMMA_DISABLED"),
                        "model": overlay.get("model", "")}
    weights = overlay.get("weights", {}) or {}
    biases = overlay.get("scores", {}) or {}
    for row in scores:
        for key in AGENT_KEYS:
            w = float(weights.get(key, 1.0))
            b = float(biases.get(key, 0.0))
            row[key] = _clip(row[key] * w + 0.20 * b)
        # Carry has no rate feed; use the model's explicit carry score.
        row["carry"] = _clip(row["carry"] * 0.5 +
                             0.5 * float(biases.get("carry", 0.0)))
    return scores, {"source": "gemma_gateway", "model": overlay.get("model", ""),
                    "weights": {k: float(weights.get(k, 1.0)) for k in AGENT_KEYS}}


def audit_market_features(rows: Sequence[Sequence[float]]) -> Dict[str, Any]:
    """Prove the nine formerly-dead slots are actually populated."""
    x = np.asarray(rows, dtype=float)
    slots: Dict[str, Dict[str, float]] = {}
    for name, idx in zip(SIGNAL_SLOT_NAMES, SIGNAL_SLOT_INDICES):
        col = x[:, idx] if x.ndim == 2 and x.shape[0] else np.zeros(0)
        nonzero = float(np.mean(np.abs(col) > 1e-9)) if len(col) else 0.0
        slots[name] = {
            "index": int(idx),
            "nonzero_ratio": round(nonzero, 6),
            "mean": round(float(np.mean(col)) if len(col) else 0.0, 6),
            "std": round(float(np.std(col)) if len(col) else 0.0, 6),
            "min": round(float(np.min(col)) if len(col) else 0.0, 6),
            "max": round(float(np.max(col)) if len(col) else 0.0, 6),
        }
    return {
        "version": SIGNAL_DATA_VERSION,
        "rows": int(x.shape[0]) if x.ndim == 2 else 0,
        "market_features": int(x.shape[1]) if x.ndim == 2 else 0,
        "slots": slots,
        "all_slots_nonzero": all(v["nonzero_ratio"] > 0.0 for v in slots.values()),
    }


@dataclass
class SignalBundle:
    symbol: str
    closes: List[float]
    highs: List[float]
    lows: List[float]
    opens: List[float]
    volumes: List[float]
    bar_times: List[str]
    orderflow: List[Dict[str, float]]
    footprint: List[Dict[str, float]]
    regimes: List[str]
    agent_scores: List[Dict[str, float]]
    market_features: List[List[float]]
    sources: Dict[str, Any] = field(default_factory=dict)
    audit: Dict[str, Any] = field(default_factory=dict)

    @property
    def n_bars(self) -> int:
        return len(self.closes)

    def at(self, index: int) -> Dict[str, Any]:
        if not self.n_bars:
            return {"orderflow": {}, "footprint": {}, "regime": "",
                    "agent_scores": {}, "market_features": [], "sources": self.sources}
        raw = int(index)
        i = (max(0, self.n_bars + raw) if raw < 0
             else min(self.n_bars - 1, raw))
        return {
            "index": i,
            "bar_time": self.bar_times[i],
            "orderflow": dict(self.orderflow[i]),
            "footprint": dict(self.footprint[i]),
            "regime": self.regimes[i],
            "agent_scores": dict(self.agent_scores[i]),
            "market_features": list(self.market_features[i]),
            "sources": self.sources,
        }

    def training_dict(self) -> Dict[str, Any]:
        return {
            "closes": self.closes, "highs": self.highs, "lows": self.lows,
            "opens": self.opens, "volumes": self.volumes,
            "orderflow": self.orderflow, "footprint": self.footprint,
            "regimes": self.regimes, "agent_scores": self.agent_scores,
            "market_features": self.market_features,
            "signal_sources": self.sources, "feature_audit": self.audit,
        }


def build_signal_bundle(
    candles: Sequence[Any], *, symbol: str = "",
    tick_trades: Optional[Dict[str, List[Any]]] = None,
    depth: Optional[Dict[str, Dict[str, float]]] = None,
    use_gemma: bool = False, window: int = 120,
) -> SignalBundle:
    """Build a causal signal bundle for a candle series."""
    n = len(candles)
    if not n:
        return SignalBundle(symbol, [], [], [], [], [], [], [], [], [], [], [],
                            {"version": SIGNAL_DATA_VERSION, "bars": 0},
                            audit_market_features([]))
    opens = np.asarray([_float_field(c, "open", _float_field(c, "close")) for c in candles], dtype=float)
    highs = np.asarray([_float_field(c, "high", _float_field(c, "close")) for c in candles], dtype=float)
    lows = np.asarray([_float_field(c, "low", _float_field(c, "close")) for c in candles], dtype=float)
    closes = np.asarray([_float_field(c, "close") for c in candles], dtype=float)
    raw_vol = np.asarray([_float_field(c, "volume") for c in candles], dtype=float)
    points = np.asarray([_float_field(c, "point", 1e-4) for c in candles], dtype=float)
    times = [str(_field(c, "open_time", "") or "") for c in candles]
    sym = symbol or str(_field(candles[0], "symbol", "") or "")
    ind = _rolling_indicators(opens, highs, lows, closes)
    regimes = _regime_series(ind)
    scores = _deterministic_agent_scores(opens, highs, lows, closes, ind, regimes)
    if use_gemma:
        scores, gemma_source = _apply_gemma_overlay(
            scores, symbol=sym, closes=closes, highs=highs, lows=lows,
            regimes=regimes)
    else:
        gemma_source = {"source": "deterministic_apex",
                        "reason": "GEMMA_NOT_REQUESTED_FOR_THIS_PASS"}

    tick_trades = tick_trades or {}
    depth = depth or {}
    of_rows: List[Dict[str, float]] = []
    fp_rows: List[Dict[str, float]] = []
    volumes: List[float] = []
    true_of_bars = true_fp_bars = depth_bars = proxy_bars = 0
    from ..analytics.orderflow import OrderFlowEngine
    from ..analytics.footprint import VolumeFootprintBuilder
    of_engine = OrderFlowEngine()
    rolling_vols: List[float] = []
    for i in range(n):
        price_scale = abs(points[i]) if points[i] > 0 else max(abs(closes[i]) * 1e-5, 1e-9)
        vol = _proxy_volume(float(opens[i]), float(highs[i]), float(lows[i]),
                            float(closes[i]), float(raw_vol[i]),
                            float(ind["atr"][i]), price_scale)
        volumes.append(vol)
        rolling_vols.append(vol)
        rolling_mean = float(np.mean(rolling_vols[-20:])) if rolling_vols else vol
        key = times[i]
        trades = tick_trades.get(key) or []
        of: Dict[str, float]
        fp: Dict[str, float]
        if trades:
            snap = of_engine.aggregate(sym, key, "", trades)
            of = dict(snap.features)
            builder = VolumeFootprintBuilder(max(price_scale, 1e-9))
            builder._anchor = min(float(t.price) for t in trades)
            footprint = builder.build(sym, trades)
            fp = dict(footprint.features)
            fp.setdefault("poc_vol_pct",
                          fp.get("poc_volume_pct", 0.0))
            of["source"] = "true_tick_trades"
            fp["source"] = "true_tick_trades"
            true_of_bars += 1
            true_fp_bars += 1
        else:
            if key in depth:
                of = dict(depth[key])
                of.setdefault("source", "depth")
                depth_bars += 1
            else:
                of = _proxy_orderflow(
                    open_=float(opens[i]), high=float(highs[i]),
                    low=float(lows[i]), close=float(closes[i]),
                    prev_close=float(closes[i - 1]) if i else float(opens[i]),
                    volume=vol, atr=float(ind["atr"][i]),
                    price_scale=price_scale, rolling_volume=rolling_mean)
                proxy_bars += 1
            fp = _proxy_footprint(
                open_=float(opens[i]), high=float(highs[i]),
                low=float(lows[i]), close=float(closes[i]), volume=vol,
                flow_imbalance=float(of.get("imbalance", 0.0)))
        of_rows.append(of)
        fp_rows.append(fp)

    market_features: List[List[float]] = []
    w = max(30, int(window))
    for i in range(n):
        lo = max(0, i - w + 1)
        sl = slice(lo, i + 1)
        close_window = closes[sl].tolist()
        rets = returns_from_closes(close_window)
        feats = build_feature_vector(
            closes=close_window, highs=highs[sl].tolist(),
            lows=lows[sl].tolist(),
            orderflow=of_rows[i], footprint=fp_rows[i], regime=regimes[i],
            agent_scores=scores[i],
            momentum_z=float(ind["slope_z"][i]),
            realised_vol_z=float(ind["vol_z"][i]), **rets)
        market_features.append(feats)

    audit = audit_market_features(market_features)
    sources = {
        "version": SIGNAL_DATA_VERSION,
        "symbol": sym, "bars": n, "window": w,
        "orderflow": {"true_tick_bars": true_of_bars,
                      "depth_bars": depth_bars, "proxy_bars": proxy_bars},
        "footprint": {"true_tick_bars": true_fp_bars,
                      "proxy_bars": n - true_fp_bars},
        "gemma": gemma_source,
        "honest_limitations": [
            "OHLCV proxies are not exchange depth or true volume footprint",
            "carry is a slow-drift proxy; no swap/rate feed is present",
            "Gemma gateway scores are optional; deterministic APEX-style scores are the offline fallback",
        ],
    }
    return SignalBundle(
        symbol=sym, closes=closes.tolist(), highs=highs.tolist(),
        lows=lows.tolist(), opens=opens.tolist(), volumes=volumes,
        bar_times=times, orderflow=of_rows, footprint=fp_rows,
        regimes=regimes, agent_scores=scores, market_features=market_features,
        sources=sources, audit=audit)


def latest_signal_features(candles: Sequence[Any], *, symbol: str = "",
                           tick_trades: Optional[Dict[str, List[Any]]] = None,
                           depth: Optional[Dict[str, Dict[str, float]]] = None,
                           use_gemma: bool = False) -> Dict[str, Any]:
    """Compute the last bar's features using a bounded causal history."""
    tail = list(candles)[-160:]
    bundle = build_signal_bundle(tail, symbol=symbol, tick_trades=tick_trades,
                                 depth=depth, use_gemma=use_gemma, window=120)
    return bundle.at(-1)


def features_for_brain(brain: Any, market_features: Sequence[float],
                       *, symbol: str) -> List[float]:
    """Adapt a 23-dim market vector to the brain's expected input width."""
    feats = [float(v) for v in market_features]
    expected = 0
    try:
        expected = int(brain.ensemble.members[0].model.sizes[0])
    except Exception:
        expected = N_FEATURES
    if len(feats) == expected:
        return feats
    if hasattr(brain, "unified_features_from_market"):
        return brain.unified_features_from_market(feats, symbol)
    if len(feats) > expected:
        return feats[:expected]
    return feats + [0.0] * (expected - len(feats))


def online_learn_from_bundle(brain: Any, *, symbol: str,
                             bundle: SignalBundle, index: int,
                             realised_return: float) -> Dict[str, Any]:
    """One causal online gradient step using the exact generated features."""
    if not bundle.n_bars:
        return {"updated": False, "reason": "NO_DATA"}
    raw = int(index)
    i = (max(0, bundle.n_bars + raw) if raw < 0
         else min(bundle.n_bars - 1, raw))
    feats = features_for_brain(brain, bundle.market_features[i], symbol=symbol)
    if hasattr(brain, "online_learn_features"):
        return brain.online_learn_features(feats, realised_return, symbol=symbol)
    return brain.online_learn(
        symbol=symbol, closes=bundle.closes[: i + 1],
        highs=bundle.highs[: i + 1], lows=bundle.lows[: i + 1],
        orderflow=bundle.orderflow[i], footprint=bundle.footprint[i],
        agent_scores=bundle.agent_scores[i], regime=bundle.regimes[i],
        realised_return=realised_return)
