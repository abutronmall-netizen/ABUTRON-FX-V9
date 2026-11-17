"""RiskAgent — the execution gate.

Given a candidate + account book + quote geometry, decides ALLOW / REDUCE
/ HOLD. Never creates a new signal. Enforces:
  * per-trade risk % of equity (default 0.15%, owner-configurable)
  * owner aggregate 20% USD stop-risk ceiling
  * valid broker SL/TP geometry (stop distance, tick size, point value)
  * costs (spread + commission)
  * DEMO/LIVE and effective-ON gating
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ...config import DEFAULT_PER_TRADE_RISK_PCT, DEFAULT_PORTFOLIO_RISK_CAP_PCT
from .base import AgentOpinion, BaseAgent


@dataclass
class RiskDecision:
    allow: bool
    reduced: bool
    volume: float
    stop_risk_usd: float
    reason: str
    detail: Dict[str, Any]


class RiskAgent(BaseAgent):
    name = "RiskAgent"

    # Geometry guards
    MAX_STOP_ATR_MULT = 4.0          # stop wider than 4x ATR is rejected
    MIN_STOP_TICKS = 1.0
    COST_REWARD_FRACTION = 0.5       # cost must not exceed 50% of reward
    SLIPPAGE_PADDING = 0.5           # extra ticks of slippage allowance

    def vote(self, context: Dict[str, Any]) -> AgentOpinion:
        dec = self.evaluate(context)
        direction = "HOLD" if dec.allow else "HOLD"
        conviction = 1.0 if dec.allow else 0.0
        return AgentOpinion(
            agent=self.name, symbol=context.get("symbol", ""),
            direction=direction, conviction=conviction,
            regime=context.get("regime", ""),
            evidence=dec.detail,
            reasons=[f"RISK_{dec.reason}"] + ([] if dec.allow else [dec.detail.get("block_reason", "")]),
        )

    def evaluate(self, ctx: Dict[str, Any]) -> RiskDecision:
        candidate = ctx.get("candidate", {})
        quote = ctx.get("quote")
        account = ctx.get("account", {})
        owner_book = ctx.get("owner_book", {})
        price = ctx.get("price")
        atr = ctx.get("atr")
        direction = candidate.get("direction", "")

        equity = float(account.get("equity", account.get("balance", 0.0)) or 0.0)
        per_trade_pct = float(account.get("per_trade_risk_pct",
                                          DEFAULT_PER_TRADE_RISK_PCT))
        cap_pct = float(account.get("portfolio_cap_pct",
                                    DEFAULT_PORTFOLIO_RISK_CAP_PCT))

        detail: Dict[str, Any] = {"equity": equity, "per_trade_pct": per_trade_pct}
        if (owner_book.get("aggregate_stop_risk_usd") is None
                or owner_book.get("owner_risk_certified") is False):
            return RiskDecision(False, False, 0.0, 0.0, "HOLD",
                                detail | {"block_reason": "OWNER_NATIVE_RISK_UNVERIFIED"})
        stop_risk_usd = float(candidate.get("stop_risk_usd", 0.0) or 0.0)
        volume = float(candidate.get("volume", 0.0) or 0.0)

        # --- Effective ON gate ------------------------------------------
        if not account.get("effective_on"):
            return RiskDecision(False, False, 0.0, stop_risk_usd, "HOLD",
                                detail | {"block_reason": "EFFECTIVE_OFF"})
        # --- Quote / geometry ------------------------------------------
        if quote is None:
            return RiskDecision(False, False, 0.0, stop_risk_usd, "HOLD",
                                detail | {"block_reason": "NO_QUOTE"})
        tick = quote.tick_size
        point = quote.point
        point_value = float(quote.point_value or 0.0)
        if not tick or not point or not point_value:
            return RiskDecision(False, False, 0.0, stop_risk_usd, "HOLD",
                                detail | {"block_reason": "CONTRACT_GEOMETRY_MISSING"})

        sl = candidate.get("stop_loss")
        tp = candidate.get("take_profit")
        if sl is None or tp is None:
            return RiskDecision(False, False, 0.0, stop_risk_usd, "HOLD",
                                detail | {"block_reason": "MISSING_SL_TP"})

        stop_dist_ticks = abs(float(price) - float(sl)) / tick
        tp_dist_ticks = abs(float(tp) - float(price)) / tick
        if stop_dist_ticks < self.MIN_STOP_TICKS:
            return RiskDecision(False, False, 0.0, stop_risk_usd, "HOLD",
                                detail | {"block_reason": "STOP_TOO_TIGHT",
                                          "stop_ticks": stop_dist_ticks})
        if atr and stop_dist_ticks > self.MAX_STOP_ATR_MULT * (atr / point):
            return RiskDecision(False, False, 0.0, stop_risk_usd, "HOLD",
                                detail | {"block_reason": "STOP_TOO_WIDE",
                                          "stop_atr_mult": stop_dist_ticks * point / atr})

        # --- Volume sizing from per-trade risk --------------------------
        # point_value is USD per point per lot; a "point" equals one tick
        # here, so risk per lot = stop distance in points × point value.
        risk_per_lot = stop_dist_ticks * point_value
        if risk_per_lot <= 0:
            return RiskDecision(False, False, 0.0, stop_risk_usd, "HOLD",
                                detail | {"block_reason": "BAD_RISK_PER_LOT"})
        target_risk = equity * per_trade_pct
        sizing_volume = target_risk / risk_per_lot
        # Round to broker lot step — FLOOR, never round upward. Rounding
        # up can push actual_risk above the approved per-trade budget.
        # If the floored lot is below the minimum, reject the trade.
        import math
        lot_step = float(quote.volume_min or 0.01)
        sizing_volume = math.floor(sizing_volume / lot_step) * lot_step
        if sizing_volume < lot_step:
            return RiskDecision(False, False, 0.0, stop_risk_usd, "HOLD",
                                detail | {"block_reason": "MIN_LOT_OVER_RISK",
                                          "target_risk_usd": target_risk,
                                          "risk_per_lot": risk_per_lot})
        sizing_volume = min(sizing_volume, float(quote.volume_max or sizing_volume))

        # Recompute stop risk at sized volume — must not exceed the target.
        # No 5% tolerance: the approved risk fraction is a hard ceiling.
        actual_stop_risk = sizing_volume * risk_per_lot
        if actual_stop_risk > target_risk + 1e-9:
            sizing_volume = math.floor(target_risk / risk_per_lot / lot_step) * lot_step
            if sizing_volume < lot_step:
                return RiskDecision(False, False, 0.0, stop_risk_usd, "HOLD",
                                    detail | {"block_reason": "MIN_LOT_OVER_RISK",
                                              "target_risk_usd": target_risk,
                                              "risk_per_lot": risk_per_lot})
            actual_stop_risk = sizing_volume * risk_per_lot

        # --- Owner aggregate cap ---------------------------------------
        aggregate = float(owner_book.get("aggregate_stop_risk_usd", 0.0) or 0.0)
        cap_usd = equity * cap_pct
        if aggregate + actual_stop_risk > cap_usd + 1e-9:
            # Reduce volume to fit headroom, if any
            headroom = cap_usd - aggregate
            if headroom <= 0:
                return RiskDecision(False, False, 0.0, stop_risk_usd, "HOLD",
                                    detail | {"block_reason": "OWNER_CAP_EXCEEDED",
                                              "cap_usd": cap_usd,
                                              "aggregate": aggregate})
            sizing_volume = math.floor(headroom / risk_per_lot / lot_step) * lot_step
            if sizing_volume < lot_step:
                return RiskDecision(False, False, 0.0, stop_risk_usd, "HOLD",
                                    detail | {"block_reason": "OWNER_CAP_TIGHT",
                                              "cap_usd": cap_usd,
                                              "aggregate": aggregate,
                                              "headroom_usd": headroom})
            actual_stop_risk = sizing_volume * risk_per_lot
            reduced = sizing_volume < volume

        # --- Cost drag (compared to expected reward) ------------------
        # point_value is USD per point per lot, so reward = tp points ×
        # point_value × volume (no extra tick factor).
        round_turn_cost = float(quote.commission or 0.0) * sizing_volume
        reward_usd = tp_dist_ticks * point_value * sizing_volume
        if reward_usd > 0 and round_turn_cost > reward_usd * self.COST_REWARD_FRACTION:
            return RiskDecision(False, False, 0.0, stop_risk_usd, "HOLD",
                                detail | {"block_reason": "COST_DRIFT",
                                          "round_turn_cost": round_turn_cost,
                                          "reward_usd": reward_usd})

        reduced = sizing_volume < volume - 1e-9
        detail.update({
            "sizing_volume": sizing_volume, "risk_per_lot": risk_per_lot,
            "stop_ticks": stop_dist_ticks, "tp_ticks": tp_dist_ticks,
            "target_risk_usd": target_risk, "cap_usd": cap_usd,
            "aggregate_before": aggregate, "round_turn_cost": round_turn_cost,
        })
        return RiskDecision(True, reduced, sizing_volume, actual_stop_risk,
                            "REDUCED" if reduced else "ALLOW", detail)
