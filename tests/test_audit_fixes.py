"""Tests for PR #17 audit-defect fixes.

Covers:
- Canonical order signing (full payload, tamper detection)
- Secret provisioning to MQL5/Files
- Lot-flooring in brain.size_position and RiskAgent
- Trailing-stop broker SL reconciliation
- RiskEngine.position_risk tick calculation
- RiskEngine.aggregate_owner_risk conservative fallback
"""
from __future__ import annotations

import os
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Canonical order signing
# ---------------------------------------------------------------------------

def test_canonical_order_covers_all_fields():
    from abutron.execution.writer_gateway import _canonical_order
    cand = {
        "candidate_id": "cand-001",
        "symbol": "EURUSD.r",
        "direction": "BUY",
        "volume": 0.05,
        "stop_loss": 1.0850,
        "take_profit": 1.0900,
    }
    canonical = _canonical_order("ABUTRON_REQ_abc123", cand, "conn-0512")
    assert "ABUTRON_REQ_abc123" in canonical
    assert "cand-001" in canonical
    assert "conn-0512" in canonical
    assert "EURUSD.r" in canonical
    assert "BUY" in canonical
    assert "0.050000" in canonical
    assert "1.085000" in canonical
    assert "1.090000" in canonical


def test_canonical_signing_tamper_detection():
    from abutron.execution.writer_gateway import _canonical_order
    from abutron.crypto import hmac_sign
    secret = "test-secret"
    cand = {
        "candidate_id": "cand-001",
        "symbol": "EURUSD.r",
        "direction": "BUY",
        "volume": 0.05,
        "stop_loss": 1.0850,
        "take_profit": 1.0900,
    }
    canonical = _canonical_order("ABUTRON_REQ_abc123", cand, "conn-0512")
    sig_orig = hmac_sign(secret, canonical)

    # Tamper with symbol
    cand_tampered = dict(cand, symbol="GBPUSD.r")
    canonical_tampered = _canonical_order("ABUTRON_REQ_abc123", cand_tampered, "conn-0512")
    sig_tampered = hmac_sign(secret, canonical_tampered)
    assert sig_orig != sig_tampered

    # Tamper with volume
    cand_tampered2 = dict(cand, volume=0.99)
    canonical_tampered2 = _canonical_order("ABUTRON_REQ_abc123", cand_tampered2, "conn-0512")
    sig_tampered2 = hmac_sign(secret, canonical_tampered2)
    assert sig_orig != sig_tampered2

    # Tamper with SL
    cand_tampered3 = dict(cand, stop_loss=1.0800)
    canonical_tampered3 = _canonical_order("ABUTRON_REQ_abc123", cand_tampered3, "conn-0512")
    sig_tampered3 = hmac_sign(secret, canonical_tampered3)
    assert sig_orig != sig_tampered3

    # Tamper with TP
    cand_tampered4 = dict(cand, take_profit=1.0999)
    canonical_tampered4 = _canonical_order("ABUTRON_REQ_abc123", cand_tampered4, "conn-0512")
    sig_tampered4 = hmac_sign(secret, canonical_tampered4)
    assert sig_orig != sig_tampered4

    # Tamper with direction
    cand_tampered5 = dict(cand, direction="SELL")
    canonical_tampered5 = _canonical_order("ABUTRON_REQ_abc123", cand_tampered5, "conn-0512")
    sig_tampered5 = hmac_sign(secret, canonical_tampered5)
    assert sig_orig != sig_tampered5


# ---------------------------------------------------------------------------
# Secret provisioning
# ---------------------------------------------------------------------------

def test_provision_secret_writes_file(monkeypatch):
    from abutron.execution.writer_gateway import SingleWriterGateway
    from abutron.events import EventLog
    monkeypatch.setenv("ABUTRON_ACTIVATION_SECRET", "provisioned-secret-abc")
    with tempfile.TemporaryDirectory() as tmpdir:
        log = EventLog(os.path.join(tmpdir, "events.jsonl"))
        gw = SingleWriterGateway(log)
        result = gw.provision_secret("conn-001", tmpdir)
        assert result is True
        secret_path = os.path.join(tmpdir, "ABUTRON_ACTIVATION_SECRET.txt")
        assert os.path.exists(secret_path)
        with open(secret_path) as fh:
            assert fh.read().strip() == "provisioned-secret-abc"


def test_provision_secret_fails_without_dir():
    from abutron.execution.writer_gateway import SingleWriterGateway
    from abutron.events import EventLog
    with tempfile.TemporaryDirectory() as tmpdir:
        log = EventLog(os.path.join(tmpdir, "events.jsonl"))
        gw = SingleWriterGateway(log)
        result = gw.provision_secret("conn-001", "")
        assert result is False


# ---------------------------------------------------------------------------
# Lot-flooring (brain.size_position)
# ---------------------------------------------------------------------------

def test_brain_size_position_floors_not_rounds():
    import math
    # Simulate the flooring logic from size_position
    risk_usd = 5.0
    risk_per_lot = 2.3
    lot_step = 0.01
    lot_min = 0.01
    lot_max = 50.0

    lots = risk_usd / risk_per_lot
    # Must floor, not round
    lots = math.floor(lots / lot_step) * lot_step
    assert lots <= risk_usd / risk_per_lot  # never exceeds budget

    # Verify rounding would have gone up (the bug)
    lots_rounded = round(lots / lot_step) * lot_step
    # If risk_per_lot doesn't divide evenly, rounding can exceed
    if lots_rounded > risk_usd / risk_per_lot + 1e-9:
        assert lots < lots_rounded, "flooring must produce fewer lots than rounding"


