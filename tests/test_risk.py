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
    # $50 starter tier -> 4% risk, 0.5 lot cap
    assert policy.risk_pct_for(50.0) == pytest.approx(0.04)
    assert policy.lot_max_for(50.0) == pytest.approx(0.5)
    # crossing $100 steps risk up + lot cap up (auto lot scaling)
    assert policy.risk_pct_for(100.0) == pytest.approx(0.05)
    assert policy.lot_max_for(100.0) == pytest.approx(1.0)
    # $10k tier
    assert policy.risk_pct_for(10000.0) == pytest.approx(0.12)
    assert policy.lot_max_for(10000.0) == pytest.approx(25.0)


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
    policy = RiskPolicy(daily_loss_pct=0.10)
    # $50 equity -> $5 loss cap
    assert policy.allow_entry(50.0, -4.9)[0] is True
    assert policy.allow_entry(50.0, -5.0) == (False, "DAILY_LOSS_CAP")
    assert policy.allow_entry(50.0, -6.0)[0] is False


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
