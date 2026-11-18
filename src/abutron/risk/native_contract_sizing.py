"""ABUTRON broker-native position sizing. Safe to use for DEMO/LIVE preflight.

Requires current, same-account MT5 OrderCalcProfit loss at ONE LOT,
broker tick size/value, USD account equity, an independently reconciled
owner-wide remaining stop-risk budget, and a native daily-loss budget.
Never substitutes point, contract or FX-conversion guesses.
No order-sending code.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, InvalidOperation
from typing import Any


class NativeSizingUnverified(ValueError):
    """Broker-calculated stop-risk or owner proof missing / inconsistent."""


def _d(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise NativeSizingUnverified(label + "_MISSING")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise NativeSizingUnverified(label + "_INVALID") from None
    if not number.is_finite():
        raise NativeSizingUnverified(label + "_NONFINITE")
    return number


@dataclass(frozen=True)
class NativeContract:
    symbol: str
    account_currency: str
    tick_size: float
    tick_value_loss: float
    volume_min: float
    volume_max: float
    volume_step: float


@dataclass(frozen=True)
class NativeSizeResult:
    symbol: str
    lots: float
    stop_risk_usd: float
    per_trade_budget_usd: float
    owner_remaining_usd: float
    daily_remaining_usd: float
    sizing_method: str
    broker_native_proof_required: bool = True


def lot_size_from_native_loss(
    *, contract: NativeContract,
    direction: str,
    entry_price: float,
    stop_loss_price: float,
    equity_usd: float,
    per_trade_risk_fraction: float,
    broker_order_calc_profit_one_lot_usd: float,
    owner_remaining_stop_risk_usd: float,
    daily_remaining_loss_budget_usd: float,
    current_owner_risk_verified: bool,
    native_contract_verified: bool,
    native_equity_verified: bool,
    owner_ceiling_fraction: float = 0.20,
) -> NativeSizeResult:
    """Size down to the MT5 lot step; never round up to minimum volume.

    A negative MT5 OrderCalcProfit(one lot, entry -> SL) is the required
    authoritative loss for the SAME broker symbol, account and direction.
    The tick-value estimate must agree within 5% (or 0.10 USD absolute).
    Never imply that estimated risk *alone* proves broker acceptance.
    """
    if not (current_owner_risk_verified and native_contract_verified
            and native_equity_verified):
        raise NativeSizingUnverified("BROKER_OR_OWNER_EXPOSURE_UNVERIFIED")
    if contract.account_currency != "USD":
        raise NativeSizingUnverified("ACCOUNT_CURRENCY_CONVERSION_UNVERIFIED")
    if not isinstance(contract.symbol, str) or not contract.symbol:
        raise NativeSizingUnverified("SYMBOL_MISSING")
    if direction not in ("BUY", "SELL"):
        raise NativeSizingUnverified("DIRECTION_INVALID")
    entry = _d(entry_price, "ENTRY")
    stop = _d(stop_loss_price, "STOP")
    equity = _d(equity_usd, "EQUITY")
    per_trade = _d(per_trade_risk_fraction, "PER_TRADE_RISK")
    ceiling = _d(owner_ceiling_fraction, "OWNER_CEILING")
    owner_room = _d(owner_remaining_stop_risk_usd, "OWNER_RISK_BUDGET")
    daily_room = _d(daily_remaining_loss_budget_usd, "DAILY_LOSS_BUDGET")
    profit_one = _d(broker_order_calc_profit_one_lot_usd, "ORDER_CALC_PROFIT")
    tick_size = _d(contract.tick_size, "TICK_SIZE")
    tick_loss = _d(contract.tick_value_loss, "TICK_VALUE_LOSS")
    lo = _d(contract.volume_min, "MIN_LOT")
    hi = _d(contract.volume_max, "MAX_LOT")
    step = _d(contract.volume_step, "LOT_STEP")
    if (entry <= 0 or stop <= 0 or equity <= 0 or
        not (0 < per_trade <= 1) or not (0 < ceiling <= 1)
        or owner_room < 0 or daily_room < 0
        or tick_size <= 0 or tick_loss <= 0
        or step <= 0 or lo <= 0 or hi < lo):
        raise NativeSizingUnverified("INVALID_NATIVE_RISK_PARAMETERS")
    if direction == "BUY" and not stop < entry:
        raise NativeSizingUnverified("BUY_STOP_NOT_ADVERSE")
    if direction == "SELL" and not stop > entry:
        raise NativeSizingUnverified("SELL_STOP_NOT_ADVERSE")
    if profit_one >= 0:
        raise NativeSizingUnverified("ORDER_CALC_PROFIT_NOT_ADVERSE")
    per_lot_loss = -profit_one
    if per_lot_loss <= 0:
        raise NativeSizingUnverified("NATIVE_STOP_RISK_INVALID")
    point_estimate = abs(entry-stop) / tick_size * tick_loss
    if abs(point_estimate - per_lot_loss) > max(Decimal("0.10"), per_lot_loss * Decimal("0.05")):
        raise NativeSizingUnverified("BROKER_TICK_VALUE_ORDER_CALC_PROFIT_MISMATCH")
    # Owner risk budget must already be calculated across *all* linked
    # accounts using authenticated broker position/equity records.
    # The 20% ceiling must not be inferred from this one account's equity.
    if per_trade > ceiling:
        raise NativeSizingUnverified("PER_TRADE_EXCEEDS_OWNER_CEILING")
    budget = min(equity * per_trade, owner_room, daily_room)
    if budget <= 0:
        raise NativeSizingUnverified("NO_AVAILABLE_RISK_BUDGET")
    max_lots_under_risk = min(hi, budget/per_lot_loss)
    # MT5 volumes are integer steps from zero, not min_lot plus steps;
    # ensure the resulting multiple also meets the broker minimum.
    steps = (max_lots_under_risk / step).to_integral_value(rounding=ROUND_FLOOR)
    volume = steps * step
    if volume < lo:
        raise NativeSizingUnverified("MINIMUM_LOT_EXCEEDS_RISK_BUDGET")
    expected_risk = volume * per_lot_loss
    if expected_risk > budget or volume > hi:
        raise NativeSizingUnverified("SIZING_BUDGET_VIOLATION")
    return NativeSizeResult(
        symbol=contract.symbol,
        lots=float(volume),
        stop_risk_usd=float(expected_risk),
        per_trade_budget_usd=float(equity * per_trade),
        owner_remaining_usd=float(owner_room),
        daily_remaining_usd=float(daily_room),
        sizing_method="MT5_ORDER_CALC_PROFIT_AND_TICK_VALUE_VERIFIED_FLOOR",
    )

