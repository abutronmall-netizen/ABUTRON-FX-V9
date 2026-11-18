"""Issue #36: native MT5 exposure publisher — corrected.

Covers the full corrected certification chain:
- The OLD path (a manually supplied ``native_positions_verified=True`` flag)
  could report certified exposure with zero broker-native proof.
- The NEW path requires an authenticated, account-bound broker-native
  snapshot (positions + equity, bound to connection_id + login, carrying a
  valid ``ABUTRON_NATIVE_*`` nonce). Certification is derived from that
  evidence; blind flags, forged nonces, misbound snapshots, wrong logins,
  untracked positions, stale receipts and stale certifications are all
  rejected fail-closed.
"""
import json
import time
import types

import pytest

from abutron.crm.controls import CrmError, crm
from abutron.crypto import make_nonce
from abutron.exposure.publisher import publish_exposure
from abutron.risk.risk_engine import RiskEngine, _certification_stale


# ---------------------------------------------------------------- helpers
def _make_crm(monkeypatch, account, receipts=None):
    """Wire a CrmService whose engine_db reads/writes in-memory state."""
    store = dict(account)
    receipt_store = {r["receipt_id"]: dict(r) for r in (receipts or [])}

    class _DB:
        def get_account(self, cid):
            return dict(store) if cid == store.get("connection_id") else None

        def upsert_account(self, acct):
            store.update(acct)

        def recent_open_positions(self, cid):
            return [dict(r) for r in receipt_store.values()
                    if r.get("connection_id") == cid
                    and r.get("execution_state") in ("OPEN", "PROTECTED")]

        def update_receipt(self, receipt_id, **kw):
            if receipt_id in receipt_store:
                receipt_store[receipt_id].update(kw)

    monkeypatch.setattr(crm, "engine", _DB())
    return crm


def _snapshot(connection_id="fp1", login="123456", equity="10000.00",
              positions=None, nonce=None, currency="USD"):
    return {
        "nonce": nonce or make_nonce(),
        "challenge": "chal",
        "connection_id": connection_id,
        "login": login,
        "equity": equity,
        "balance": "10000.00",
        "currency": currency,
        "positions": positions if positions is not None else [],
    }


def _account(login=123456):
    return {
        "connection_id": "fp1", "mt5_login": login,
        "equity": 0.0, "account_currency": "", "native_positions_verified": 0,
        "exposure_certified_at": "",
    }


# ------------------------------------------- blind flag / forged nonce
def test_blind_flag_cannot_certify(monkeypatch):
    """The old boolean-flag signature no longer exists."""
    _make_crm(monkeypatch, _account())
    with pytest.raises(TypeError):
        crm.certify_exposure("fp1", equity=10000.0, account_currency="USD",
                             native_positions_verified=True)


def test_non_snapshot_dict_rejected(monkeypatch):
    """Passing a bare boolean/non-dict is rejected, not certified."""
    _make_crm(monkeypatch, _account())
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=True)


def test_invalid_nonce_rejected(monkeypatch):
    _make_crm(monkeypatch, _account())
    snap = _snapshot(nonce="not-a-valid-nonce")
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


def test_non_positive_equity_rejected(monkeypatch):
    _make_crm(monkeypatch, _account())
    snap = _snapshot(equity="0.00")
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


# ------------------------------------------- account-binding
def test_misbound_connection_id_rejected(monkeypatch):
    """A snapshot for a different account cannot certify this account."""
    _make_crm(monkeypatch, _account())
    snap = _snapshot(connection_id="other_account")
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


def test_wrong_login_rejected(monkeypatch):
    """A snapshot from a different terminal (login mismatch) is rejected."""
    _make_crm(monkeypatch, _account(login=123456))
    snap = _snapshot(login="999999")
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


# ------------------------------------------- reconciliation
def test_untracked_broker_position_rejected(monkeypatch):
    """A broker position with no matching open receipt fails certification."""
    _make_crm(monkeypatch, _account(), receipts=[])
    pos = [{"ticket": 5001, "symbol": "EURUSD.r", "direction": "BUY",
            "volume": 0.1, "entry": 1.10, "sl": 1.09,
            "stop_risk_usd": 10.0, "ordercalcprofit_verified": True}]
    snap = _snapshot(positions=pos)
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


