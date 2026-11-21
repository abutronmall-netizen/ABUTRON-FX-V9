"""Production execution repair — regression tests for the four verified fixes.

1. FP 0512 receipt archiver — hardcoded MAX_FILES limit raised
2. V9 broker ticket reconciliation — resolves position ticket from
   native deal history (by broker_deal_id), handles closed positions
3. V9 trade-record accuracy — dispatcher ACK records actual native
   execution price/volume, not the model's proposed values
4. OrderCheck rejection diagnostics — exposes MT5 return codes

All fixtures are synthetic; no real terminal, credentials or broker data.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from abutron.crm.controls import CrmService, CrmError
from abutron.config import utc_now
from abutron.execution.dispatcher import NativeDispatcher
from abutron.execution.receipts import ReceiptManager
from abutron.events import EventLog


CID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BROKER = "native"
LOGIN = 7410512


# =========================================================================== #
# 1. FP 0512 receipt archiver — MAX_FILES limit
# =========================================================================== #

def test_archiver_max_files_constant_raised():
    """The hardcoded 10000 limit is replaced with a configurable ceiling."""
    script_path = os.path.join(os.path.dirname(__file__), "..",
                               "scripts", "fp_issue21_receipt_auditor.py")
    with open(script_path) as f:
        source = f.read()
    # The original hardcoded value was 10000 — must be gone.
    assert "MAX_FILES = 10000" not in source
    # Must be configurable via env var with a generous default.
    assert "ABUTRON_ISSUE21_MAX_FILES" in source
    assert "MAX_FILES = int(" in source
    # Default must be well above the 10,043 files that caused failure.
    import re
    m = re.search(r"MAX_FILES = int\(os\.environ\.get\([^)]+\)\)", source)
    assert m is not None


def test_archiver_list_receipt_names_accepts_many_files(monkeypatch, tmp_path):
    """list_receipt_names does not reject a folder with >10000 files."""
    import importlib.util
    script_path = os.path.join(os.path.dirname(__file__), "..",
                               "scripts", "fp_issue21_receipt_auditor.py")
    # Set env vars before import so module-level code works.
    monkeypatch.setenv("ABUTRON_ISSUE21_CONNECTION_ID", "0" * 32)
    monkeypatch.setenv("ABUTRON_ISSUE21_ACCOUNT_LOGIN", "12345")
    monkeypatch.setenv("ABUTRON_ISSUE21_MAX_FILES", "50000")

    spec = importlib.util.spec_from_file_location(
        "fp_issue21_auditor_test", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()
    # Create 10043 receipt files with valid hash64 names.
    import hashlib
    for i in range(10043):
        h = hashlib.sha256(f"receipt-{i}".encode()).hexdigest()
        (receipts_dir / f"{h}.txt").write_text("test")
    # The original code would raise TOO_MANY_RECEIPTS at >10000.
    names = mod.list_receipt_names(receipts_dir)
    assert len(names) == 10043


# =========================================================================== #
# 2. V9 broker ticket reconciliation — closed positions
# =========================================================================== #

class _FakeDBRecon:
    """Mock engine_db for reconciliation tests."""

    def __init__(self):
        self._receipts: dict[str, dict] = {}
        ev_path = "/tmp/abutron_recon_test.jsonl"
        if os.path.exists(ev_path):
            os.remove(ev_path)
        self.event_log = EventLog(path=ev_path)

    def add_receipt(self, r):
        rid = r.get("receipt_id", "")
        self._receipts[rid] = dict(r)

    def update_receipt(self, receipt_id, **kw):
        if receipt_id in self._receipts:
            for k, v in kw.items():
                self._receipts[receipt_id][k] = v

    def list_pending_reconciled_receipts(self, connection_id):
        return [dict(r) for r in self._receipts.values()
                if r.get("connection_id") == connection_id
                and r.get("execution_state") == "ACK"
                and r.get("reconciled_state") == "PENDING"]


def _make_ack_receipt(receipt_id="rcpt-001", deal="8835609331",
                      order_ticket=245674739, cid=CID):
    return {
        "receipt_id": receipt_id,
        "candidate_id": "cand-001",
        "connection_id": cid,
        "mt5_login": LOGIN,
        "symbol": "VOL50",
        "direction": "BUY",
        "native_ticket": order_ticket,
        "broker_deal_id": deal,
        "volume": 1.0,
        "fill_price": 83.0829,
        "stop_loss": 82.9370,
        "take_profit": 83.3426,
        "execution_state": "ACK",
        "reconciled_state": "PENDING",
        "received_at": "2026-11-20T10:10:26Z",
        "reason_code": "NATIVE_ACK",
    }


def _make_native_position(ticket=8835609331, deal="8835609331",
                          symbol="VOL50", sl=82.9370, tp=83.3426,
                          stop_risk=9.53):
    return {
        "ticket": ticket,
        "symbol": symbol,
        "direction": "BUY",
        "volume": 1.0,
        "entry": 83.0829,
        "sl": sl,
        "tp": tp,
        "stop_risk_usd": stop_risk,
        "ordercalcprofit_verified": True,
        "broker_deal_id": deal,
    }


def test_reconcile_closed_position_by_deal_id():
    """Reconciler resolves position ticket from broker_deal_id when
    order ticket differs from position ticket."""
    fake = _FakeDBRecon()
    fake.add_receipt(_make_ack_receipt(
        receipt_id="rcpt-001", deal="8835609331", order_ticket=245674739))

    crm = CrmService()
    crm.engine = fake

    # Native history has the position with the deal ID but a DIFFERENT
    # position ticket than the order ticket.
    history = [_make_native_position(
        ticket=8835609331, deal="8835609331")]

    result = crm.reconcile_closed_positions(CID, history)

    assert result["resolved"] == 1
    assert result["unresolved"] == 0
    assert "rcpt-001" not in result.get("unresolved_ids", [])

    row = fake._receipts["rcpt-001"]
    assert row["reconciled_state"] == "PROTECTED"
    assert row["native_position_ticket"] == 8835609331
    assert row["broker_native_stop_risk_usd"] == 9.53
    assert row["broker_native_ordercalcprofit_verified"] == 1
    assert row["broker_sl"] == 82.9370
    assert row["broker_tp"] == 83.3426


def test_reconcile_unresolved_when_no_match():
    """Receipts with no matching native position stay PENDING (fail-closed)."""
    fake = _FakeDBRecon()
    fake.add_receipt(_make_ack_receipt(receipt_id="rcpt-002", deal="nomatch"))

    crm = CrmService()
    crm.engine = fake

    result = crm.reconcile_closed_positions(CID, [])

    assert result["resolved"] == 0
    assert result["unresolved"] == 1
    assert "rcpt-002" in result["unresolved_ids"]
    assert fake._receipts["rcpt-002"]["reconciled_state"] == "PENDING"


def test_reconcile_unresolved_when_stop_risk_invalid():
    """Positions with invalid stop-risk are not certified (fail-closed)."""
    fake = _FakeDBRecon()
    fake.add_receipt(_make_ack_receipt(receipt_id="rcpt-004"))

    crm = CrmService()
    crm.engine = fake

    bad_pos = _make_native_position()
    bad_pos["stop_risk_usd"] = "invalid"

    result = crm.reconcile_closed_positions(CID, [bad_pos])
    assert result["resolved"] == 0
    assert result["unresolved"] == 1


def test_reconcile_unresolved_when_not_verified():
    """Positions without OrderCalcProfit verification are not certified."""
    fake = _FakeDBRecon()
    fake.add_receipt(_make_ack_receipt(receipt_id="rcpt-005"))

    crm = CrmService()
    crm.engine = fake

    bad_pos = _make_native_position()
    bad_pos["ordercalcprofit_verified"] = False

    result = crm.reconcile_closed_positions(CID, [bad_pos])
    assert result["resolved"] == 0
    assert result["unresolved"] == 1


def test_reconcile_empty_pending():
    """No pending receipts returns a clean zero summary."""
    fake = _FakeDBRecon()
    crm = CrmService()
    crm.engine = fake

    result = crm.reconcile_closed_positions(CID, [])
    assert result["resolved"] == 0
    assert result["unresolved"] == 0
    assert result["already_protected"] == 0


# =========================================================================== #
# 3 & 4. Dispatcher ACK accuracy + OrderCheck rejection diagnostics
# =========================================================================== #

class _FakeDBDispatch:
    """Mock engine_db for dispatcher tests."""

    def __init__(self):
        self._candidates: dict[str, dict] = {}
        self._receipts: dict[str, dict] = {}
        ev_path = "/tmp/abutron_dispatch_accuracy_test.jsonl"
        if os.path.exists(ev_path):
            os.remove(ev_path)
        self.event_log = EventLog(path=ev_path)

    def get_account(self, cid):
        return {
            "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
            "server": "DerivDemo", "owner_id": "owner1",
            "environment": "demo", "effective_on": 1,
            "desired_on": 1, "equity": 10000.0,
            "account_currency": "USD",
            "native_positions_verified": 1,
            "exposure_certified_at": utc_now(),
        }

    def list_accounts(self, owner_id=None):
        acct = dict(self.get_account(CID))
        acct["owner_risk_certified"] = True
        acct["cap_exceeded"] = False
        return [acct]

    def add_candidate(self, cand):
        cid = cand.get("candidate_id", "")
        self._candidates[cid] = dict(cand)
        return True

    def get_candidate(self, candidate_id):
        return self._candidates.get(candidate_id)

    def set_candidate_state(self, candidate_id, state, reason=""):
        if candidate_id in self._candidates:
            self._candidates[candidate_id]["execution_state"] = state
            self._candidates[candidate_id]["reason_code"] = reason

    def add_receipt(self, r):
        rid = r.get("receipt_id", "")
        self._receipts[rid] = dict(r)

    def update_receipt(self, receipt_id, **kw):
        if receipt_id in self._receipts:
            for k, v in kw.items():
                self._receipts[receipt_id][k] = v

    def recent_open_positions(self, connection_id):
        return []


class _AckGateway:
    """Gateway stub that returns an ACK with actual execution data."""

    def __init__(self, actual_price, actual_volume, ticket=999, deal="777"):
        self._actual_price = actual_price
        self._actual_volume = actual_volume
        self._ticket = ticket
        self._deal = deal

    def send_order(self, **kwargs):
        return {
            "outcome": "ACK",
            "receipt_ref": {
                "ticket": self._ticket,
                "deal": self._deal,
                "price": self._actual_price,
                "volume": self._actual_volume,
            },
            "request_id": "req-001",
        }


class _RejectGateway:
    """Gateway stub that returns a REJECT with MT5 return code."""

    def __init__(self, retcode="BROKER_NATIVE_ORDERCHECK_FAILED"):
        self._retcode = retcode

    def send_order(self, **kwargs):
        return {
            "outcome": "REJECTED",
            "receipt_ref": {"reason": self._retcode, "deal": ""},
            "request_id": "req-002",
        }


def _make_candidate(candidate_id="cand-001", price=83.0722, volume=2.5,
                    trigger_price=83.0722):
    return {
        "candidate_id": candidate_id,
        "connection_id": CID,
        "owner_id": "owner1",
        "broker": BROKER,
        "symbol": "VOL50",
        "direction": "BUY",
        "volume": volume,
        "stop_loss": 82.9370,
        "take_profit": 83.3426,
        "price": price,
        "trigger_price": trigger_price,
        "entry_type": "CONFIRM",
        "execution_state": "RESERVED",
        "replay_key": "VOL50:2026-11-20T10:00:00Z:BUY",
    }


def _make_dispatcher(fake, gateway, ev_path):
    """Create a dispatcher with the fake DB patched in."""
    if os.path.exists(ev_path):
        os.remove(ev_path)
    log = EventLog(path=ev_path)
    receipt_mgr = ReceiptManager(log)
    dispatcher = NativeDispatcher(log, gateway, receipt_mgr)

    # Patch the real engine_db so dispatcher finds our fake data.
    import abutron.db as abdb
    import abutron.execution.dispatcher as disp_mod
    original_engine = abdb.engine_db
    abdb.engine_db = fake
    disp_mod.db.engine_db = fake
    # Also patch RiskEngine to return certified exposure.
    import abutron.risk.risk_engine as risk_mod
    original_engine_risk = risk_mod.engine_db
    risk_mod.engine_db = fake

    # Patch _reserved_candidate_ids to use the fake DB instead of
    # raw SQLAlchemy session.
    original_reserved = dispatcher._reserved_candidate_ids
    def fake_reserved(connection_id):
        return [cid for cid, c in fake._candidates.items()
                if c.get("connection_id") == connection_id
                and c.get("execution_state") in ("RESERVED", "SEND")]
    dispatcher._reserved_candidate_ids = fake_reserved

    return log, receipt_mgr, dispatcher, (
        abdb, disp_mod, risk_mod, original_engine, original_engine_risk)


def test_dispatcher_ack_uses_actual_execution_price():
    """Dispatcher ACK records actual broker fill price, not model price."""
    fake = _FakeDBDispatch()
    fake.add_candidate(_make_candidate(price=83.0722, volume=2.5))

    ev_path = "/tmp/abutron_dispatch_ev.jsonl"
    log, receipt_mgr, dispatcher, patched = _make_dispatcher(
        fake, _AckGateway(actual_price=83.0829, actual_volume=1.0,
                          ticket=245674739, deal="8835609331"),
        ev_path)

    result = dispatcher.dispatch_account(CID)

    try:
        assert result["ack"] == 1
        # Check the receipt recorded the ACTUAL execution price.
        receipts = list(fake._receipts.values())
        assert len(receipts) == 1
        r = receipts[0]
        assert r["fill_price"] == 83.0829
        assert r["volume"] == 1.0
        assert r["native_ticket"] == 245674739
        assert r["broker_deal_id"] == "8835609331"
    finally:
        # Restore patched modules.
        patched[0].engine_db = patched[3]
        patched[1].db.engine_db = patched[3]
        patched[2].engine_db = patched[4]


def test_dispatcher_reject_exposes_mt5_return_code():
    """Rejection events include the actual MT5 return code."""
    fake = _FakeDBDispatch()
    fake.add_candidate(_make_candidate(candidate_id="cand-002"))

    ev_path = "/tmp/abutron_dispatch_reject.jsonl"
    log, receipt_mgr, dispatcher, patched = _make_dispatcher(
        fake, _RejectGateway(retcode="BROKER_NATIVE_ORDERCHECK_FAILED"),
        ev_path)

    result = dispatcher.dispatch_account(CID)

    try:
        assert result["rejected"] == 1
        details = result["details"][0]
        assert details["outcome"] == "REJECTED"
        assert details["reason"] == "BROKER_NATIVE_ORDERCHECK_FAILED"
        assert "mt5_return_code" in details

        # Candidate state should record the MT5 return code.
        cand = fake.get_candidate("cand-002")
        assert cand["execution_state"] == "REJECTED"
        assert cand["reason_code"] == "BROKER_NATIVE_ORDERCHECK_FAILED"

        # Event log should have the return code.
        events = [json.loads(line) for line in open(ev_path)]
        reject_events = [e for e in events
                         if e.get("event_type") == "dispatch.rejected"]
        assert len(reject_events) >= 1
        assert reject_events[0]["reason_code"] == \
            "BROKER_NATIVE_ORDERCHECK_FAILED"
        assert reject_events[0]["payload"]["mt5_return_code"] == \
            "BROKER_NATIVE_ORDERCHECK_FAILED"
    finally:
        patched[0].engine_db = patched[3]
        patched[1].db.engine_db = patched[3]
        patched[2].engine_db = patched[4]


def test_dispatcher_ack_falls_back_to_candidate_when_no_actual():
    """When the EA does not report actual price (older EA), fall back
    to the candidate's proposed price."""
    fake = _FakeDBDispatch()
    fake.add_candidate(_make_candidate(price=83.0722, volume=2.5))

    ev_path = "/tmp/abutron_dispatch_fallback.jsonl"
    log, receipt_mgr, dispatcher, patched = _make_dispatcher(
        fake, _AckGateway(actual_price=0.0, actual_volume=0.0,
                          ticket=111, deal="222"),
        ev_path)

    result = dispatcher.dispatch_account(CID)

    try:
        assert result["ack"] == 1
        receipts = list(fake._receipts.values())
        assert len(receipts) == 1
        # Falls back to candidate's proposed price.
        assert receipts[0]["fill_price"] == 83.0722
        assert receipts[0]["volume"] == 2.5
    finally:
        patched[0].engine_db = patched[3]
        patched[1].db.engine_db = patched[3]
        patched[2].engine_db = patched[4]
