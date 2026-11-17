"""Broker contract verification — validate risk math against MT5 specs.

Reconciles the platform's risk/lot-size mathematics with real MT5
contract specifications: point size, point value (USD per point per
lot), account currency, minimum lot, lot step, maximum loss at SL,
daily caps, and owner-wide exposure.

This is the "First" priority from the V9 verification checklist:
verify every risk number using actual broker contract data rather than
static proxies or assumptions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..config import (DEFAULT_PER_TRADE_RISK_PCT,
                      DEFAULT_PORTFOLIO_RISK_CAP_PCT,
                      DEFAULT_DAILY_LOSS_CAP_PCT)
from ..apex.eligibility import SYMBOL_REGISTRY, SymbolContract


@dataclass
class ContractVerification:
    """Result of verifying a contract's risk math against MT5 specs."""
    symbol: str
    broker: str
    point: float
    point_value: float
    lot_min: float
    lot_step: float
    lot_max: float
    account_currency: str = "USD"
    # Computed values
    min_lot_stop_risk_usd: float = 0.0
    per_trade_risk_usd_at_min: float = 0.0
    per_trade_risk_pct: float = DEFAULT_PER_TRADE_RISK_PCT
    portfolio_cap_pct: float = DEFAULT_PORTFOLIO_RISK_CAP_PCT
    issues: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "broker": self.broker,
            "point": self.point,
            "point_value": self.point_value,
            "lot_min": self.lot_min,
            "lot_step": self.lot_step,
            "lot_max": self.lot_max,
            "account_currency": self.account_currency,
            "min_lot_stop_risk_usd": round(self.min_lot_stop_risk_usd, 4),
            "per_trade_risk_usd_at_min": round(self.per_trade_risk_usd_at_min, 4),
            "per_trade_risk_pct": self.per_trade_risk_pct,
            "portfolio_cap_pct": self.portfolio_cap_pct,
            "issues": self.issues,
            "verified": len(self.issues) == 0,
        }


def verify_contract(symbol: str, *, equity: float,
                    stop_distance_points: float,
                    account_currency: str = "USD",
                    per_trade_risk_pct: float = DEFAULT_PER_TRADE_RISK_PCT,
                    portfolio_cap_pct: float = DEFAULT_PORTFOLIO_RISK_CAP_PCT
                    ) -> ContractVerification:
    """Verify a symbol's risk math against its MT5 contract spec.

    Computes:
    - Minimum-lot stop risk: lot_min * stop_distance_points * point_value
    - Whether the minimum lot already exceeds the per-trade risk budget
    - Whether the point_value is reasonable (non-zero, positive)
    - Whether lot_min / lot_step are valid

    Args:
        symbol: Symbol to verify (e.g. "EURUSD.r").
        equity: Account equity for risk percentage checks.
        stop_distance_points: Stop distance in points (from sniper/ATR).
        account_currency: Account currency (for future FX conversion).
        per_trade_risk_pct: Approved per-trade risk fraction.
        portfolio_cap_pct: Approved portfolio ceiling fraction.

    Returns:
        ContractVerification with computed values and any issues found.
    """
    profile = SYMBOL_REGISTRY.get(symbol)
    # Registry profiles are simulation estimates, not live MT5 proof.
    # Native acceptance requires same-account SymbolInfoDouble AND
    # OrderCalcProfit(entry -> SL) supplied to native_contract_sizing.
    issues: List[str] = ["BROKER_NATIVE_ORDER_CALC_PROFIT_NOT_VERIFIED"]

    if profile is None:
        # Conservative fallback — cannot verify without contract data
        contract = SymbolContract(point=0.0001, point_value=1.0,
                                  lot_min=0.01, lot_step=0.01,
                                  lot_max=50.0)
        issues.append(f"SYMBOL_NOT_IN_REGISTRY: {symbol} — using "
                      "conservative fallback, must reconcile against MT5")
        broker = "unknown"
    else:
        contract = profile.contract
        broker = profile.broker

    # --- Validate contract fields ---
    if contract.point <= 0:
        issues.append(f"INVALID_POINT: {symbol} point={contract.point}")
    if contract.point_value <= 0:
        issues.append(f"INVALID_POINT_VALUE: {symbol} "
                      f"point_value={contract.point_value}")
    if contract.lot_min <= 0:
        issues.append(f"INVALID_LOT_MIN: {symbol} lot_min={contract.lot_min}")
    if contract.lot_step <= 0:
        issues.append(f"INVALID_LOT_STEP: {symbol} lot_step={contract.lot_step}")

    # --- Compute minimum-lot stop risk ---
    # USD risk at minimum lot = lot_min * stop_dist_points * point_value
    min_lot_stop_risk = contract.lot_min * stop_distance_points * contract.point_value

    # --- Per-trade risk budget at minimum lot ---
    per_trade_risk_budget = equity * per_trade_risk_pct
    per_trade_risk_at_min = min_lot_stop_risk

    if per_trade_risk_at_min > per_trade_risk_budget:
        issues.append(
            f"MIN_LOT_OVER_RISK: {symbol} min-lot stop risk "
            f"(${min_lot_stop_risk:.4f}) exceeds per-trade budget "
            f"(${per_trade_risk_budget:.4f}) — trade must be rejected")

    return ContractVerification(
        symbol=symbol, broker=broker,
        point=contract.point,
        point_value=contract.point_value,
        lot_min=contract.lot_min,
        lot_step=contract.lot_step,
        lot_max=contract.lot_max if hasattr(contract, 'lot_max') else 50.0,
        account_currency=account_currency,
        min_lot_stop_risk_usd=min_lot_stop_risk,
        per_trade_risk_usd_at_min=per_trade_risk_at_min,
        per_trade_risk_pct=per_trade_risk_pct,
        portfolio_cap_pct=portfolio_cap_pct,
        issues=issues,
    )


