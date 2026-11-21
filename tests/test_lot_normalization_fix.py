"""Tests for the lot-normalization and volume-reconciliation fixes.

Reproduces the 6207 unresolved-receipt root cause:

1. The MT5 writer's ``NormalizeLots`` used ``MathFloor(v / step) * step``,
   which suffers IEEE 754 representation error: 65.57 / 0.01 evaluates
   to 6556.999999999999, flooring to 65.56 — one lot step short. The fix
   adds a 1e-9 epsilon (matching the FP native bridge pattern).

2. ``match_protective_order`` compared the broker position's volume
   against the candidate's REQUESTED volume. When the writer floored
   lots, the executed volume was one step smaller, causing
   BROKER_VOLUME_MISMATCH. The fix compares against the broker-EXECUTED
   volume carried in the receipt.

These tests reproduce the defect and pass after the fix.
"""
from __future__ import annotations

import math

import pytest

from abutron.reconciliation.native_crm_6207 import match_protective_order


# =========================================================================== #
# Part 1: MT5 NormalizeLots floating-point floor defect
# =========================================================================== #

def _mql5_normalize_lots_old(v: float, step: float = 0.01) -> float:
    """Reproduce the OLD (defective) MQL5 NormalizeLots arithmetic."""
    lots = math.floor(v / step) * step
    decimals = int(round(-math.log10(step)))
    if decimals < 0:
        decimals = 0
    if decimals > 8:
        decimals = 8
    return round(lots, decimals)


def _mql5_normalize_lots_fixed(v: float, step: float = 0.01) -> float:
    """Reproduce the FIXED MQL5 NormalizeLots with epsilon compensation."""
    lots = math.floor(v / step + 1.0e-9) * step
    decimals = int(round(-math.log10(step)))
    if decimals < 0:
        decimals = 0
    if decimals > 8:
        decimals = 8
    return round(lots, decimals)


# The five affected volumes from the 6207 investigation.
AFFECTED_VOLUMES = [65.57, 65.85, 65.35, 67.41, 67.13]


@pytest.mark.parametrize("v", AFFECTED_VOLUMES)
def test_old_normalize_lots_loses_one_step(v):
    """The OLD arithmetic floors 65.57 to 65.56 — the defect."""
    result = _mql5_normalize_lots_old(v)
    assert result == round(v - 0.01, 2), (
        f"old NormalizeLots({v}) returned {result}, expected {v - 0.01}"
        " (one lot step short — the defect)")


@pytest.mark.parametrize("v", AFFECTED_VOLUMES)
def test_fixed_normalize_lots_preserves_volume(v):
    """The FIXED arithmetic preserves the requested volume."""
    result = _mql5_normalize_lots_fixed(v)
    assert result == round(v, 2), (
        f"fixed NormalizeLots({v}) returned {result}, expected {v}")


def test_epsilon_does_not_round_up_fractionals():
    """The epsilon must not round up genuinely fractional results."""
    # 65.565 is between 65.56 and 65.57 — floor must give 65.56.
    assert _mql5_normalize_lots_fixed(65.565) == 65.56
    # 65.561 must floor to 65.56, not 65.57.
    assert _mql5_normalize_lots_fixed(65.561) == 65.56


def test_old_normalize_lots_fractionals_still_correct():
    """Sanity: the old arithmetic was fine for non-boundary values."""
    assert _mql5_normalize_lots_old(5.0) == 5.0
    assert _mql5_normalize_lots_old(10.25) == 10.25


def test_mql5_source_uses_epsilon():
    """The MQL5 source must contain the epsilon compensation."""
    src = open("mql5/ApexNativeWriter.mq5").read()
    assert "MathFloor(v / step + 1.0e-9) * step" in src, (
        "ApexNativeWriter.mq5 NormalizeLots must use epsilon compensation")


# =========================================================================== #
# Part 2: match_protective_order volume comparison fix
# =========================================================================== #

def _exposure_with_position(volume, ticket=1001, sl=99.0, tp=101.0):
    return {"positions": [{
        "ticket": ticket, "symbol": "Volatility 50 Index.0",
        "direction": "BUY", "sl": sl, "tp": tp, "volume": volume,
        "stop_risk_usd": 1.5, "ordercalcprofit_verified": True,
    }], "position_count": 1}


def _candidate(volume=65.57):
    return {
        "symbol": "Volatility 50 Index.0", "direction": "BUY",
        "volume": volume, "stop_loss": 99.0, "take_profit": 101.0,
    }


def test_volume_mismatch_without_broker_volume():
    """Legacy: comparing against requested volume → mismatch (the defect)."""
    # Broker executed 65.56 (floored), candidate requested 65.57.
    status = match_protective_order(
        {"ticket": 1001, "state": "ACK"},
        _candidate(volume=65.57),
        _exposure_with_position(volume=65.56),
        tick_size=0.1,
    )
    assert status["status"] == "BROKER_VOLUME_MISMATCH"


def test_volume_match_with_broker_volume():
    """Fix: comparing against executed volume → protected."""
    status = match_protective_order(
        {"ticket": 1001, "state": "ACK"},
        _candidate(volume=65.57),
        _exposure_with_position(volume=65.56),
        tick_size=0.1,
        broker_volume=65.56,
    )
    assert status["status"] == "BROKER_PROTECTED_SLTP"
    assert status["ticket"] == 1001


def test_broker_volume_none_preserves_legacy():
    """broker_volume=None falls back to candidate volume (legacy)."""
    status = match_protective_order(
        {"ticket": 1001, "state": "ACK"},
        _candidate(volume=65.57),
        _exposure_with_position(volume=65.57),
        tick_size=0.1,
    )
    assert status["status"] == "BROKER_PROTECTED_SLTP"


def test_broker_volume_mismatch_still_detected():
    """A genuine volume mismatch (not a floor artifact) is still caught."""
    status = match_protective_order(
        {"ticket": 1001, "state": "ACK"},
        _candidate(volume=65.57),
        _exposure_with_position(volume=50.0),
        tick_size=0.1,
        broker_volume=65.56,
    )
    assert status["status"] == "BROKER_VOLUME_MISMATCH"


def test_all_five_affected_receipts_reconciled():
    """All five 6207 volumes reconcile with broker_volume=executed."""
    for requested in AFFECTED_VOLUMES:
        executed = round(requested - 0.01, 2)
        status = match_protective_order(
            {"ticket": 1001, "state": "ACK"},
            _candidate(volume=requested),
            _exposure_with_position(volume=executed),
            tick_size=0.1,
            broker_volume=executed,
        )
        assert status["status"] == "BROKER_PROTECTED_SLTP", (
            f"requested={requested}, executed={executed}: "
            f"got {status['status']}")
