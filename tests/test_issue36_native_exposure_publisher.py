"""Issue #36: native MT5 exposure publisher — HMAC-authenticated.

Covers the full corrected certification chain:
- The OLD path (a manually supplied ``native_positions_verified=True`` flag)
  could report certified exposure with zero broker-native proof.
- The NEW path requires an HMAC-authenticated, account-bound broker snapshot:
  the EA signs a canonical message with the activation secret; Python
  verifies the HMAC sig, the nonce freshness (replay protection), the
  account-binding (connection_id + login), and reconciles every position
  into stop-risk. A fabricated snapshot without the secret, a replayed
  stale snapshot, a misbound snapshot, or a swapped-positions snapshot
  cannot certify exposure (fail-closed).
"""
import time
import types

import pytest

from abutron.crm.controls import CrmError, crm
from abutron.crypto import (activation_secret, build_exposure_canonical,
                            hmac_sign, parse_native_nonce)
from abutron.exposure.publisher import publish_exposure
from abutron.risk.risk_engine import RiskEngine, _certification_stale

SECRET = activation_secret()


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


def _signed_snapshot(connection_id="fp1", login="123456", equity="10000.00",
                     positions=None, nonce=None, currency="USD",
                     secret=SECRET):
    """Build a properly HMAC-signed snapshot (as the EA emits)."""
    nonce = nonce or f"ABUTRON_NATIVE_{login}_{int(time.time())}"
    snap = {
        "nonce": nonce, "challenge": "chal",
        "connection_id": connection_id, "login": login,
        "equity": equity, "balance": "10000.00", "currency": currency,
        "server": "synthetic-demo-server",
        "position_count": len(positions or []),
        "positions": positions if positions is not None else [],
    }
    snap["sig"] = hmac_sign(
        secret,
        build_exposure_canonical(
            connection_id, login, 10000.0, nonce, snap["positions"],
            server=snap["server"], currency=snap["currency"],
            balance=float(snap["balance"]), challenge=snap["challenge"]))
    return snap


def _account(login=123456):
    return {
        "connection_id": "fp1", "mt5_login": login,
        "server": "synthetic-demo-server",
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
    _make_crm(monkeypatch, _account())
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=True)


def test_invalid_nonce_rejected(monkeypatch):
    _make_crm(monkeypatch, _account())
    snap = _signed_snapshot(nonce="not-a-valid-nonce")
    # recompute sig over the bad nonce so only the nonce format fails
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


def test_non_positive_equity_rejected(monkeypatch):
    _make_crm(monkeypatch, _account())
    snap = _signed_snapshot(equity="0.00")
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


# ------------------------------------------- HMAC authentication
def test_forged_signature_rejected(monkeypatch):
    """A snapshot with a forged/invalid HMAC sig cannot certify."""
    _make_crm(monkeypatch, _account())
    snap = _signed_snapshot()
    snap["sig"] = "0" * 64  # forged signature
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


def test_wrong_secret_signature_rejected(monkeypatch):
    """A snapshot signed with a different secret is rejected."""
    _make_crm(monkeypatch, _account())
    snap = _signed_snapshot(secret="wrong-secret")
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


def test_stale_nonce_rejected(monkeypatch):
    """A replayed/stale nonce (old timestamp) is rejected."""
    _make_crm(monkeypatch, _account())
    old_ts = int(time.time()) - 3600
    snap = _signed_snapshot(nonce=f"ABUTRON_NATIVE_123456_{old_ts}")
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


def test_nonce_login_mismatch_rejected(monkeypatch):
    """Nonce login must match the snapshot login."""
    _make_crm(monkeypatch, _account())
    snap = _signed_snapshot()
    # break the nonce's login (sig still valid over snapshot fields, but
    # nonce login now mismatches -> internal inconsistency)
    snap["nonce"] = f"ABUTRON_NATIVE_999999_{int(time.time())}"
    snap["sig"] = hmac_sign(SECRET, build_exposure_canonical(
        "fp1", "123456", 10000.0, snap["nonce"], []))
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


def test_swapped_positions_break_signature(monkeypatch):
    """Modifying positions after signing breaks the HMAC (fail-closed)."""
    receipt = {"receipt_id": "r1", "connection_id": "fp1",
               "native_ticket": 5001, "symbol": "EURUSD.r",
               "direction": "BUY", "volume": 0.1, "fill_price": 1.10,
               "stop_loss": 1.09, "execution_state": "OPEN"}
    engine = _make_crm(monkeypatch, _account(), receipts=[receipt])
    pos = [{"ticket": 5001, "symbol": "EURUSD.r", "direction": "BUY",
            "volume": 0.1, "entry": 1.10, "sl": 1.09,
            "stop_risk_usd": 10.0, "ordercalcprofit_verified": True}]
    snap = _signed_snapshot(positions=pos)
    # Tamper with a position after signing.
    snap["positions"][0]["volume"] = 99.0
    with pytest.raises(CrmError):
        engine.certify_exposure("fp1", snapshot=snap)


# ------------------------------------------- account-binding
def test_misbound_connection_id_rejected(monkeypatch):
    _make_crm(monkeypatch, _account())
    snap = _signed_snapshot(connection_id="other_account")
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