def test_stale_receipt_rejected(monkeypatch):
    """An open receipt with no matching broker position fails certification."""
    receipt = {"receipt_id": "r1", "connection_id": "fp1",
               "native_ticket": 5001, "symbol": "EURUSD.r",
               "direction": "BUY", "volume": 0.1, "fill_price": 1.10,
               "stop_loss": 1.09, "execution_state": "OPEN"}
    _make_crm(monkeypatch, _account(), receipts=[receipt])
    # Snapshot is flat but the receipt is still open -> discrepancy.
    snap = _snapshot(positions=[])
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


def test_position_without_ordercalcprofit_rejected(monkeypatch):
    """A position lacking OrderCalcProfit verification fails certification."""
    receipt = {"receipt_id": "r1", "connection_id": "fp1",
               "native_ticket": 5001, "symbol": "EURUSD.r",
               "direction": "BUY", "volume": 0.1, "fill_price": 1.10,
               "stop_loss": 1.09, "execution_state": "OPEN"}
    _make_crm(monkeypatch, _account(), receipts=[receipt])
    pos = [{"ticket": 5001, "symbol": "EURUSD.r", "direction": "BUY",
            "volume": 0.1, "entry": 1.10, "sl": 1.09,
            "stop_risk_usd": 10.0, "ordercalcprofit_verified": False}]
    snap = _snapshot(positions=pos)
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


def test_reconciled_position_certifies_with_stop_risk(monkeypatch):
    """A matched, verified position is reconciled into stop-risk + certified."""
    receipt = {"receipt_id": "r1", "connection_id": "fp1",
               "native_ticket": 5001, "symbol": "EURUSD.r",
               "direction": "BUY", "volume": 0.1, "fill_price": 1.10,
               "stop_loss": 1.09, "execution_state": "OPEN"}
    engine = _make_crm(monkeypatch, _account(), receipts=[receipt])
    pos = [{"ticket": 5001, "symbol": "EURUSD.r", "direction": "BUY",
            "volume": 0.1, "entry": 1.10, "sl": 1.09,
            "stop_risk_usd": 10.0, "ordercalcprofit_verified": True}]
    snap = _snapshot(positions=pos)
    result = engine.certify_exposure("fp1", snapshot=snap)
    assert result["native_positions_verified"] == 1
    assert result["exposure_snapshot_count"] == 1
    # The receipt now carries the broker-native stop-risk.
    open_pos = engine.engine.recent_open_positions("fp1")
    assert open_pos[0]["broker_native_stop_risk_usd"] == 10.0
    assert open_pos[0]["broker_native_ordercalcprofit_verified"] == 1


# ------------------------------------------- stale certification
def test_stale_certification_expires(monkeypatch):
    """A certification older than the TTL is treated as uncertified."""
    old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() - 600))
    assert _certification_stale(old_ts, 300) is True


def test_fresh_certification_not_stale():
    fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    assert _certification_stale(fresh, 300) is False


def test_missing_certification_is_stale():
    assert _certification_stale("", 300) is True


def test_malformed_certification_timestamp_is_stale():
    assert _certification_stale("not-a-timestamp", 300) is True


def test_stale_certification_blocks_risk_engine(monkeypatch):
    """aggregate_owner_risk treats a stale certification as uncertified."""
    from abutron.risk import risk_engine
    old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() - 600))
    acct = {"connection_id": "fp1", "equity": 10000.0,
            "account_currency": "USD", "native_positions_verified": 1,
            "exposure_certified_at": old_ts}
    monkeypatch.setattr(risk_engine, "engine_db", types.SimpleNamespace(
        list_accounts=lambda owner_id: [acct],
        recent_open_positions=lambda cid: [],
    ))
    re = risk_engine.RiskEngine.__new__(risk_engine.RiskEngine)
    result = re.aggregate_owner_risk("owner")
    assert result["owner_risk_certified"] is False


