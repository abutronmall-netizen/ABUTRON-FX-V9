"""TrendAgent — EMA 20/55 structure + ADX + ATR.

Direction follows EMA structure; conviction scales with ADX trend
strength and EMA separation.
"""
from __future__ import annotations

from typing import Any, Dict

from ..indicators import adx, atr, ema
from .base import AgentOpinion, BaseAgent


class TrendAgent(BaseAgent):
    name = "TrendAgent"

    FAST = 20
    SLOW = 55
    ADX_PERIOD = 14
    ATR_PERIOD = 14
    STRONG_ADX = 30.0
    WEAK_ADX = 20.0
    EMA_SEP_STRONG = 0.004

    def vote(self, context: Dict[str, Any]) -> AgentOpinion:
        close = context["close"]
        high = context["high"]
        low = context["low"]
        symbol = context["symbol"]
        regime = context.get("regime", "")

        e_fast = ema(close, self.FAST)
        e_slow = ema(close, self.SLOW)
        adx_val = adx(high, low, close, self.ADX_PERIOD)
        a = atr(high, low, close, self.ATR_PERIOD)

        direction = "HOLD"
        conviction = 0.0
        reasons = []
        sep = None
        if e_fast is None or e_slow is None:
            reasons.append("INSUFFICIENT_EMA_HISTORY")
        elif e_slow == 0:
            reasons.append("ZERO_SLOW_EMA")
        else:
            sep = (e_fast - e_slow) / e_slow
            if regime in ("trend_up", "high_vol") and e_fast > e_slow:
                direction = "BUY"
                reasons.append(f"EMA_FAST>SLOW sep={sep:.4f}")
            elif regime in ("trend_down", "high_vol") and e_fast < e_slow:
                direction = "SELL"
                reasons.append(f"EMA_FAST<SLOW sep={sep:.4f}")

        # Strength
        if direction != "HOLD" and adx_val is not None:
            base = (adx_val - self.WEAK_ADX) / (self.STRONG_ADX - self.WEAK_ADX)
            base = max(0.0, min(1.0, base))
            sep_boost = min(1.0, abs(sep or 0.0) / self.EMA_SEP_STRONG)
            conviction = round(0.6 * base + 0.4 * sep_boost, 3)

        return AgentOpinion(
            agent=self.name, symbol=symbol, direction=direction,
            conviction=conviction, regime=regime,
            evidence={"ema_fast": e_fast, "ema_slow": e_slow,
                      "ema_separation": sep, "adx": adx_val, "atr": a},
            reasons=reasons,
        )
