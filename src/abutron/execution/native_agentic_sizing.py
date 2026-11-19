"""Convert a trained V9 decision into a broker-sized DEMO order proposal.

No socket/file order submission. Broker OrderCalcProfit and OrderCheck on
the *exact* price, volume and protective stop remain MT5-side operations.
"""
from __future__ import annotations
from decimal import Decimal as D, ROUND_FLOOR, ROUND_CEILING
from typing import Any, Mapping

class NativeSizingError(ValueError):
    pass

def positive(value: Any, label: str) -> D:
    try:
        d = D(str(value))
    except Exception as exc:
        raise NativeSizingError(label + "_INVALID") from exc
    if not d.is_finite() or d <= 0:
        raise NativeSizingError(label + "_INVALID")
    return d

def quantize_to(value: D, tick: D, mode: str) -> D:
    return (value/tick).to_integral_value(rounding=mode)*tick

def plan_native_agentic_order(
    candidate: Any, spec: Mapping[str, Any], *,
    equity_usd: float, owner_open_risk_usd: float = 0.0,
    owner_equity_usd: float | None = None,
    per_trade_risk_pct: float = 0.0015,
    aggregate_open_risk_pct: float = 0.02,
) -> dict:
    """Use genuine native broker minimum lots, quote and OrderCalcProfit probe.

    The model's position_size is a bounded fraction of its permitted
    risk budget; it is never interpreted as physical MT5 lots.
    """
    symbol = str(candidate.symbol)
    direction = str(candidate.direction)
    if direction not in ("BUY", "SELL"):
        raise NativeSizingError("NO_DIRECTIONAL_AGENTIC_ENTRY")
    if spec.get("symbol", symbol) != symbol:
        raise NativeSizingError("NATIVE_BROKER_SYMBOL_MISMATCH")
    bid, ask = positive(spec["bid"], "BID"), positive(spec["ask"], "ASK")
    if bid > ask:
        raise NativeSizingError("BROKER_QUOTE_CROSSED")
    if int(spec.get("tick_age_ms", 999999)) > 3000:
        raise NativeSizingError("BROKER_QUOTE_NOT_CURRENT")
    tick, point = positive(spec["tick_size"], "TICK"), positive(spec["point"], "POINT")
    lot_min = positive(spec["min_lots"], "MIN_LOTS")
    lot_max = positive(spec["max_lots"], "MAX_LOTS")
    step = positive(spec["volume_step"], "LOT_STEP")
    if lot_max < lot_min or lot_min % step != 0:
        raise NativeSizingError("BROKER_LOT_GEOMETRY_INVALID")
    entry = ask if direction == "BUY" else bid
    ai_price = positive(candidate.price, "AI_PRICE")
    ai_stop = positive(candidate.stop_loss, "AI_STOP")
    if direction == "BUY" and ai_stop >= ai_price:
        raise NativeSizingError("AI_STOP_WRONG_SIDE")
    if direction == "SELL" and ai_stop <= ai_price:
        raise NativeSizingError("AI_STOP_WRONG_SIDE")
    native_floor = D(max(int(spec["stops_level_points"]),
                         int(spec["freeze_level_points"]))) * point
    # Broker stop is measured from close-side bid/ask, including spread.
    dist = max(abs(ai_price - ai_stop), native_floor + (ask-bid) + 2*tick)
    dist = quantize_to(dist, tick, ROUND_CEILING)
    if direction == "BUY":
        stop = quantize_to(entry-dist, tick, ROUND_FLOOR)
        target = quantize_to(entry+2*dist, tick, ROUND_CEILING)
    else:
        stop = quantize_to(entry+dist, tick, ROUND_CEILING)
        target = quantize_to(entry-2*dist, tick, ROUND_FLOOR)
    if stop <= 0 or target <= 0:
        raise NativeSizingError("BROKER_PROTECTIVE_PRICE_INVALID")
    ticks = abs(entry-stop)/tick
    probe_ticks = positive(spec["pnl_probe_displacement_ticks"], "PROBE_TICKS")
    probe_key = ("buy_probe_native_ordercalcprofit_usd" if direction == "BUY"
                 else "sell_probe_native_ordercalcprofit_usd")
    observed_native_probe = abs(D(str(spec[probe_key])))
    if not observed_native_probe.is_finite() or observed_native_probe <= 0:
        raise NativeSizingError("BROKER_ORDER_CALC_PROFIT_PROBE_MISSING")
    per_lot_per_tick = max(
        positive(spec["broker_tick_value_loss"], "BROKER_TICK_LOSS"),
        observed_native_probe / (probe_ticks * lot_min))
    loss_one_lot = per_lot_per_tick * ticks
    eq = positive(equity_usd, "EQUITY")
    risk_share = min(D("1"), positive(candidate.risk_fraction, "AI_RISK_SHARE"))
    used = D(str(owner_open_risk_usd))
    if not used.is_finite() or used < 0:
        raise NativeSizingError("OWNER_OPEN_RISK_INVALID")
    trade_cap = eq * positive(per_trade_risk_pct, "TRADE_RISK_PCT") * risk_share
    owner_eq = positive(owner_equity_usd if owner_equity_usd is not None else
                        equity_usd, "OWNER_EQUITY")
    open_remaining = owner_eq * positive(aggregate_open_risk_pct,
                                         "OPEN_RISK_PCT") - used
    permitted_loss = min(trade_cap, open_remaining)
    if permitted_loss < lot_min * loss_one_lot:
        raise NativeSizingError("BROKER_MINIMUM_LOT_EXCEEDS_ACCOUNT_RISK")
    lots = min(lot_max, quantize_to(permitted_loss/loss_one_lot,step,ROUND_FLOOR))
    if lots < lot_min:
        raise NativeSizingError("NO_NATIVE_VOLUME_WITHIN_OWNER_LIMIT")
    projected_risk = lots * loss_one_lot
    if projected_risk > permitted_loss:
        raise NativeSizingError("VOLUME_ROUNDING_EXCEEDS_RISK_BUDGET")
    return {
        "symbol":symbol, "direction":direction,
        "volume":float(lots), "price":float(entry),
        "stop_loss":float(stop), "take_profit":float(target),
        "estimated_stop_risk_usd":float(projected_risk),
        "max_per_trade_risk_usd":float(trade_cap),
        "owner_open_risk_usd":float(used),
        "source":"broker_native_quote_and_ordercalcprofit_probe",
        "requires_exact_native_ordercalcprofit":True,
        "requires_broker_ordercheck":True,
        "orders_submitted":0,
    }
