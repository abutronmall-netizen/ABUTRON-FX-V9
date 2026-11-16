"""MeanReversionAgent — 40-bar z-score + RSI14.

Counter-extreme strategy: fades statistically extreme moves once
confirmed by RSI. Only acts in the 'range' regime (fading trends is
the known failure mode, so we gate on regime).
"""
from __future__ import annotations

from typing import Any, Dict

from ..indicators import rsi, z_score
from .base import AgentOpinion, BaseAgent


class MeanReversionAgent(BaseAgent):
    name = "MeanReversionAgent"

    Z_PERIOD = 40
    RSI_PERIOD = 14
    Z_EXTREME = 2.0
    RSI_EXTREME_BUY = 25.0     # price washed out -> expect bounce
    RSI_EXTREME_SELL = 75.0    # price extended -> expect drop
    RSI_CONFIRM = 0.75

    def vote(self, context: Dict[str, Any]) -> AgentOpinion:
        close = context["close"]
        symbol = context["symbol"]
        regime = context.get("regime", "")

        z = z_score(close, self.Z_PERIOD)
        r = rsi(close, self.RSI_PERIOD)

        direction = "HOLD"
        conviction = 0.0
        reasons = []
        if regime != "range":
            reasons.append(f"REGIME_GATE:{regime}_NO_FADE")
        elif z is None or r is None:
            reasons.append("INSUFFICIENT_HISTORY")
        else:
            # Counter-extreme: extreme down + oversold -> BUY; extreme up
            # + overbought -> SELL. Confirmation required by RSI alignment.
            if z <= -self.Z_EXTREME and r <= self.RSI_EXTREME_BUY:
                direction = "BUY"
                conviction = round(min(1.0, abs(z) / 3.0) * self.RSI_CONFIRM, 3)
                reasons.append(f"Z={z:.2f} RSI={r:.1f} EXTREME_DOWN_WASHOUT")
            elif z >= self.Z_EXTREME and r >= self.RSI_EXTREME_SELL:
                direction = "SELL"
                conviction = round(min(1.0, abs(z) / 3.0) * self.RSI_CONFIRM, 3)
                reasons.append(f"Z={z:.2f} RSI={r:.1f} EXTREME_UP_EXTENSION")
            else:
                reasons.append(f"Z={z:.2f} RSI={r:.1f} WITHIN_RANGE")

        return AgentOpinion(
            agent=self.name, symbol=symbol, direction=direction,
            conviction=conviction, regime=regime,
            evidence={"z_score": z, "rsi": r},
            reasons=reasons,
        )