def test_wrong_login_rejected(monkeypatch):
    _make_crm(monkeypatch, _account(login=123456))
    snap = _signed_snapshot(login="999999")
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


# ------------------------------------------- reconciliation
def test_untracked_broker_position_rejected(monkeypatch):
    _make_crm(monkeypatch, _account(), receipts=[])
    pos = [{"ticket": 5001, "symbol": "EURUSD.r", "direction": "BUY",
            "volume": 0.1, "entry": 1.10, "sl": 1.09,
            "stop_risk_usd": 10.0, "ordercalcprofit_verified": True}]
    snap = _signed_snapshot(positions=pos)
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


def test_stale_receipt_rejected(monkeypatch):
    receipt = {"receipt_id": "r1", "connection_id": "fp1",
               "native_ticket": 5001, "symbol": "EURUSD.r",
               "direction": "BUY", "volume": 0.1, "fill_price": 1.10,
               "stop_loss": 1.09, "execution_state": "OPEN"}
    _make_crm(monkeypatch, _account(), receipts=[receipt])
    snap = _signed_snapshot(positions=[])
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


def test_position_without_ordercalcprofit_rejected(monkeypatch):
    receipt = {"receipt_id": "r1", "connection_id": "fp1",
               "native_ticket": 5001, "symbol": "EURUSD.r",
               "direction": "BUY", "volume": 0.1, "fill_price": 1.10,
               "stop_loss": 1.09, "execution_state": "OPEN"}
    _make_crm(monkeypatch, _account(), receipts=[receipt])
    pos = [{"ticket": 5001, "symbol": "EURUSD.r", "direction": "BUY",
            "volume": 0.1, "entry": 1.10, "sl": 1.09,
            "stop_risk_usd": 10.0, "ordercalcprofit_verified": False}]
    snap = _signed_snapshot(positions=pos)
    with pytest.raises(CrmError):
        crm.certify_exposure("fp1", snapshot=snap)


def test_reconciled_position_certifies_with_stop_risk(monkeypatch):
    receipt = {"receipt_id": "r1", "connection_id": "fp1",
               "native_ticket": 5001, "symbol": "EURUSD.r",
               "direction": "BUY", "volume": 0.1, "fill_price": 1.10,
               "stop_loss": 1.09, "execution_state": "OPEN"}
    engine = _make_crm(monkeypatch, _account(), receipts=[receipt])
    pos = [{"ticket": 5001, "symbol": "EURUSD.r", "direction": "BUY",
            "volume": 0.1, "entry": 1.10, "sl": 1.09,
            "stop_risk_usd": 10.0, "ordercalcprofit_verified": True}]
    snap = _signed_snapshot(positions=pos)
    result = engine.certify_exposure("fp1", snapshot=snap)
    assert result["native_positions_verified"] == 1
    assert result["exposure_snapshot_count"] == 1
    open_pos = engine.engine.recent_open_positions("fp1")
    assert open_pos[0]["broker_native_stop_risk_usd"] == 10.0
    assert open_pos[0]["broker_native_ordercalcprofit_verified"] == 1


def test_flat_authenticated_snapshot_certifies(monkeypatch):
    """A validly-signed flat (zero-position) snapshot certifies."""
    engine = _make_crm(monkeypatch, _account())
    snap = _signed_snapshot(positions=[])
    result = engine.certify_exposure("fp1", snapshot=snap)
    assert result["native_positions_verified"] == 1
    assert result["exposure_snapshot_count"] == 0
    assert result["exposure_nonce"] == snap["nonce"]


# ------------------------------------------- stale certification
def test_stale_certification_expires():
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


# ------------------------------------------- nonce parser
def test_parse_native_nonce_valid():
    nonce = f"ABUTRON_NATIVE_123456_{int(time.time())}"
    parsed = parse_native_nonce(nonce)
    assert parsed is not None
    assert parsed[0] == "123456"
    assert isinstance(parsed[1], int)


def test_parse_native_nonce_invalid():
    assert parse_native_nonce("not-a-nonce") is None
    assert parse_native_nonce("ABUTRON_NATIVE_abc") is None
    assert parse_native_nonce("ABUTRON_NATIVE_123456_notanumber") is None


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
    """Full publisher path: valid signed snapshot -> certify_exposure -> certified."""
    snap = _signed_snapshot()

    class _Mgr:
        def read_native_exposure(self, cid):
            return snap

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
    assert result["exposure_nonce"] == snap["nonce"]
    assert result["exposure_snapshot_count"] == 0


def test_publisher_rejects_unsigned_snapshot(monkeypatch):
    """A snapshot with no valid sig cannot certify via the publisher."""
    snap = _signed_snapshot()
    snap["sig"] = ""  # strip the signature

    class _Mgr:
        def read_native_exposure(self, cid):
            return snap

    monkeypatch.setattr(crm, "engine", types.SimpleNamespace(
        get_account=lambda cid: dict(_account()) if cid == "fp1" else None,
        upsert_account=lambda a: None,
        recent_open_positions=lambda cid: [],
        update_receipt=lambda *a, **k: None))
    with pytest.raises(CrmError):
        publish_exposure("fp1", manager=_Mgr())
