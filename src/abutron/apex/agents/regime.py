"""RegimeAgent — ADX(14), normalized slope, realized-vol percentile.

Classifies the market as trend_up / trend_down / range / high_vol.
This is the context every other agent runs in; it never votes direction
directly, only defines the regime.
"""
from __future__ import annotations

from typing import Any, Dict

from ..indicators import adx, normalized_slope, volatility_percentile
from .base import AgentOpinion, BaseAgent


class RegimeAgent(BaseAgent):
    name = "RegimeAgent"

    # Parameters (blueprint: ADX 14, slope 20, realized-vol percentile)
    ADX_PERIOD = 14
    SLOPE_PERIOD = 20
    VOL_PERIOD = 20
    VOL_LOOKBACK = 100
    TREND_ADX = 25.0
    HIGH_VOL_PCT = 0.85
    TREND_SLOPE = 0.0008

    def vote(self, context: Dict[str, Any]) -> AgentOpinion:
        high = context["high"]
        low = context["low"]
        close = context["close"]
        symbol = context["symbol"]

        adx_val = adx(high, low, close, self.ADX_PERIOD)
        slope = normalized_slope(close, self.SLOPE_PERIOD)
        vol_pct = volatility_percentile(close, self.VOL_LOOKBACK,
                                        self.VOL_PERIOD)

        regime = "range"
        reasons = []
        if adx_val is None:
            regime = "unknown"
            reasons.append("INSUFFICIENT_HISTORY")
        elif vol_pct is not None and vol_pct >= self.HIGH_VOL_PCT:
            regime = "high_vol"
            reasons.append(f"REALIZED_VOL_PERCENTILE={vol_pct:.2f}")
        elif adx_val >= self.TREND_ADX:
            if slope is not None and slope > self.TREND_SLOPE:
                regime = "trend_up"
                reasons.append(f"ADX={adx_val:.1f} SLOPE={slope:.5f}")
            elif slope is not None and slope < -self.TREND_SLOPE:
                regime = "trend_down"
                reasons.append(f"ADX={adx_val:.1f} SLOPE={slope:.5f}")
            else:
                regime = "range"
                reasons.append(f"ADX={adx_val:.1f} FLAT_SLOPE")
        else:
            regime = "range"
            reasons.append(f"ADX={adx_val:.1f} BELOW_TREND_THRESHOLD")

        return AgentOpinion(
            agent=self.name, symbol=symbol, direction="HOLD",
            conviction=0.0, regime=regime,
            evidence={"adx": adx_val, "normalized_slope": slope,
                      "realized_vol_percentile": vol_pct},
            reasons=reasons,
        )