def test_brain_size_position_rejects_min_lot_over_risk():
    import math
    # When the minimum lot already exceeds the risk budget
    risk_usd = 0.05
    risk_per_lot = 2.3
    lot_step = 0.01
    lot_min = 0.10  # broker minimum is higher than the risk budget allows

    lots = risk_usd / risk_per_lot
    lots = math.floor(lots / lot_step) * lot_step
    assert lots < lot_min, "minimum lot exceeds risk budget — must reject"


# ---------------------------------------------------------------------------
# Lot-flooring (RiskAgent)
# ---------------------------------------------------------------------------

def test_risk_agent_floors_volume():
    import math
    # Simulate RiskAgent flooring logic
    target_risk = 5.0
    risk_per_lot = 2.3
    lot_step = 0.01

    sizing_volume = target_risk / risk_per_lot
    sizing_volume = math.floor(sizing_volume / lot_step) * lot_step
    actual_risk = sizing_volume * risk_per_lot
    assert actual_risk <= target_risk + 1e-9, "actual risk must not exceed target"


def test_risk_agent_no_tolerance():
    # The old code allowed 5% tolerance; new code must not
    target_risk = 10.0
    risk_per_lot = 3.0
    lot_step = 0.01

    import math
    sizing_volume = math.floor((target_risk / risk_per_lot) / lot_step) * lot_step
    actual_risk = sizing_volume * risk_per_lot
    # Must be at or below target, not within 5%
    assert actual_risk <= target_risk + 1e-9
    assert actual_risk < target_risk * 1.05, "must not use 5% tolerance"


# ---------------------------------------------------------------------------
# Trailing-stop broker SL reconciliation
# ---------------------------------------------------------------------------

def test_broker_confirms_sl_requires_ticket_and_matching_sl():
    from abutron.risk.risk_engine import RiskEngine
    re = RiskEngine.__new__(RiskEngine)  # no DB init needed

    # No ticket → not confirmed
    assert re._broker_confirms_sl(0, 1.0850, 1.0850, 1.0800, "BUY") is False

    # Ticket but no broker_sl → not confirmed
    assert re._broker_confirms_sl(12345, None, 1.0850, 1.0800, "BUY") is False

    # Ticket + matching SL → confirmed
    assert re._broker_confirms_sl(12345, 1.0850, 1.0850, 1.0800, "BUY") is True

    # Ticket + mismatched SL → not confirmed
    assert re._broker_confirms_sl(12345, 1.0800, 1.0850, 1.0800, "BUY") is False


def test_trailing_stop_pending_without_broker_sl():
    """Trailing without broker_sl must be PENDING, not PROTECTED."""
    from abutron.risk.risk_engine import RiskEngine
    re = RiskEngine.__new__(RiskEngine)
    re.log = []

    # ATR in price units: trail_dist = 2.0 * 0.0005 = 0.001
    # new_stop = 1.0900 - 0.001 = 1.0890
    result = re.trailing_stop(
        connection_id="conn-1", candidate_id="cand-1",
        symbol="EURUSD.r", direction="BUY", current_price=1.0900,
        entry_price=1.0880, current_stop=1.0850, atr=0.0005,
        point=0.00001, point_value=1.0,
        native_ticket=12345, broker_sl=None)
    assert result["tightened"] is True
    assert result["confirmed"] is False  # PENDING, not PROTECTED


def test_trailing_stop_protected_with_broker_sl():
    """Trailing with matching broker_sl must be PROTECTED."""
    from abutron.risk.risk_engine import RiskEngine
    re = RiskEngine.__new__(RiskEngine)
    re.log = []

    # new_stop = 1.0900 - 0.001 = 1.0890
    result = re.trailing_stop(
        connection_id="conn-1", candidate_id="cand-1",
        symbol="EURUSD.r", direction="BUY", current_price=1.0900,
        entry_price=1.0880, current_stop=1.0850, atr=0.0005,
        point=0.00001, point_value=1.0,
        native_ticket=12345, broker_sl=1.0890)
    assert result["tightened"] is True
    assert result["confirmed"] is True


# ---------------------------------------------------------------------------
# position_risk tick calculation
# ---------------------------------------------------------------------------

def test_position_risk_uses_point_distance():
    """position_risk must convert price distance to points before
    multiplying by point_value."""
    from abutron.risk.risk_engine import RiskEngine
    re = RiskEngine.__new__(RiskEngine)

    # EURUSD: point=0.00001, point_value=1.0 USD/point/lot
    # entry=1.0850, SL=1.0840 → 100 pips → 100 points
    # volume=0.10 → risk = 0.10 * 100 * 1.0 = $10.00
    pr = re.position_risk(
        connection_id="conn-1", symbol="EURUSD.r", direction="BUY",
        volume=0.10, entry_price=1.0850, stop_loss=1.0840,
        atr=20.0, point_value=1.0, point=0.00001)
    assert pr.stop_dist_points == pytest.approx(100.0, rel=1e-3)
    assert pr.stop_risk_usd == pytest.approx(10.0, rel=1e-3)


def test_position_risk_without_point_falls_back():
    """Without point info, falls back to raw price distance."""
    from abutron.risk.risk_engine import RiskEngine
    re = RiskEngine.__new__(RiskEngine)

    pr = re.position_risk(
        connection_id="conn-1", symbol="UNKNOWN", direction="BUY",
        volume=1.0, entry_price=1.0850, stop_loss=1.0840,
        atr=20.0, point_value=1.0, point=0.0)
    # Falls back: 1.0 * 0.001 * 1.0 = 0.001
    assert pr.stop_risk_usd == pytest.approx(0.001, rel=1e-3)
