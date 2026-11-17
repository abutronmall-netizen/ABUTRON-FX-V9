"""Native sizing fail-closed regression tests (no brokers / no order paths)."""
import pytest

from abutron.risk.native_contract_sizing import (
    NativeContract, NativeSizingUnverified, lot_size_from_native_loss,
)


def fixture(**kwargs):
    args = dict(
        contract=NativeContract(
            symbol="EURUSD.r", account_currency="USD", tick_size=.00001,
            tick_value_loss=1.0, volume_min=.01, volume_max=100.,
            volume_step=.01),
        direction="BUY", entry_price=1.10000, stop_loss_price=1.09900,
        equity_usd=10000., per_trade_risk_fraction=.002,
        broker_order_calc_profit_one_lot_usd=-100.,
        owner_remaining_stop_risk_usd=2000.,
        daily_remaining_loss_budget_usd=200.,
        current_owner_risk_verified=True, native_contract_verified=True,
        native_equity_verified=True,
    )
    args.update(kwargs)
    return args


def test_floor_uses_broker_order_calc_profit_and_tick_consensus():
    r = lot_size_from_native_loss(**fixture())
    assert r.lots == .2
    assert r.stop_risk_usd == 20
    assert r.sizing_method.startswith("MT5_ORDER_CALC_PROFIT")


def test_min_lot_not_rounded_up():
    with pytest.raises(NativeSizingUnverified, match="MINIMUM_LOT"):
        lot_size_from_native_loss(**fixture(
            equity_usd=100., per_trade_risk_fraction=.002))


def test_owner_budget_cross_account_limits_volume():
    r = lot_size_from_native_loss(**fixture(
        owner_remaining_stop_risk_usd=7.50))
    assert r.lots == .07
    assert r.stop_risk_usd == 7.0


def test_daily_loss_remaining_hard_limit():
    r = lot_size_from_native_loss(**fixture(
        daily_remaining_loss_budget_usd=5.50))
    assert r.lots == .05
    assert r.stop_risk_usd == 5.0


@pytest.mark.parametrize("kwargs,reason", [
    ({"native_equity_verified":False}, "BROKER_OR_OWNER"),
    ({"current_owner_risk_verified":False}, "BROKER_OR_OWNER"),
    ({"native_contract_verified":False}, "BROKER_OR_OWNER"),
    ({"owner_remaining_stop_risk_usd":None}, "OWNER_RISK_BUDGET"),
    ({"daily_remaining_loss_budget_usd":None}, "DAILY_LOSS_BUDGET"),
    ({"broker_order_calc_profit_one_lot_usd":None}, "ORDER_CALC_PROFIT"),
    ({"broker_order_calc_profit_one_lot_usd":100}, "NOT_ADVERSE"),
    ({"contract":NativeContract("GER40","EUR",1.,1.,.01,10.,.01)}, "ACCOUNT_CURRENCY"),
    ({"direction":"BUY","stop_loss_price":1.101}, "BUY_STOP"),
    ({"direction":"SELL","stop_loss_price":1.099}, "SELL_STOP"),
    ({"contract":NativeContract("XAUUSD","USD",.01,1.,.01,100.,.01)}, "TICK_VALUE"),
    ({"per_trade_risk_fraction":.30}, "PER_TRADE_EXCEEDS"),
    ({"per_trade_risk_fraction":float("nan")}, "NONFINITE"),
    ({"equity_usd":0}, "INVALID_NATIVE"),
    ({"owner_remaining_stop_risk_usd":0}, "NO_AVAILABLE"),
    ({"contract":NativeContract("EURUSD.r","USD",0.,1.,.01,10.,.01)}, "INVALID_NATIVE"),
])
def test_rejects_unverified_or_inconsistent_risk(kwargs,reason):
    with pytest.raises(NativeSizingUnverified, match=reason):
        lot_size_from_native_loss(**fixture(**kwargs))


def test_index_contract_validated():
    r = lot_size_from_native_loss(**fixture(
        contract=NativeContract("GER40","USD",.01,.01,.01,10.,.01),
        entry_price=26500., stop_loss_price=26480.,
        broker_order_calc_profit_one_lot_usd=-20.,
    ))
    assert r.lots == 1.0
    assert r.stop_risk_usd == 20.


def test_no_fake_fx_conversion():
    with pytest.raises(NativeSizingUnverified, match="ACCOUNT_CURRENCY"):
        lot_size_from_native_loss(**fixture(
            contract=NativeContract("EURUSD","EUR",.00001,1.,.01,10.,.01)))


def test_not_rounded_up_to_disallowed_steps():
    r = lot_size_from_native_loss(**fixture(
        contract=NativeContract("EURUSD.r","USD",.00001,1.,.01,100.,.03),
        daily_remaining_loss_budget_usd=10.))
    assert r.lots == .09
    assert r.stop_risk_usd == 9.0

