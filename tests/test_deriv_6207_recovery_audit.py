"""Deriv DEMO 6207 read-only recovery proof fixture regression matrix."""
from __future__ import annotations

import hashlib
import time

import pytest

from abutron.execution.deriv_6207_recovery_audit import (
    RecoveryUnverified, assess_position_evidence,
)


def fixture(now: float | None = None) -> dict:
    now = float(now if now is not None else time.time())
    owner, cid, nonce = "a" * 32, "b" * 32, "c" * 32
    login, server = "32456207", "Deriv-Demo"
    symbol, side = "Volatility 75 Index.0", "BUY"
    receipt_stamp = int(now) - 800
    bar = ((receipt_stamp - 300) // 300) * 300
    key = hashlib.sha256((owner + "|" + cid + "|" + symbol +
                          "|M5|" + str(bar) + "|" + side).encode()).hexdigest()
    p = {
        "symbol": symbol, "side": side, "ticket": 8829719119,
        "volume": 0.052, "sl": 39344.56,
        "tp": 39905.68, "protected": True,
    }
    evidence = [
        "APEX_BROKER_POSITION_V2", key, nonce, login, server,
        symbol, side, "8829719119", "99112", "8829719119",
        "0.05200000", "39538.600000", "39344.560000", "39905.680000",
        "10.090", "19.090", "OPEN_PROTECTED", str(int(now)-3), "0",
    ]
    receipt = [
        "APEX_NATIVE_V2", key, "ACK",
        "V4_BROKER_ACK_POSITION_STILL_UNVERIFIED",
        "8829719119", "99112", str(receipt_stamp), nonce, login, server,
    ]
    intent = [
        "APEX_OPTIONC_NATIVE_RISK_INTENT_V2", key, nonce, login, server,
        symbol, side, "9344.07", "10.1100", "0.05200000",
        "39538.600000", "39344.560000", "39905.680000",
        str(receipt_stamp - 8),
    ]
    return {
        "owner": owner, "cid": cid, "login": login,
        "server": server, "nonce": nonce, "native_position": p,
        "evidence": evidence, "receipt": receipt, "intent": intent,
        "ledger_keys": set(), "ledger_tickets": set(), "now": now,
    }


def test_realistic_protected_broker_position_is_not_automatically_recovered():
    proof = assess_position_evidence(**fixture())
    assert proof["position_state"] == "BROKER_NATIVE_OPEN_PROTECTED"
    assert proof["crm_m5_signal_reconstructed"]
    assert proof["native_risk_usd"] == 10.09
    assert proof["native_0_10_percent_contract_pass"] is False
    assert proof["ledger_coverage"] == "MISSING"
    assert proof["recovery_class"] == "UNLEDGERED_NATIVE_POSITION_WITH_POLICY_CONFLICT"
    assert proof["ledger_modified"] is False
    assert proof["execution_authorized"] is False


def test_policy_compliant_fixture_remains_manual_recovery_only():
    f = fixture()
    f["intent"][7] = "12000.00"
    proof = assess_position_evidence(**f)
    assert proof["native_0_10_percent_contract_pass"] is True
    assert proof["execution_authorized"] is False
    assert proof["ledger_modified"] is False


@pytest.mark.parametrize("field,slot,value,reason", [
    ("receipt", 2, "UNKNOWN", "NATIVE_ORDER_RECEIPT_NOT_ACK"),
    ("receipt", 5, "99113", "BROKER_DEAL_AND_POSITION_NOT_LINKED"),
    ("receipt", 7, "d"*32, "NATIVE_ORDER_RECEIPT_ACCOUNT_MISMATCH"),
    ("evidence", 16, "OPEN_UNPROTECTED", "POSITION_NOT_BROKER_PROTECTED"),
    ("evidence", 2, "d"*32, "POSITION_PROOF_ACCOUNT_BINDING_INVALID"),
    ("evidence", 14, "nan", "POSITION_NATIVE_NUMERIC_INVALID"),
    ("evidence", 17, "1600000000", "NATIVE_POSITION_PROOF_STALE"),
    ("intent", 1, "d"*64, "NATIVE_RISK_INTENT_ACCOUNT_MISMATCH"),
    ("intent", 9, "0.05300000", "NATIVE_INTENT_BROKER_VOLUME_TP_MISMATCH"),
    ("intent", 11, "39400.0", "NATIVE_BROKER_STOP_WIDENED"),
])
def test_broker_identity_geometry_and_timing_mismatch_fail(
        field, slot, value, reason):
    f = fixture()
    f[field][slot] = value
    with pytest.raises(RecoveryUnverified, match=reason):
        assess_position_evidence(**f)


def test_wrong_crm_owner_cannot_reconstruct_signal():
    f = fixture()
    f["owner"] = "d"*32
    with pytest.raises(RecoveryUnverified,
                       match="NATIVE_SIGNAL_KEY_NOT_LINKED_TO_CRM_OWNER"):
        assess_position_evidence(**f)


def test_duplicate_signal_already_on_ledger_is_not_new_recovery_candidate():
    f = fixture()
    f["ledger_keys"] = {f["evidence"][1]}
    with pytest.raises(RecoveryUnverified,
                       match="NATIVE_POSITION_ALREADY_EXISTS_IN_LEDGER"):
        assess_position_evidence(**f)


def test_duplicate_native_broker_ticket_already_in_ledger_rejected():
    f = fixture()
    f["ledger_tickets"] = {8829719119}
    with pytest.raises(RecoveryUnverified,
                       match="NATIVE_POSITION_ALREADY_EXISTS_IN_LEDGER"):
        assess_position_evidence(**f)


def test_wrong_native_position_ticket_rejected():
    f = fixture()
    f["native_position"]["ticket"] = 8829719120
    with pytest.raises(RecoveryUnverified,
                       match="BROKER_FEED_POSITION_IDENTITY_MISMATCH"):
        assess_position_evidence(**f)


def test_unprotected_broker_feed_position_rejected():
    f = fixture()
    f["native_position"]["protected"] = False
    with pytest.raises(RecoveryUnverified,
                       match="BROKER_FEED_POSITION_IDENTITY_MISMATCH"):
        assess_position_evidence(**f)
