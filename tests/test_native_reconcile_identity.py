"""Read-only native receipt reconciliation anti-spoof checks (test fixtures)."""
from __future__ import annotations
from dataclasses import replace

import pytest

from abutron.events import EventLog
from abutron.execution.ipc import ManagerRequest, ManagerSocketServer
from abutron.crypto import hmac_sign
from abutron.reconciliation.reconciler import NativeHistoryEntry, Reconciler


class NeverReconcile:
    def reconcile(self, *args, **kwargs):
        raise AssertionError("insufficient evidence was promoted to MATCHED")


def sample():
    account = {"connection_id": "fixture-cid", "mt5_login": 6207,
               "broker": "deriv", "owner_id": "fixture-owner"}
    receipt = {"receipt_id": "rcpt-fixture", "candidate_id": "cand-fixture",
               "connection_id": "fixture-cid", "mt5_login": 6207,
               "native_ticket": 7101, "broker_deal_id": "8101",
               "symbol": "Volatility 50 Index.0", "direction": "BUY",
               "volume": 0.1, "stop_loss": 98.0, "take_profit": 104.0,
               "execution_state": "ACK", "reconciled_state": "PENDING"}
    native = NativeHistoryEntry(ticket=7101, login=6207,
                                symbol="Volatility 50 Index.0",
                                volume=0.1, price=100, stop_loss=98,
                                take_profit=104, state="OPEN",
                                deal_id="8101", direction="BUY")
    return account, receipt, native


@pytest.mark.parametrize("changes,reason", [
    ({"login": 9244}, "NATIVE_ACCOUNT_TICKET_OR_SYMBOL_MISMATCH"),
    ({"symbol": "Volatility 75 Index.0"}, "NATIVE_ACCOUNT_TICKET_OR_SYMBOL_MISMATCH"),
    ({"ticket": 7102}, "NATIVE_ACCOUNT_TICKET_OR_SYMBOL_MISMATCH"),
    ({"deal_id": ""}, "NATIVE_DEAL_ID_NOT_ATTESTED"),
    ({"deal_id": "not-the-deal"}, "NATIVE_DEAL_ID_NOT_ATTESTED"),
    ({"direction": "SELL"}, "NATIVE_DIRECTION_MISMATCH"),
    ({"direction": ""}, "NATIVE_DIRECTION_MISMATCH"),
    ({"stop_loss": float("nan")}, "NATIVE_PROTECTED_POSITION_OR_VOLUME_INVALID"),
    ({"stop_loss": 97.0}, "NATIVE_STOP_WIDENED"),
    ({"take_profit": 105.0}, "NATIVE_TAKE_PROFIT_MISMATCH"),
    ({"stop_loss": 0.0}, "NATIVE_PROTECTED_POSITION_OR_VOLUME_INVALID"),
    ({"volume": 0.2}, "NATIVE_PROTECTED_POSITION_OR_VOLUME_INVALID"),
])
def test_invalid_native_receipt_never_matched(tmp_path, changes, reason):
    account, receipt, native = sample()
    rec = Reconciler(EventLog(str(tmp_path / "reconcile.jsonl")),
                     NeverReconcile())
    native = replace(native, **changes)
    report = {"matched": 0, "details": []}
    assert rec._match_receipt(receipt, native, account, report) is False
    assert report["matched"] == 0
    assert report["details"][0]["issue"] == reason


def test_symbol_volume_match_cannot_resolve_unknown(tmp_path):
    account, receipt, native = sample()
    receipt["execution_state"] = "UNKNOWN"
    receipt["native_ticket"] = 0
    receipt["broker_deal_id"] = ""
    # Original implementation incorrectly accepted symbol and lots alone.
    native = replace(native, candidate_id="", deal_id="")
    rec = Reconciler(EventLog(str(tmp_path / "unknown.jsonl")),
                     NeverReconcile())
    report = {"resolved_unknown": 0, "details": []}
    assert not rec._resolve_unknown(receipt, [native], account, report)
    assert report["resolved_unknown"] == 0
    assert report["details"][0]["issue"] == "UNKNOWN_NATIVE_IDENTITY_UNVERIFIED"


def test_foreign_account_cannot_resolve_unknown(tmp_path):
    account, receipt, native = sample()
    receipt["execution_state"] = "UNKNOWN"
    native = replace(native, candidate_id=receipt["candidate_id"],
                     login=7593)
    rec = Reconciler(EventLog(str(tmp_path / "unknown2.jsonl")),
                     NeverReconcile())
    report = {"resolved_unknown": 0, "details": []}
    assert not rec._resolve_unknown(receipt, [native], account, report)


def test_duplicate_native_ticket_never_auto_certified(tmp_path, monkeypatch):
    account, receipt, native = sample()
    from abutron import db
    # Non-invasive mocking: no actual DB write or account linking.
    monkeypatch.setattr(db.engine_db, "get_account", lambda cid: account)
    monkeypatch.setattr(db.engine_db, "list_receipts",
                        lambda cid, limit=500: [receipt])
    rec = Reconciler(EventLog(str(tmp_path / "dup.jsonl")),
                     NeverReconcile())
    report = rec.reconcile_account(
        account["connection_id"], [native, replace(native, price=100.1)])
    assert report["matched"] == 0
    assert report["unresolved"] == 1
    assert report["details"][0]["issue"] == "NATIVE_TICKET_AMBIGUOUS"


def test_manager_requires_configured_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("ABUTRON_ACTIVATION_SECRET", raising=False)
    no_secret = ManagerSocketServer()
    assert no_secret.secret == ""
    request = ManagerRequest({
        "kind": "order_handoff", "connection_id": "cid",
        "request_id": "request", "signed": "",
    })
    assert not request.verified(no_secret.secret)
    assert not request.verified("apex-activation")
    assert not request.verified("bad-secret")


def test_manager_accepts_hmac_for_correct_account_only():
    secret = "fixture-only-hmac-key"
    req = ManagerRequest({
        "kind": "order_handoff", "connection_id": "cid-6207",
        "request_id": "req-001",
        "signed": hmac_sign(secret, "req-001|cid-6207"),
    })
    assert req.verified(secret)
    wrong = ManagerRequest({**req.raw, "connection_id": "cid-9244"})
    assert not wrong.verified(secret)
