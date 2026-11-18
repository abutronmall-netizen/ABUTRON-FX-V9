"""Regression tests for broker-native exposure authentication and MQL5 source contract.

These tests use synthetic snapshots and do NOT connect to MT5 or submit orders.
"""
import copy
import time
from pathlib import Path

import pytest

from abutron.crm.controls import CrmError, CrmService
from abutron.crypto import (
    activation_secret, build_exposure_canonical, hmac_sign,
)

TEST_KEY = "ABUTRON_TEST_ONLY_DO_NOT_USE_ON_REAL_TERMINALS_2026"


class MemoryEngine:
    def __init__(self):
        self.account = {
            "connection_id": "synthetic-cid", "mt5_login": 123456,
            "server": "Synthetic-Demo", "native_positions_verified": 0,
            "equity": 0.0, "exposure_certified_at": "",
        }
        self.receipts = []

    def get_account(self, cid):
        return dict(self.account) if cid == self.account["connection_id"] else None

    def upsert_account(self, account):
        self.account.update(account)

    def recent_open_positions(self, cid):
        return list(self.receipts)

    def update_receipt(self, receipt_id, **updates):
        for receipt in self.receipts:
            if receipt["receipt_id"] == receipt_id:
                receipt.update(updates)


def snapshot_with_signature(positions=None):
    positions = [] if positions is None else positions
    nonce = f"ABUTRON_NATIVE_123456_{int(time.time())}"
    snap = {
        "connection_id": "synthetic-cid", "login": "123456",
        "server": "Synthetic-Demo", "currency": "USD",
        "equity": 10000.00, "balance": 10000.00,
        "nonce": nonce, "challenge": "synthetic-challenge",
        "position_count": len(positions), "positions": copy.deepcopy(positions),
    }
    snap["sig"] = hmac_sign(
        TEST_KEY,
        build_exposure_canonical(
            snap["connection_id"], snap["login"], snap["equity"],
            snap["nonce"], snap["positions"],
            server=snap["server"], currency=snap["currency"],
            balance=snap["balance"], challenge=snap["challenge"],
        ),
    )
    return snap


@pytest.fixture
def crm_memory(monkeypatch):
    monkeypatch.setenv("ABUTRON_ACTIVATION_SECRET", TEST_KEY)
    service = CrmService()
    db = MemoryEngine()
    service.engine = db
    return service, db


def test_missing_secret_never_uses_public_default(monkeypatch, crm_memory):
    service, db = crm_memory
    snap = snapshot_with_signature()
    monkeypatch.delenv("ABUTRON_ACTIVATION_SECRET", raising=False)
    with pytest.raises(ValueError, match="must be provisioned"):
        activation_secret()
    with pytest.raises(CrmError, match="authentication not configured"):
        service.certify_exposure("synthetic-cid", snapshot=snap)
    assert db.account["native_positions_verified"] == 0


def test_placeholder_secret_rejected(monkeypatch, crm_memory):
    service, db = crm_memory
    snap = snapshot_with_signature()
    monkeypatch.setenv("ABUTRON_ACTIVATION_SECRET", "apex-activation")
    with pytest.raises(ValueError):
        activation_secret()
    with pytest.raises(CrmError):
        service.certify_exposure("synthetic-cid", snapshot=snap)
    assert db.account["native_positions_verified"] == 0


def test_valid_synthetic_flat_certificate_requires_explicit_secret(crm_memory):
    service, db = crm_memory
    certified = service.certify_exposure("synthetic-cid", snapshot=snapshot_with_signature())
    assert certified["native_positions_verified"] == 1
    assert certified["exposure_snapshot_count"] == 0


@pytest.mark.parametrize("field,altered", [
    ("entry", 1.99), ("sl", 0.95), ("tp", 1.80),
    ("stop_risk_usd", 99999.00), ("ordercalcprofit_verified", False),
])
def test_money_relevant_tampering_breaks_hmac(crm_memory, field, altered):
    service, db = crm_memory
    pos = {
        "ticket": 123, "symbol": "EURUSD", "direction": "BUY",
        "volume": 0.10, "entry": 1.10, "sl": 1.09, "tp": 1.20,
        "stop_risk_usd": 100.00, "ordercalcprofit_verified": True,
    }
    db.receipts = [{
        "receipt_id": "synthetic-r1", "native_ticket": 123,
        "connection_id": "synthetic-cid", "execution_state": "OPEN",
    }]
    snap = snapshot_with_signature([pos])
    snap["positions"][0][field] = altered  # signature must no longer verify
    with pytest.raises(CrmError, match="invalid HMAC"):
        service.certify_exposure("synthetic-cid", snapshot=snap)
    assert db.account["native_positions_verified"] == 0


@pytest.mark.parametrize("field,altered", [
    ("balance", 1.00), ("currency", "ZAR"),
    ("server", "Other-Demo"), ("challenge", "replaced"),
])
def test_account_and_balance_tampering_breaks_proof(crm_memory, field, altered):
    service, db = crm_memory
    snap = snapshot_with_signature()
    snap[field] = altered
    with pytest.raises(CrmError):
        service.certify_exposure("synthetic-cid", snapshot=snap)
    assert db.account["native_positions_verified"] == 0


def test_replay_does_not_extend_certificate(crm_memory):
    service, db = crm_memory
    snap = snapshot_with_signature()
    service.certify_exposure("synthetic-cid", snapshot=snap)
    first_ts = db.account["exposure_certified_at"]
    with pytest.raises(CrmError, match="replay"):
        service.certify_exposure("synthetic-cid", snapshot=snap)
    assert db.account["exposure_certified_at"] == first_ts


def test_worker_invalidation_clears_old_proof(crm_memory):
    service, db = crm_memory
    service.certify_exposure("synthetic-cid", snapshot=snapshot_with_signature())
    service.invalidate_exposure("synthetic-cid")
    assert db.account["native_positions_verified"] == 0
    assert db.account["exposure_certified_at"] == ""


def test_future_nonce_rejected(crm_memory):
    service, db = crm_memory
    snap = snapshot_with_signature()
    snap["nonce"] = f"ABUTRON_NATIVE_123456_{int(time.time())+600}"
    snap["sig"] = hmac_sign(
        TEST_KEY, build_exposure_canonical(
            snap["connection_id"], snap["login"], snap["equity"],
            snap["nonce"], snap["positions"],
            server=snap["server"], currency=snap["currency"],
            balance=snap["balance"], challenge=snap["challenge"],
        ),
    )
    with pytest.raises(CrmError, match="stale"):
        service.certify_exposure("synthetic-cid", snapshot=snap)
    assert db.account["native_positions_verified"] == 0


def test_mql5_uses_documented_profit_api_and_matching_signature_fields():
    source = (Path(__file__).resolve().parents[1] /
              "mql5" / "ApexNativeWriter.mq5").read_text()
    assert "OrderCalcProfit(ordType, sym, vol, entry, sl," in source
    assert "calcProfit)" in source
    assert "OrderCalcProfit(ticket, ordType" not in source
    assert "ArraySort(allTickets);" in source
    assert "StringFindLast(" not in source
    assert "StringTrim(s)" not in source
    assert "stopRiskUsd, ocVerified ?" in source
    assert 'expServer, expCurrency' in source
    assert '(long)TimeGMT()' in source
    assert 'string dir = "MQL5\\Files' not in source
    assert 'string file = "ABUTRON_ACTIVATION_SECRET.txt"' in source
