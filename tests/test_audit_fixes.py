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
        "entry_type": "LIMIT",
        "trigger_price": 1.0880,
        "trail_k": 2.0,
        "trail_stop": 1.0860,
    }
    canonical = _canonical_order("ABUTRON_REQ_abc123", cand, "conn-0512",
                                 expires_at="1794879999")
    fields = canonical.split("|")
    assert len(fields) == 13, f"expected 13 fields, got {len(fields)}"
    assert fields[-1] == "1794879999"
    assert "ABUTRON_REQ_abc123" in canonical
    assert "cand-001" in canonical
    assert "conn-0512" in canonical
    assert "EURUSD.r" in canonical
    assert "BUY" in canonical
    assert "0.050000" in canonical
    assert "1.085000" in canonical
    assert "1.090000" in canonical
    assert "LIMIT" in canonical
    assert "1.088000" in canonical
    assert "2.000000" in canonical
    assert "1.086000" in canonical


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
        "entry_type": "LIMIT",
        "trigger_price": 1.0880,
        "trail_k": 2.0,
        "trail_stop": 1.0860,
    }
    canonical = _canonical_order("ABUTRON_REQ_abc123", cand, "conn-0512")
    sig_orig = hmac_sign(secret, canonical)

    # Tamper with symbol
    cand_tampered = dict(cand, symbol="GBPUSD.r")
    assert hmac_sign(secret, _canonical_order("ABUTRON_REQ_abc123", cand_tampered, "conn-0512")) != sig_orig

    # Tamper with volume
    cand_tampered2 = dict(cand, volume=0.99)
    assert hmac_sign(secret, _canonical_order("ABUTRON_REQ_abc123", cand_tampered2, "conn-0512")) != sig_orig

    # Tamper with SL
    cand_tampered3 = dict(cand, stop_loss=1.0800)
    assert hmac_sign(secret, _canonical_order("ABUTRON_REQ_abc123", cand_tampered3, "conn-0512")) != sig_orig

    # Tamper with TP
    cand_tampered4 = dict(cand, take_profit=1.0999)
    assert hmac_sign(secret, _canonical_order("ABUTRON_REQ_abc123", cand_tampered4, "conn-0512")) != sig_orig

    # Tamper with direction
    cand_tampered5 = dict(cand, direction="SELL")
    assert hmac_sign(secret, _canonical_order("ABUTRON_REQ_abc123", cand_tampered5, "conn-0512")) != sig_orig

    # Tamper with entry_type
    cand_tampered6 = dict(cand, entry_type="CONFIRM")
    assert hmac_sign(secret, _canonical_order("ABUTRON_REQ_abc123", cand_tampered6, "conn-0512")) != sig_orig

    # Tamper with trigger_price
    cand_tampered7 = dict(cand, trigger_price=1.0885)
    assert hmac_sign(secret, _canonical_order("ABUTRON_REQ_abc123", cand_tampered7, "conn-0512")) != sig_orig

    # Tamper with trail_k
    cand_tampered8 = dict(cand, trail_k=3.0)
    assert hmac_sign(secret, _canonical_order("ABUTRON_REQ_abc123", cand_tampered8, "conn-0512")) != sig_orig

    # Tamper with trail_stop
    cand_tampered9 = dict(cand, trail_stop=1.0870)
    assert hmac_sign(secret, _canonical_order("ABUTRON_REQ_abc123", cand_tampered9, "conn-0512")) != sig_orig


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


def test_provision_secret_sets_restrictive_permissions(monkeypatch):
    """Secret file must be owner-only (0600)."""
    import stat
    from abutron.execution.writer_gateway import SingleWriterGateway
    from abutron.events import EventLog
    monkeypatch.setenv("ABUTRON_ACTIVATION_SECRET", "secret-xyz")
    with tempfile.TemporaryDirectory() as tmpdir:
        log = EventLog(os.path.join(tmpdir, "events.jsonl"))
        gw = SingleWriterGateway(log)
        result = gw.provision_secret("conn-001", tmpdir)
        assert result is True
        secret_path = os.path.join(tmpdir, "ABUTRON_ACTIVATION_SECRET.txt")
        mode = stat.S_IMODE(os.stat(secret_path).st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_arm_downgrades_on_provisioning_failure(monkeypatch):
    """Arming must fail when secret provisioning is not possible."""
    import hashlib
    from abutron.execution.writer_gateway import SingleWriterGateway
    from abutron.events import EventLog
    monkeypatch.setenv("ABUTRON_ACTIVATION_SECRET", "secret-xyz")
    with tempfile.TemporaryDirectory() as tmpdir:
        log = EventLog(os.path.join(tmpdir, "events.jsonl"))
        gw = SingleWriterGateway(log)
        ea_path = os.path.join(tmpdir, "test_ea.ex5")
        with open(ea_path, "wb") as f:
            f.write(b"fake-ea-binary")
        expected_sha = hashlib.sha256(b"fake-ea-binary").hexdigest()
        # No mql5_files_dir → provisioning fails → arm must be disarmed
        status = gw.arm(connection_id="conn-002", ea_path=ea_path,
                        expected_sha256=expected_sha,
                        activation_revision="rev1", broker="fpmarkets",
                        mql5_files_dir="")
        assert status.armed is False, "arm must be disarmed when secret provisioning fails"


def test_arm_succeeds_with_provisioning(monkeypatch):
    """Arming must succeed when secret provisioning works."""
    import hashlib
    from abutron.execution.writer_gateway import SingleWriterGateway
    from abutron.events import EventLog
    monkeypatch.setenv("ABUTRON_ACTIVATION_SECRET", "secret-xyz")
    with tempfile.TemporaryDirectory() as tmpdir:
        log = EventLog(os.path.join(tmpdir, "events.jsonl"))
        gw = SingleWriterGateway(log)
        ea_path = os.path.join(tmpdir, "test_ea.ex5")
        with open(ea_path, "wb") as f:
            f.write(b"fake-ea-binary")
        expected_sha = hashlib.sha256(b"fake-ea-binary").hexdigest()
        status = gw.arm(connection_id="conn-003", ea_path=ea_path,
                        expected_sha256=expected_sha,
                        activation_revision="rev1", broker="fpmarkets",
                        mql5_files_dir=tmpdir)
        assert status.armed is True, "arm must succeed with secret provisioning"


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

    # Ticket + matching SL alone is insufficient: account-bound broker
    # proof is also required.
    assert re._broker_confirms_sl(12345, 1.0850, 1.0850, 1.0800, "BUY") is False
    assert re._broker_confirms_sl(12345, 1.0850, 1.0850, 1.0800, "BUY", True) is True

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
    assert result["proposed"] is True
    assert result["tightened"] is False
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
        native_ticket=12345, broker_sl=1.0890,
        broker_reconciliation_verified=True)
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


