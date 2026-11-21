"""Tests for the read-only unresolved-receipt diagnostic.

Verifies that ``classify_receipt`` mirrors ``reconcile_engine_receipts``
and reports the exact failure reason for each receipt that would land in
the ``unresolved`` bucket. No broker, terminal, or real DB — synthetic
fixtures only.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

# ops/v9-cutover has a hyphen; load the module by file path.
_spec = importlib.util.spec_from_file_location(
    "diagnose_unresolved_receipts",
    os.path.join(os.path.dirname(__file__), "..",
                 "ops", "v9-cutover", "diagnose_unresolved_receipts.py"))
_diag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_diag)
classify_receipt = _diag.classify_receipt


def _receipt(**overrides):
    r = {
        "receipt_id": "rcpt_test0001",
        "candidate_id": "cand_001",
        "connection_id": "a" * 32,
        "symbol": "Volatility 50 Index.0",
        "direction": "BUY",
        "native_ticket": 1001,
        "broker_deal_id": "5001",
        "volume": 1.0,
        "fill_price": 100.0,
        "stop_loss": 99.0,
        "take_profit": 101.0,
        "execution_state": "ACK",
        "reconciled_state": "PENDING",
        "received_at": "2026-11-20T10:00:00+00:00",
        "reason_code": "",
        "broker_native_stop_risk_usd": 0.0,
        "broker_native_ordercalcprofit_verified": 0,
        "native_position_ticket": 0,
    }
    r.update(overrides)
    return r


def _candidate(**overrides):
    c = {
        "candidate_id": "cand_001",
        "symbol": "Volatility 50 Index.0",
        "direction": "BUY",
        "volume": 1.0,
        "stop_loss": 99.0,
        "take_profit": 101.0,
    }
    c.update(overrides)
    return c


def _exposure(*positions):
    return {"positions": list(positions), "position_count": len(positions)}


def _protected_pos(ticket=1001, symbol="Volatility 50 Index.0",
                   direction="BUY", sl=99.0, tp=101.0, volume=1.0):
    return {
        "ticket": ticket, "symbol": symbol, "direction": direction,
        "sl": sl, "tp": tp, "volume": volume,
        "stop_risk_usd": 1.5, "ordercalcprofit_verified": True,
    }


# --------------------------------------------------------------------- #
# Already-protected receipts are not unresolved
# --------------------------------------------------------------------- #

def test_already_protected_not_unresolved():
    receipt = _receipt(reconciled_state="PROTECTED",
                       broker_native_ordercalcprofit_verified=1,
                       broker_native_stop_risk_usd=1.5,
                       native_position_ticket=1001)
    unresolved, reason, _ = classify_receipt(
        receipt, _candidate(), _exposure(), {"Volatility 50 Index.0": 0.1},
        secret="s", cid="a" * 32, login=6207, server="Deriv-Demo",
        files_dir="/tmp")
    assert unresolved is False
    assert reason == "ALREADY_PROTECTED"


def test_non_ack_skipped():
    receipt = _receipt(execution_state="REJECTED")
    unresolved, reason, _ = classify_receipt(
        receipt, _candidate(), _exposure(), {"Volatility 50 Index.0": 0.1},
        secret="s", cid="a" * 32, login=6207, server="Deriv-Demo",
        files_dir="/tmp")
    assert unresolved is False
    assert reason == "SKIPPED_NON_ACK"


# --------------------------------------------------------------------- #
# Unresolved reasons
# --------------------------------------------------------------------- #

def test_candidate_missing(monkeypatch):
    # broker_position_from_native_deal returns None without a real DB,
    # and match_protective_order with no positions returns pending.
    unresolved, reason, detail = classify_receipt(
        _receipt(), None, _exposure(), {"Volatility 50 Index.0": 0.1},
        secret="s", cid="a" * 32, login=6207, server="Deriv-Demo",
        files_dir="/tmp")
    assert unresolved is True
    assert reason == "CANDIDATE_MISSING"


def test_tick_size_invalid():
    unresolved, reason, detail = classify_receipt(
        _receipt(), _candidate(), _exposure(), {},
        secret="s", cid="a" * 32, login=6207, server="Deriv-Demo",
        files_dir="/tmp")
    assert unresolved is True
    assert reason == "TICK_SIZE_INVALID"


def test_position_proof_pending():
    # No matching position in current exposure -> POSITION_PROOF_PENDING.
    # broker_position_from_native_deal returns None (no real DB), so
    # native_position falls back to the receipt ticket, but exposure has
    # no matching position.
    unresolved, reason, _ = classify_receipt(
        _receipt(native_ticket=1001), _candidate(),
        _exposure(_protected_pos(ticket=9999)),
        {"Volatility 50 Index.0": 0.1},
        secret="s", cid="a" * 32, login=6207, server="Deriv-Demo",
        files_dir="/tmp")
    assert unresolved is True
    assert reason == "POSITION_PROOF_PENDING"


def test_would_be_protected():
    unresolved, reason, detail = classify_receipt(
        _receipt(native_ticket=1001), _candidate(),
        _exposure(_protected_pos(ticket=1001)),
        {"Volatility 50 Index.0": 0.1},
        secret="s", cid="a" * 32, login=6207, server="Deriv-Demo",
        files_dir="/tmp")
    assert unresolved is False
    assert reason == "WOULD_BE_PROTECTED"
    assert detail["ticket"] == 1001


def test_unknown_no_native_receipt(monkeypatch):
    receipt = _receipt(execution_state="UNKNOWN",
                       reason_code="SEND_UNKNOWN")
    # recover_native_delayed_receipt returns None with no real files.
    unresolved, reason, _ = classify_receipt(
        receipt, _candidate(), _exposure(), {"Volatility 50 Index.0": 0.1},
        secret="s", cid="a" * 32, login=6207, server="Deriv-Demo",
        files_dir="/tmp")
    assert unresolved is True
    assert reason == "UNKNOWN_NO_NATIVE_RECEIPT"


def test_unknown_recovered_rejected(monkeypatch):
    receipt = _receipt(execution_state="UNKNOWN",
                       reason_code="REQUEST_ID=ABUTRON_REQ_abc;timeout")

    def _fake_recover(files_dir, *, request_id, login):
        return {"state": "REJECTED", "reason": "ORDER_CHECK_FAILED"}

    monkeypatch.setattr(_diag, "recover_native_delayed_receipt", _fake_recover)
    unresolved, reason, detail = classify_receipt(
        receipt, _candidate(), _exposure(), {"Volatility 50 Index.0": 0.1},
        secret="s", cid="a" * 32, login=6207, server="Deriv-Demo",
        files_dir="/tmp")
    assert unresolved is True
    assert reason == "UNKNOWN_RECOVERED_REJECTED"
