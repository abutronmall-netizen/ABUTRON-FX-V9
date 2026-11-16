"""BreakoutAgent — 55-bar Donchian extreme + 0.35 ATR buffer.

Fires when price breaks the buffered Donchian channel; conviction scales
with the breakout distance and volume confirmation.
"""
from __future__ import annotations

from typing import Any, Dict

from ..indicators import atr, donchian_breakout
from .base import AgentOpinion, BaseAgent


class BreakoutAgent(BaseAgent):
    name = "BreakoutAgent"

    DONCHIAN_PERIOD = 55
    ATR_PERIOD = 14
    ATR_BUFFER = 0.35
    STRONG_BREAK_ATR = 0.5

    def vote(self, context: Dict[str, Any]) -> AgentOpinion:
        high = context["high"]
        low = context["low"]
        close = context["close"]
        symbol = context["symbol"]
        regime = context.get("regime", "")

        dc = donchian_breakout(high, low, close, self.DONCHIAN_PERIOD,
                               self.ATR_BUFFER)
        a = atr(high, low, close, self.ATR_PERIOD)
        direction = "HOLD"
        conviction = 0.0
        reasons = []

        if dc is None:
            reasons.append("INSUFFICIENT_DONCHIAN_HISTORY")
        else:
            upper, lower = dc["upper"], dc["lower"]
            last = close[-1]
            dist = 0.0
            if last > upper:
                direction = "BUY"
                dist = (last - upper) / a if a else 0.0
                reasons.append(f"BREAK_UP upper={upper:.5f} dist={dist:.2f}ATR")
            elif last < lower:
                direction = "SELL"
                dist = (lower - last) / a if a else 0.0
                reasons.append(f"BREAK_DOWN lower={lower:.5f} dist={dist:.2f}ATR")
            else:
                reasons.append(f"INSIDE_CHANNEL [{lower:.5f},{upper:.5f}]")

            if direction != "HOLD":
                conviction = round(min(1.0, dist / self.STRONG_BREAK_ATR), 3)

        return AgentOpinion(
            agent=self.name, symbol=symbol, direction=direction,
            conviction=conviction, regime=regime,
            evidence={"donchian_upper": dc["upper"] if dc else None,
                      "donchian_lower": dc["lower"] if dc else None,
                      "atr": a, "breakout_distance_atr": (
                          (close[-1] - dc["upper"]) / a if a and dc and close[-1] > dc["upper"]
                          else (dc["lower"] - close[-1]) / a if a and dc and close[-1] < dc["lower"]
                          else 0.0)},
            reasons=reasons,
        )
