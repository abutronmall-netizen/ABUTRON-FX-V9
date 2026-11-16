"""PortfolioAgent — combines regime-weighted agent opinions.

Aggregates BUY/SELL conviction from the three signal agents, weights by
regime suitability, resolves conflicting multi-timeframe signals to HOLD,
and produces the final ranked candidate list (conviction order, never
alphabetical symbol order).
"""
from __future__ import annotations

from typing import Any, Dict, List

from .base import AgentOpinion, BaseAgent


class PortfolioAgent(BaseAgent):
    name = "PortfolioAgent"

    REGIME_WEIGHTS = {
        "trend_up": {"trend": 1.0, "breakout": 0.8, "mean_reversion": 0.0},
        "trend_down": {"trend": 1.0, "breakout": 0.8, "mean_reversion": 0.0},
        "range": {"trend": 0.2, "breakout": 0.2, "mean_reversion": 1.0},
        "high_vol": {"trend": 0.4, "breakout": 1.0, "mean_reversion": 0.2},
    }
    MIN_CONVICTION = 0.25

    def vote(self, context: Dict[str, Any]) -> AgentOpinion:
        opinions: List[AgentOpinion] = context.get("opinions", [])
        symbol = context["symbol"]
        regime = context.get("regime", "range")
        weights = self.REGIME_WEIGHTS.get(regime, self.REGIME_WEIGHTS["range"])

        weighted = {"BUY": 0.0, "SELL": 0.0}
        breakdown: Dict[str, Dict[str, float]] = {}
        for op in opinions:
            w = weights.get(op.agent.replace("Agent", "").lower(), 0.0)
            contrib = op.conviction * w
            if op.direction in weighted:
                weighted[op.direction] += contrib
            breakdown[op.agent] = {"direction": op.direction,
                                   "conviction": op.conviction,
                                   "weight": w, "contribution": contrib}

        total = weighted["BUY"] + weighted["SELL"]
        direction = "HOLD"
        conviction = 0.0
        reasons = []
        if total > 0:
            if weighted["BUY"] >= weighted["SELL"]:
                direction = "BUY"
                conviction = weighted["BUY"] / total
            else:
                direction = "SELL"
                conviction = weighted["SELL"] / total
        else:
            reasons.append("NO_ALIGGNED_EVIDENCE")

        if conviction < self.MIN_CONVICTION:
            direction = "HOLD"
            reasons.append(f"CONVICTION_BELOW_THRESHOLD {conviction:.2f}")

        return AgentOpinion(
            agent=self.name, symbol=symbol, direction=direction,
            conviction=conviction, regime=regime,
            evidence={"weighted_buy": weighted["BUY"],
                      "weighted_sell": weighted["SELL"],
                      "breakdown": breakdown},
            reasons=reasons,
        )

    def rank(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Rank eligible candidates by conviction (blueprint: conviction
        order, not alphabetic symbol order)."""
        return sorted(candidates, key=lambda c: c.get("conviction", 0.0),
                      reverse=True)
