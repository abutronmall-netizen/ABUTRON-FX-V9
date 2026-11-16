"""Technical indicator library for the APEX agents.

All indicators operate on close-price series of closed candles only.
Implements the exact inputs named in the blueprint: ADX(14), EMA 20/55,
ATR, RSI14, Donchian(55) + 0.35 ATR buffer, 40-bar z-score, normalized
regression slope and realized-volatility percentile.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence


# ---------------------------------------------------------------------------
# Basic series
# ---------------------------------------------------------------------------
def sma(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------
def true_range(high: Sequence[float], low: Sequence[float],
               close: Sequence[float]) -> List[float]:
    tr: List[float] = []
    for i in range(len(close)):
        if i == 0:
            tr.append(high[i] - low[i])
        else:
            tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]),
                          abs(low[i] - close[i - 1])))
    return tr


def atr(high: Sequence[float], low: Sequence[float],
        close: Sequence[float], period: int = 14) -> Optional[float]:
    if len(close) < period:
        return None
    trs = true_range(high, low, close)
    return sum(trs[-period:]) / period


def realized_volatility(close: Sequence[float], period: int = 20) -> Optional[float]:
    """Std-dev of log returns over the last `period` bars."""
    if len(close) < period + 1:
        return None
    rets = [math.log(close[i] / close[i - 1])
            for i in range(len(close) - period, len(close))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(var)


def volatility_percentile(close: Sequence[float], lookback: int = 100,
                          period: int = 20) -> Optional[float]:
    """Percentile of current realized vol vs the trailing lookback window."""
    if len(close) < period + lookback:
        return None
    current = realized_volatility(close, period)
    history = []
    for i in range(period + lookback, len(close)):
        window = close[:i]
        v = realized_volatility(window, period)
        if v:
            history.append(v)
    if current is None or not history:
        return None
    below = sum(1 for h in history if h <= current)
    return below / len(history)


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------
def adx(high: Sequence[float], low: Sequence[float],
        close: Sequence[float], period: int = 14) -> Optional[float]:
    """Wilder's ADX."""
    if len(close) < period * 2:
        return None
    plus_dm: List[float] = []
    minus_dm: List[float] = []
    trs = true_range(high, low, close)
    for i in range(1, len(close)):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    if len(plus_dm) < period:
        return None
    atr_vals = trs[1:]
    atr_val = sum(atr_vals[:period]) / period
    if atr_val <= 0:
        return None
    plus_di = 100.0 * (sum(plus_dm[:period]) / period) / atr_val
    minus_di = 100.0 * (sum(minus_dm[:period]) / period) / atr_val
    dx = 100.0 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) else 0.0
    for i in range(period, len(plus_dm)):
        atr_val = (atr_vals[i] + (period - 1) * atr_val) / period
        pdi_i = 100.0 * plus_dm[i] / atr_val if atr_val else 0.0
        mdi_i = 100.0 * minus_dm[i] / atr_val if atr_val else 0.0
        dx = 100.0 * abs(pdi_i - mdi_i) / (pdi_i + mdi_i) if (pdi_i + mdi_i) else 0.0
    return dx


def normalized_slope(close: Sequence[float], period: int = 20) -> Optional[float]:
    """Linear-regression slope of close over `period`, normalized by price."""
    if len(close) < period:
        return None
    xs = list(range(period))
    ys = close[-period:]
    n = len(xs)
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom
    price = ys[-1]
    return slope / price if price else None


# ---------------------------------------------------------------------------
# Momentum / range
# ---------------------------------------------------------------------------
def rsi(close: Sequence[float], period: int = 14) -> Optional[float]:
    if len(close) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(close)):
        ch = close[i] - close[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    if len(gains) < period:
        return None
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))


def z_score(close: Sequence[float], period: int = 40) -> Optional[float]:
    if len(close) < period:
        return None
    window = close[-period:]
    mean = sum(window) / period
    var = sum((x - mean) ** 2 for x in window) / period
    std = math.sqrt(var)
    return (close[-1] - mean) / std if std else 0.0


def donchian(high: Sequence[float], low: Sequence[float],
             period: int = 55) -> Optional[Tuple[float, float]]:
    """(upper, lower) of the trailing period."""
    if len(high) < period:
        return None
    upper = max(high[-period:])
    lower = min(low[-period:])
    return upper, lower


def donchian_breakout(high: Sequence[float], low: Sequence[float],
                      close: Sequence[float], period: int = 55,
                      atr_buf_mult: float = 0.35) -> Optional[Dict]:
    """Donchian breakout with an ATR buffer (blueprint: 55-bar + 0.35 ATR)."""
    dc = donchian(high, low, period)
    a = atr(high, low, close, 14)
    if dc is None or a is None:
        return None
    upper, lower = dc
    return {
        "upper": upper + atr_buf_mult * a,
        "lower": lower - atr_buf_mult * a,
        "atr": a,
    }