# ------------------------------------------- round-trip format
def test_native_snapshot_round_trip_valid_json():
    """The MQL5 export format (corrected escaping) parses as valid JSON.

    Simulates the exact JSON the corrected ApexNativeWriter.mq5 emits:
    single-backslash quote escaping (\\") producing plain JSON quotes, NOT
    the broken \\\\\\\" form that produced invalid JSON.
    """
    nonce = make_nonce()
    # This is the string the corrected MQL5 StringFormat produces: equity
    # and balance are JSON numbers (unquoted), quotes are plain " (not \").
    raw = ('{"nonce":"%s","challenge":"chal","connection_id":"fp1",'
           '"login":"123456","equity":10000.00,"balance":10000.00,'
           '"currency":"USD","positions":[{"ticket":5001,'
           '"symbol":"EURUSD.r","direction":"BUY","volume":0.10,'
           '"entry":1.10,"sl":1.09,"tp":1.12,"swap":0.0,'
           '"stop_risk_usd":10.00,"ordercalcprofit_verified":true}]}' % nonce)
    snapshot = json.loads(raw)  # must not raise
    assert snapshot["nonce"] == nonce
    assert snapshot["connection_id"] == "fp1"
    assert snapshot["login"] == "123456"
    assert snapshot["positions"][0]["ticket"] == 5001
    assert snapshot["positions"][0]["ordercalcprofit_verified"] is True
    assert snapshot["positions"][0]["stop_risk_usd"] == 10.0


def test_broken_escaping_is_invalid_json():
    """The OLD MQL5 escaping (\\\\\\") produces invalid JSON (regression guard)."""
    raw = '{\\"nonce\\":\\"abc\\"}'  # what the broken form emitted
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


# ------------------------------------------- publisher path
def test_publisher_rejects_missing_snapshot(monkeypatch):
    class _Mgr:
        def read_native_exposure(self, cid):
            return None

    monkeypatch.setattr(crm, "engine", types.SimpleNamespace(
        get_account=lambda cid: None, upsert_account=lambda a: None,
        recent_open_positions=lambda cid: [], update_receipt=lambda *a, **k: None))
    with pytest.raises(CrmError):
        publish_exposure("fp1", manager=_Mgr())


def test_publisher_rejects_forged_nonce(monkeypatch):
    class _Mgr:
        def read_native_exposure(self, cid):
            return {"nonce": "forged", "equity": "10000.00",
                    "currency": "USD", "positions": []}

    monkeypatch.setattr(crm, "engine", types.SimpleNamespace(
        get_account=lambda cid: None, upsert_account=lambda a: None,
        recent_open_positions=lambda cid: [], update_receipt=lambda *a, **k: None))
    with pytest.raises(CrmError):
        publish_exposure("fp1", manager=_Mgr())


def test_publisher_certifies_from_authenticated_snapshot(monkeypatch):
    """Full publisher path: valid account-bound snapshot -> certified."""
    nonce = make_nonce()
    snapshot = {
        "nonce": nonce, "challenge": "chal", "connection_id": "fp1",
        "login": "123456", "equity": "10000.00", "balance": "10000.00",
        "currency": "USD", "positions": [],
    }

    class _Mgr:
        def read_native_exposure(self, cid):
            return snapshot

    store = _account()
    receipts = {}

    class _DB:
        def get_account(self, cid):
            return dict(store) if cid == "fp1" else None

        def upsert_account(self, acct):
            store.update(acct)

        def recent_open_positions(self, cid):
            return [dict(r) for r in receipts.values()]

        def update_receipt(self, receipt_id, **kw):
            receipts[receipt_id].update(kw)

    monkeypatch.setattr(crm, "engine", _DB())
    result = publish_exposure("fp1", manager=_Mgr())
    assert result["native_positions_verified"] == 1
    assert result["exposure_nonce"] == nonce
    assert result["exposure_snapshot_count"] == 0
