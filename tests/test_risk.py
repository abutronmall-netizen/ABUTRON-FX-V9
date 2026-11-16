"""Tests for the equity-tiered compounding + daily-target risk policy."""
from __future__ import annotations

import pytest

from abutron.brain.risk import (
    DEFAULT_TIERS, EquityTier, RiskPolicy,
    equity_for_daily_target, tier_for_equity,
)


def test_tier_for_equity_picks_correct_milestone():
    assert tier_for_equity(50.0).label == "$50 starter"
    assert tier_for_equity(99.99).label == "$50 starter"
    assert tier_for_equity(100.0).label == "$100"
    assert tier_for_equity(1000.0).label == "$1k"
    assert tier_for_equity(99999.0).label == "$25k+"


def test_risk_pct_and_lot_scale_with_equity():
    policy = RiskPolicy()
    # Extreme-low-drawdown cap: per-trade risk is capped at dd_target /
    # max_consec_losses = 0.05 / 5 = 1% across every tier (the 15% tier
    # risk is only an upper bound). lot_max still scales with equity.
    assert policy.dd_risk_pct() == pytest.approx(0.01)
    assert policy.risk_pct_for(50.0) == pytest.approx(0.01)
    assert policy.lot_max_for(50.0) == pytest.approx(0.5)
    assert policy.risk_pct_for(100.0) == pytest.approx(0.01)
    assert policy.lot_max_for(100.0) == pytest.approx(1.0)
    assert policy.risk_pct_for(10000.0) == pytest.approx(0.01)
    assert policy.lot_max_for(10000.0) == pytest.approx(25.0)


def test_dd_risk_scale_tapers_with_drawdown():
    policy = RiskPolicy()  # dd_target=5%, max_consec_losses=5
    # no drawdown -> full risk
    assert policy.dd_risk_scale(100.0, 100.0) == pytest.approx(1.0)
    # 2.5% drawdown (half the target) -> scale 0.5
    assert policy.dd_risk_scale(97.5, 100.0) == pytest.approx(0.5)
    # at the target -> floor 0.2 (risk scaled, never hard-stopped)
    assert policy.dd_risk_scale(95.0, 100.0) == pytest.approx(0.2)
    # deep drawdown -> still floors at 0.2 (no fail-closed stop)
    assert policy.dd_risk_scale(80.0, 100.0) == pytest.approx(0.2)
    # runtime risk combines cap + taper
    assert policy.risk_pct_for_runtime(97.5, 100.0) == pytest.approx(0.005)


def test_daily_stops_off_by_default():
    policy = RiskPolicy()  # no target / no loss cap configured
    assert policy.allow_entry(50.0, 0.0) == (True, "OK")
    # even a huge daily loss does not stop when loss cap is None
    assert policy.allow_entry(50.0, -100000.0)[0] is True


def test_daily_profit_stop():
    policy = RiskPolicy(target_pnl=1000.0)
    assert policy.allow_entry(50.0, 999.0)[0] is True
    assert policy.allow_entry(50.0, 1000.0) == (False, "DAILY_TARGET_REACHED")
    assert policy.allow_entry(50.0, 5000.0)[0] is False


def test_daily_loss_cap():
    policy = RiskPolicy(daily_loss_r=3.0)
    # $50 equity @ DD-capped 1% risk -> $0.50/trade; 3R cap = $1.50/day
    assert policy.allow_entry(50.0, -1.4)[0] is True
    assert policy.allow_entry(50.0, -1.5) == (False, "DAILY_LOSS_CAP")
    assert policy.allow_entry(50.0, -2.0)[0] is False


def test_equity_for_daily_target_reachable_equity():
    # $1000/day is NOT reachable at $50 (needs far more equity)
    need = equity_for_daily_target(1000.0)
    assert need > 50.0
    # it should be reachable somewhere in the mid ladder
    assert need < 25000.0
    # at the computed equity, realistic_daily_pnl should be ~target
    policy = RiskPolicy()
    est = policy.realistic_daily_pnl(need)
    assert est == pytest.approx(1000.0, rel=0.05)


def test_realistic_daily_pnl_grows_with_equity():
    policy = RiskPolicy()
    p50 = policy.realistic_daily_pnl(50.0)
    p100 = policy.realistic_daily_pnl(100.0)
    p10k = policy.realistic_daily_pnl(10000.0)
    assert p50 > 0
    assert p100 > p50
    assert p10k > p100
    # $1000/day is far beyond the $50 daily expectancy
    assert p50 < 1000.0
