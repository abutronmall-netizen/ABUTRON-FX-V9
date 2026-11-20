"""Deterministic broker-volume tests; no MT5 connections or orders."""
from dataclasses import dataclass, replace
import pytest
from abutron.execution.native_agentic_sizing import (
    NativeSizingError, plan_native_agentic_order,
)

@dataclass
class Candidate:
    symbol: str = "Volatility 50 Index.0"
    direction: str = "BUY"
    price: float = 85.7032
    stop_loss: float = 85.6175
    risk_fraction: float = 0.60142

def spec(**overrides):
    base = {
        "symbol":"Volatility 50 Index.0", "bid":85.5449, "ask":85.5449,
        "tick_age_ms":900, "tick_size":0.0001, "point":0.0001,
        "min_lots":4.0, "max_lots":3700.0, "volume_step":0.01,
        "stops_level_points":1350, "freeze_level_points":0,
        "broker_tick_value_loss":0.0001,
        "pnl_probe_displacement_ticks":100,
        "buy_probe_native_ordercalcprofit_usd":-0.04,
        "sell_probe_native_ordercalcprofit_usd":-0.04,
    }
    base.update(overrides)
    return base

def test_v50_broker_minimum_lots_and_protective_geometry():
    result = plan_native_agentic_order(Candidate(),spec(),equity_usd=9577.61)
    assert result["volume"] >= 4
    assert (result["volume"] * 100) % 1 == 0
    assert result["stop_loss"] < result["price"] < result["take_profit"]
    assert result["price"]-result["stop_loss"] >= 0.135
    assert result["estimated_stop_risk_usd"] <= result["max_per_trade_risk_usd"]
    assert result["max_per_trade_risk_usd"] < 9577.61 * .0015
    assert result["orders_submitted"] == 0
    assert result["requires_broker_ordercheck"] is True

def test_sizing_respects_owner_aggregate_open_risk():
    result = plan_native_agentic_order(
        Candidate(),spec(),equity_usd=9577.61,owner_open_risk_usd=190)
    assert result["estimated_stop_risk_usd"] <= 9577.61*.02-190

def test_broker_min_lot_exceeds_remaining_budget():
    with pytest.raises(NativeSizingError,match="MINIMUM_LOT"):
        plan_native_agentic_order(Candidate(),spec(),
                                 equity_usd=9577.61,owner_open_risk_usd=191.5)

@pytest.mark.parametrize("change", [
    {"bid":87,"ask":85},
    {"tick_age_ms":7000},
    {"min_lots":0},
    {"volume_step":0},
    {"buy_probe_native_ordercalcprofit_usd":0},
    {"broker_tick_value_loss":float("nan")},
    {"symbol":"Volatility 25 Index.0"},
])
def test_invalid_native_geometry_or_provenance_is_not_an_order(change):
    with pytest.raises(NativeSizingError):
        plan_native_agentic_order(Candidate(),spec(**change),equity_usd=9577.61)

def test_hold_has_no_entry():
    with pytest.raises(NativeSizingError,match="NO_DIRECTIONAL"):
        plan_native_agentic_order(replace(Candidate(), direction="HOLD"),
                                  spec(),equity_usd=9577.61)

def test_sell_stop_target_geometry():
    c=replace(Candidate(),direction="SELL",stop_loss=85.8)
    r=plan_native_agentic_order(c,spec(),equity_usd=9577.61)
    assert r["stop_loss"]>r["price"]>r["take_profit"]
    assert r["estimated_stop_risk_usd"]<=r["max_per_trade_risk_usd"]


def test_linked_owner_equity_is_distinct_from_trade_account_equity():
    r=plan_native_agentic_order(
        Candidate(),spec(), equity_usd=9306.74,
        owner_equity_usd=9306.74+101360.88,
        owner_open_risk_usd=316.38)
    assert r["volume"] >= 4.0
    assert r["estimated_stop_risk_usd"] <= 9306.74*.0015*.60142
