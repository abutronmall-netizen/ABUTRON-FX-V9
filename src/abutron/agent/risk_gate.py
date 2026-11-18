"""Deterministic, unbypassable risk gate.

The ``RiskGate`` is the backbone of the agentic system and the
embodiment of the rule that **V9 is the main system**. Every action that
can affect money or broker state passes through it, and it is never
bypassable — not by the reasoning layer, not by an LLM, not by a live
toggle.

It enforces the V9 money-safety invariants:

* per-trade risk cap (``DEFAULT_PER_TRADE_RISK_PCT``, 0.15%)
* owner portfolio ceiling (``DEFAULT_PORTFOLIO_RISK_CAP_PCT``, 20%)
* reward:risk ratio floor (R:R >= 1.50)
* stop-loss / take-profit geometry sanity

It wraps the existing ``RiskGuard`` / ``RiskEngine`` when available, and
*independently* re-enforces the hard invariants from the config
constants directly — so even if the risk modules are unavailable
(offline, CI, sandbox) the gate still blocks bad actions. The gate can
never be talked out of a rejection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from ..config import (
    DEFAULT_PER_TRADE_RISK_PCT, DEFAULT_PORTFOLIO_RISK_CAP_PCT,
)
from .interfaces import AgentAction, AgentContext, Direction, Verdict

#: Minimum reward:risk ratio required to approve a new position.
MIN_REWARD_RISK_RATIO = 1.50


@dataclass
class GateResult:
    """Outcome of the risk gate for one action."""
    verdict: Verdict
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.verdict in (Verdict.APPROVE, Verdict.SHADOW)


class RiskGate:
    """The unbypassable deterministic risk gate."""

    def __init__(
        self,
        risk_guard: Any = None,
        risk_engine: Any = None,
        portfolio: Optional[Dict[str, Any]] = None,
        per_trade_pct: float = DEFAULT_PER_TRADE_RISK_PCT,
        portfolio_cap_pct: float = DEFAULT_PORTFOLIO_RISK_CAP_PCT,
    ) -> None:
        self._guard = risk_guard
        self._engine = risk_engine
        self._portfolio = portfolio or {}
        self.per_trade_pct = per_trade_pct
        self.portfolio_cap_pct = portfolio_cap_pct
        self._guard_available = risk_guard is not None
        if risk_guard is None:
            self._try_load_guard()

    # ------------------------------------------------------------------ #
    def _try_load_guard(self) -> None:
        try:
            from ..risk.riskguard import RiskGuard
            self._guard = RiskGuard()
            self._guard_available = True
        except Exception:  # noqa: BLE001 - gate still enforces invariants
            self._guard_available = False

    @property
    def guard_available(self) -> bool:
        return self._guard_available

    # ------------------------------------------------------------------ #
    def evaluate(self, action: AgentAction,
                 context: AgentContext) -> GateResult:
        """Evaluate an action against the risk invariants.

        Read-only / record actions (``requires_gate=False``) are approved
        for recording without money checks. Everything else is subjected
        to the full gate. The portfolio is taken from the context (the
        gate's own ``_portfolio`` acts only as a default/floor).
        """
        # Effective portfolio: context portfolio wins; gate default fills
        # in anything the context omits.
        portfolio = dict(self._portfolio)
        portfolio.update(context.portfolio or {})

        if not action.requires_gate:
            return GateResult(Verdict.SHADOW, "read_only_action",
                              {"action": action.kind})

        # 1. Geometry sanity: a position needs a stop and a target.
        geo = self._check_geometry(action)
        if not geo.approved:
            return geo

        # 2. Reward:risk ratio floor.
        rr = self._check_reward_risk(action)
        if not rr.approved:
            return rr

        # 3. Delegate to the V9 RiskGuard when available (portfolio,
        #    concentration, per-trade cap, exposure).
        if self._guard_available and self._guard is not None:
            try:
                guard_result = self._guard.check_trade(
                    self._to_trade_proposal(action),
                    self._to_portfolio_state(portfolio))
                if getattr(guard_result, "approved", True) is False:
                    return GateResult(
                        Verdict.REJECT,
                        str(getattr(guard_result, "reason",
                                     "RISK_GUARD_REJECTED")),
                        {"guard": guard_result.to_dict()
                         if hasattr(guard_result, "to_dict") else {}},
                    )
            except Exception as exc:  # noqa: BLE001
                # Guard errored — fail closed.
                return GateResult(Verdict.REJECT,
                                  f"RISK_GUARD_ERROR:{type(exc).__name__}")

        # 4. Independent hard-invariant re-check from config constants,
        #    so the gate is never bypassed even without the guard object.
        hard = self._check_hard_invariants(action, context, portfolio)
        if not hard.approved:
            return hard

        return GateResult(Verdict.APPROVE, "all_invariants_passed",
                          {"guard_available": self._guard_available})

    # ------------------------------------------------------------------ #
    # Invariant checks
    # ------------------------------------------------------------------ #
    def _check_geometry(self, action: AgentAction) -> GateResult:
        if action.direction is Direction.HOLD:
            return GateResult(Verdict.REJECT, "HOLD_NO_POSITION")
        if action.stop_loss <= 0:
            return GateResult(Verdict.REJECT, "MISSING_STOP_LOSS")
        if action.take_profit <= 0:
            return GateResult(Verdict.REJECT, "MISSING_TAKE_PROFIT")
        if action.quantity <= 0:
            return GateResult(Verdict.REJECT, "NON_POSITIVE_QUANTITY")
        return GateResult(Verdict.APPROVE, "geometry_ok")

    def _check_reward_risk(self, action: AgentAction) -> GateResult:
        if action.stop_loss <= 0 or action.take_profit <= 0:
            return GateResult(Verdict.REJECT, "RR_NO_LEGS")
        risk = abs(action.price - action.stop_loss)
        reward = abs(action.take_profit - action.price)
        if risk <= 0:
            return GateResult(Verdict.REJECT, "RR_ZERO_RISK")
        ratio = reward / risk
        if ratio < MIN_REWARD_RISK_RATIO:
            return GateResult(
                Verdict.REJECT,
                f"REWARD_RISK_BELOW_FLOOR:{ratio:.3f}<{MIN_REWARD_RISK_RATIO}",
                {"ratio": round(ratio, 4),
                 "min_required": MIN_REWARD_RISK_RATIO})
        return GateResult(Verdict.APPROVE, "reward_risk_ok",
                          {"ratio": round(ratio, 4)})

    def _check_hard_invariants(self, action: AgentAction,
                               context: AgentContext,
                               portfolio: Dict[str, Any]) -> GateResult:
        """Re-enforce the money-safety invariants from config constants.

        This runs *after* the RiskGuard so the gate is doubly protected:
        even a misconfigured or absent guard cannot let a violation through.
        """
        equity = float(portfolio.get("equity", 0.0))
        if equity > 0:
            max_risk_usd = equity * self.per_trade_pct
            # Estimate stop risk from price geometry * quantity. We use the
            # reported stop_risk_usd if present, else approximate.
            stop_risk = float(action.payload.get("stop_risk_usd", 0.0))
            if stop_risk <= 0 and action.stop_loss > 0:
                risk_per_unit = abs(action.price - action.stop_loss)
                stop_risk = risk_per_unit * action.quantity
            if stop_risk > max_risk_usd + 1e-9:
                return GateResult(
                    Verdict.REJECT,
                    f"PER_TRADE_RISK_EXCEEDED:{stop_risk:.2f}"
                    f">{max_risk_usd:.2f}",
                    {"stop_risk_usd": round(stop_risk, 2),
                     "max_risk_usd": round(max_risk_usd, 2),
                     "per_trade_pct": self.per_trade_pct})

            # Owner portfolio ceiling: not inferred from one account, but
            # enforced if the portfolio reports a fraction already at risk.
            at_risk_frac = float(portfolio.get("risk_fraction", 0.0))
            if at_risk_frac > self.portfolio_cap_pct + 1e-9:
                return GateResult(
                    Verdict.REJECT,
                    f"PORTFOLIO_CEILING_EXCEEDED:{at_risk_frac:.4f}"
                    f">{self.portfolio_cap_pct:.4f}",
                    {"risk_fraction": at_risk_frac,
                     "cap_pct": self.portfolio_cap_pct})
        return GateResult(Verdict.APPROVE, "hard_invariants_ok")

    # ------------------------------------------------------------------ #
    # Adapters to the V9 RiskGuard types
    # ------------------------------------------------------------------ #
    def _to_trade_proposal(self, action: AgentAction) -> Any:
        from ..risk.riskguard import TradeProposal
        # stop_risk_usd is required > 0 by the guard; estimate from
        # price/stop geometry * quantity when not supplied.
        stop_risk = float(action.payload.get("stop_risk_usd", 0.0))
        if stop_risk <= 0 and action.stop_loss > 0 and action.price > 0:
            stop_risk = abs(action.price - action.stop_loss) * action.quantity
        return TradeProposal(
            symbol=action.symbol,
            direction=action.direction.value,
            volume_lots=float(action.quantity),
            price=float(action.price),
            stop_loss=float(action.stop_loss),
            take_profit=float(action.take_profit),
            stop_risk_usd=stop_risk,
        )

    def _to_portfolio_state(self, portfolio: Dict[str, Any]) -> Any:
        from ..risk.riskguard import PortfolioState
        exposures = portfolio.get("exposure_by_symbol")
        if exposures is None:
            exposures = portfolio.get("exposures", {})
        return PortfolioState(
            equity=float(portfolio.get("equity", 0.0)),
            risk_used_usd=float(portfolio.get("risk_used_usd", 0.0)),
            exposure_by_symbol=dict(exposures),
        )
