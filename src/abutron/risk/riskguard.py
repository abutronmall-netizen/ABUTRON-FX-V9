"""Hardened pre-trade risk guard (kweinmeister RiskGuard pattern, FX-adapted).

A self-contained, independently-testable rule set that validates a proposed
FX/CFD trade against configurable hard limits before it can be dispatched.
It complements (and is deliberately separate from) the portfolio `RiskEngine`
that computes stop-risk/volume: the RiskGuard is a *gate*, invoked as an
optional fail-closed pre-trade check, not a replacement for broker-native
risk authority.

Rules enforced (adapted from the reference RiskGuard):
  - structural validation (fields present, positive quantities/prices)
  - per-trade risk cap: stop_risk_usd must not exceed risk_per_trade_pct of equity
  - max position size: volume_lots must not exceed max_position_lots
  - max per-symbol concentration: post-trade symbol exposure % of equity
  - max total exposure: aggregate open exposure % of equity
  - BUY/SELL direction validity

Every rejection carries a machine-readable reason code so it can flow into
the audit event envelope and fault-code registry.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..config import (DEFAULT_PER_TRADE_RISK_PCT,
                      DEFAULT_PORTFOLIO_RISK_CAP_PCT)


# Default hard limits (FX/CFD, per-trade risk driven).
# These MUST match the config defaults — a mismatch between the guard's
# hard ceiling and the config's approved per-trade fraction would let
# trades through that the account-level policy does not authorize.
DEFAULT_MAX_POSITION_LOTS = 50.0
DEFAULT_MAX_CONCENTRATION_PCT = DEFAULT_PORTFOLIO_RISK_CAP_PCT  # 20% owner portfolio ceiling
DEFAULT_MAX_TOTAL_EXPOSURE_PCT = 1.00  # gross leverage guard, 100% of equity
DEFAULT_MAX_RISK_PER_TRADE_PCT = DEFAULT_PER_TRADE_RISK_PCT  # 0.15% hard ceiling per trade


@dataclass
class TradeProposal:
    symbol: str
    direction: str          # 'BUY' | 'SELL'
    volume_lots: float
    price: float
    stop_loss: float
    take_profit: float
    stop_risk_usd: float    # computed USD stop-risk (from RiskEngine geometry)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class PortfolioState:
    equity: float
    risk_used_usd: float = 0.0          # aggregate USD stop-risk already open
    exposure_by_symbol: Dict[str, float] = field(default_factory=dict)

    def symbol_exposure(self, symbol: str) -> float:
        return self.exposure_by_symbol.get(symbol, 0.0)

    def total_exposure(self) -> float:
        return sum(self.exposure_by_symbol.values())


@dataclass
class RiskCheckResult:
    approved: bool
    reason: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"approved": self.approved, "reason": self.reason,
                "detail": self.detail}


class RiskGuard:
    """Validate a trade proposal against hard risk limits (fail-closed)."""

    def __init__(self,
                 max_position_lots: float = DEFAULT_MAX_POSITION_LOTS,
                 max_concentration_pct: float = DEFAULT_MAX_CONCENTRATION_PCT,
                 max_total_exposure_pct: float = DEFAULT_MAX_TOTAL_EXPOSURE_PCT,
                 max_risk_per_trade_pct: float = DEFAULT_MAX_RISK_PER_TRADE_PCT):
        self.max_position_lots = max_position_lots
        self.max_concentration_pct = max_concentration_pct
        self.max_total_exposure_pct = max_total_exposure_pct
        self.max_risk_per_trade_pct = max_risk_per_trade_pct

    def check_trade(self, proposal: TradeProposal,
                    portfolio: PortfolioState) -> RiskCheckResult:
        # --- structural validation ------------------------------------ #
        if not proposal.symbol or not isinstance(proposal.symbol, str):
            return RiskCheckResult(False, "RISKGUARD_INVALID_SYMBOL")
        direction = (proposal.direction or "").upper()
        if direction not in ("BUY", "SELL"):
            return RiskCheckResult(False, "RISKGUARD_INVALID_DIRECTION")
        if not isinstance(proposal.volume_lots, (int, float)) or proposal.volume_lots <= 0:
            return RiskCheckResult(False, "RISKGUARD_INVALID_VOLUME")
        if not isinstance(proposal.price, (int, float)) or proposal.price <= 0:
            return RiskCheckResult(False, "RISKGUARD_INVALID_PRICE")
        if not isinstance(proposal.stop_risk_usd, (int, float)) or proposal.stop_risk_usd <= 0:
            return RiskCheckResult(False, "RISKGUARD_INVALID_RISK")
        if portfolio.equity <= 0:
            return RiskCheckResult(False, "RISKGUARD_INVALID_EQUITY")

        # --- per-trade risk cap --------------------------------------- #
        max_risk_usd = portfolio.equity * self.max_risk_per_trade_pct
        if proposal.stop_risk_usd > max_risk_usd:
            return RiskCheckResult(
                False, "RISKGUARD_RISK_CAP",
                {"stop_risk_usd": proposal.stop_risk_usd,
                 "max_risk_usd": round(max_risk_usd, 2),
                 "risk_pct": self.max_risk_per_trade_pct})

        # --- max position size (lots) --------------------------------- #
        if proposal.volume_lots > self.max_position_lots:
            return RiskCheckResult(
                False, "RISKGUARD_POSITION_SIZE",
                {"volume_lots": proposal.volume_lots,
                 "max_position_lots": self.max_position_lots})

        # --- per-symbol concentration (post-trade) -------------------- #
        # Post-trade symbol exposure = existing + new trade notional.
        new_notional = proposal.volume_lots * proposal.price
        post_symbol_exposure = (portfolio.symbol_exposure(proposal.symbol)
                                + new_notional)
        post_total_exposure = portfolio.total_exposure() + new_notional
        concentration = (post_symbol_exposure / portfolio.equity
                         if portfolio.equity > 0 else 1.0)
        if concentration > self.max_concentration_pct:
            return RiskCheckResult(
                False, "RISKGUARD_CONCENTRATION",
                {"symbol": proposal.symbol,
                 "post_exposure_usd": round(post_symbol_exposure, 2),
                 "equity": round(portfolio.equity, 2),
                 "concentration_pct": round(concentration, 4),
                 "max_concentration_pct": self.max_concentration_pct})

        # --- max total (gross) exposure ------------------------------- #
        total_exposure_pct = (post_total_exposure / portfolio.equity
                              if portfolio.equity > 0 else 1.0)
        if total_exposure_pct > self.max_total_exposure_pct:
            return RiskCheckResult(
                False, "RISKGUARD_TOTAL_EXPOSURE",
                {"post_total_exposure_usd": round(post_total_exposure, 2),
                 "equity": round(portfolio.equity, 2),
                 "exposure_pct": round(total_exposure_pct, 4),
                 "max_total_exposure_pct": self.max_total_exposure_pct})

        return RiskCheckResult(True, "RISKGUARD_APPROVED",
                               {"volume_lots": proposal.volume_lots,
                                "stop_risk_usd": proposal.stop_risk_usd,
                                "concentration_pct": round(concentration, 4)})


risk_guard = RiskGuard()