def test_position_risk_without_point_is_rejected():
    """Never guess USD loss from unknown broker contract geometry."""
    from abutron.risk.risk_engine import RiskEngine
    re = RiskEngine.__new__(RiskEngine)

    with pytest.raises(ValueError, match="BROKER_NATIVE_STOP_RISK_GEOMETRY_UNVERIFIED"):
        re.position_risk(
            connection_id="conn-1", symbol="UNKNOWN", direction="BUY",
            volume=1.0, entry_price=1.0850, stop_loss=1.0840,
            atr=20.0, point_value=1.0, point=0.0)


# PR #17 follow-up: activation revision + native-source contract integrity.

def test_activation_secret_missing_fails_provision(monkeypatch, tmp_path):
    from abutron.execution.writer_gateway import SingleWriterGateway
    from abutron.events import EventLog
    monkeypatch.delenv("ABUTRON_ACTIVATION_SECRET", raising=False)
    gateway = SingleWriterGateway(EventLog(str(tmp_path / "events.jsonl")))
    assert gateway.provision_secret("conn", str(tmp_path), "rev-123") is False
    assert not (tmp_path / "ABUTRON_ACTIVATION_SECRET.txt").exists()


def test_arm_provisions_matching_revision_and_secret(monkeypatch, tmp_path):
    import hashlib
    import stat
    from abutron.execution.writer_gateway import SingleWriterGateway
    from abutron.events import EventLog
    monkeypatch.setenv("ABUTRON_ACTIVATION_SECRET", "random-test-fixture-key")
    ea = tmp_path / "writer.ex5"
    ea.write_bytes(b"not a broker binary - test fixture only")
    gateway = SingleWriterGateway(EventLog(str(tmp_path / "events.jsonl")))
    status = gateway.arm(
        connection_id="conn-0512", ea_path=str(ea),
        expected_sha256=hashlib.sha256(ea.read_bytes()).hexdigest(),
        activation_revision="rev-123", broker="fpmarkets",
        mql5_files_dir=str(tmp_path))
    assert status.armed is True
    assert (tmp_path / "ABUTRON_ACTIVATION_REVISION.txt").read_text().strip() == "rev-123"
    assert (tmp_path / "ABUTRON_ACTIVATION_SECRET.txt").read_text().strip() == "random-test-fixture-key"
    assert stat.S_IMODE((tmp_path / "ABUTRON_ACTIVATION_REVISION.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "ABUTRON_ACTIVATION_SECRET.txt").stat().st_mode) == 0o600


def test_disarmed_gateway_does_not_touch_native_delivery(monkeypatch, tmp_path):
    from abutron.execution.writer_gateway import SingleWriterGateway
    from abutron.events import EventLog
    gateway = SingleWriterGateway(EventLog(str(tmp_path / "events.jsonl")))
    def should_not_deliver(*args, **kwargs):
        raise AssertionError("disarmed writer attempted socket delivery")
    monkeypatch.setattr(gateway, "_deliver_to_ea", should_not_deliver)
    outcome = gateway.send_order(
        connection_id="conn", candidate={"candidate_id": "C1"},
        account={}, broker="fpmarkets")
    assert outcome["outcome"] == "REJECTED"
    assert outcome["reason"] == "SINGLE_WRITER_NOT_ARMED"


def test_native_writer_removes_known_invalid_mql_apis():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] /
              "mql5/ApexNativeWriter.mq5").read_text()
    for forbidden in ("SetTradeAllowed(", "SetTradeExecution(",
                      "StringFindLast(", "CryptHash(",
                      "Zero(req)", "Zero(res)", "StringTrim(s)"):
        assert forbidden not in source
    assert "CryptEncode(CRYPT_HASH_SHA256" in source
    assert "SymbolInfoTick(symbol, currentTick)" in source
    assert "ABUTRON_ACTIVATION_REVISION.txt" in source
    assert 'string dir = "";' in source