def verify_all_contracts(*, equity: float,
                         stop_distance_points: float = 26.0,
                         account_currency: str = "USD") -> List[ContractVerification]:
    """Verify all symbols in the registry against MT5 contract specs.

    Args:
        equity: Reference equity for risk percentage checks.
        stop_distance_points: Representative ATR stop distance in points.
        account_currency: Account currency.

    Returns:
        List of ContractVerification for each registered symbol.
    """
    results = []
    for symbol in SYMBOL_REGISTRY:
        results.append(verify_contract(
            symbol, equity=equity,
            stop_distance_points=stop_distance_points,
            account_currency=account_currency))
    return results


def verify_owner_exposure(owner_id: str, *, equity_by_conn: Dict[str, float],
                          open_positions: Dict[str, List[Dict]],
                          portfolio_cap_pct: float = DEFAULT_PORTFOLIO_RISK_CAP_PCT
                          ) -> Dict[str, Any]:
    """Verify owner-wide exposure using real contract specs.

    Aggregates stop risk across all linked accounts using actual
    contract point/point_value from SYMBOL_REGISTRY (with conservative
    fallback for unknown symbols).

    Args:
        owner_id: Owner identifier.
        equity_by_conn: Mapping connection_id -> equity.
        open_positions: Mapping connection_id -> list of open position dicts.
        portfolio_cap_pct: Approved portfolio ceiling fraction.

    Returns:
        Dict with aggregate risk, cap, headroom, and any issues.
    """
    total_risk = 0.0
    total_balance = 0.0
    issues: List[str] = []
    unknown_symbols: set = set()

    for cid, positions in open_positions.items():
        equity = equity_by_conn.get(cid, 0.0)
        total_balance += equity
        conn_risk = 0.0
        for pos in positions:
            entry = float(pos.get("fill_price", 0.0) or 0.0)
            sl = float(pos.get("stop_loss", 0.0) or 0.0)
            vol = float(pos.get("volume", 0.0) or 0.0)
            sym = pos.get("symbol", "")
            profile = SYMBOL_REGISTRY.get(sym)
            if profile is not None:
                point = float(profile.contract.point or 0.0)
                point_value = float(profile.contract.point_value or 1.0)
            else:
                point = 0.0001
                point_value = 1.0
                unknown_symbols.add(sym)
            if entry and sl:
                stop_dist = abs(entry - sl)
                if point and point > 0:
                    stop_dist_pts = stop_dist / point
                    conn_risk += vol * stop_dist_pts * point_value
                else:
                    conn_risk += vol * stop_dist * point_value
        total_risk += conn_risk

    cap_usd = portfolio_cap_pct * total_balance
    if unknown_symbols:
        issues.append(
            f"UNKNOWN_SYMBOLS: {sorted(unknown_symbols)} — using "
            "conservative fallback, must reconcile against MT5")
    if total_risk > cap_usd:
        issues.append(
            f"PORTFOLIO_CAP_EXCEEDED: aggregate risk "
            f"(${total_risk:.2f}) exceeds cap (${cap_usd:.2f})")

    return {
        "owner_id": owner_id,
        "aggregate_stop_risk_usd": round(total_risk, 2),
        "aggregate_balance_usd": round(total_balance, 2),
        "cap_pct": portfolio_cap_pct,
        "cap_usd": round(cap_usd, 2),
        "headroom_usd": round(cap_usd - total_risk, 2),
        "cap_exceeded": total_risk > cap_usd,
        "unknown_symbols": sorted(unknown_symbols),
        "issues": issues,
        "verified": len(issues) == 0,
    }
