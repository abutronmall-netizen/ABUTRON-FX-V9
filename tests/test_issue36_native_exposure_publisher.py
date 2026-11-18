"""Issue #36: native MT5 exposure publisher binds certification to broker evidence.

Reproduces the defect and proves the fix:
- The OLD path (certify_exposure with a manually supplied ``native_positions_
  verified=True`` flag) could report certified exposure with zero broker-native
  proof.
- The NEW path requires an authenticated broker-native snapshot (positions
  list + a valid ``ABUTRON_NATIVE_*`` nonce). Certification is derived from
  that evidence; a blind flag or invalid nonce is rejected fail-closed.
"""
import types

import pytest

from abutron.crm.controls import CrmError, CrmService, crm
from abutron.crypto import make_nonce
from abutron.exposure.publisher import publish_exposure


def _make_crm(monkeypatch, account):
    """Wire a CrmService whose engine_db reads/writes an in-memory account."""
    store = dict(account)

    class _DB:
        def get_account(self, cid):
            return dict(store) if cid == store.get("connection_id") else None

        def upsert_account(self, acct):
            store.update(acct)

    monkeypatch.setattr(crm, "engine", _DB())
    return crm


def test_blind_flag_cannot_certify_without_broker_evidence(monkeypatch):
    """A manually supplied boolean is no longer a certification path."""
    _make_crm(monkeypatch, {
        "connection_id": "fp1", "mt5_login": 123456,
        "equity": 0.0, "account_currency": "", "native_positions_verified": 0,
    })
    # The new signature takes native_positions + nonce, not a bool flag.
    with pytest.raises(TypeError):
        crm.certify_exposure("fp1", equity=10000.0, account_currency="USD",
                             native_positions_verified=True)


def test_invalid_nonce_rejected_fail_closed(monkeypatch):
    """A snapshot with a forged/invalid nonce cannot certify exposure."""
    _make_crm(monkeypatch, {
        "connection_id": "fp1", "mt5_login": 123456,
        "equity": 0.0, "account_currency": "", "native_positions_verified": 0,
    })
    with pytest.raises(CrmError):
        crm.certify_exposure(
            "fp1", equity=10000.0, account_currency="USD",
            native_positions=[], nonce="not-a-valid-nonce")


def test_non_list_positions_rejected(monkeypatch):
    """Passing a non-list (e.g. a bare bool) is rejected, not certified."""
    _make_crm(monkeypatch, {
        "connection_id": "fp1", "mt5_login": 123456,
        "equity": 0.0, "account_currency": "", "native_positions_verified": 0,
    })
    with pytest.raises(CrmError):
        crm.certify_exposure(
            "fp1", equity=10000.0, account_currency="USD",
            native_positions=True, nonce=make_nonce())


def test_non_positive_equity_rejected(monkeypatch):
    _make_crm(monkeypatch, {
        "connection_id": "fp1", "mt5_login": 123456,
        "equity": 0.0, "account_currency": "", "native_positions_verified": 0,
    })
    with pytest.raises(CrmError):
        crm.certify_exposure(
            "fp1", equity=0.0, account_currency="USD",
            native_positions=[], nonce=make_nonce())


def test_authenticated_snapshot_certifies_with_proof(monkeypatch):
    """A valid broker-native snapshot + nonce certifies and stores the proof."""
    nonce = make_nonce()
    positions = [
        {"symbol": "EURUSD.r", "direction": "BUY", "volume": 0.10,
         "entry": 1.10, "stop_loss": 1.09,
         "broker_native_stop_risk_usd": 10.0,
         "broker_native_ordercalcprofit_verified": True},
    ]
    crm_svc = _make_crm(monkeypatch, {
        "connection_id": "fp1", "mt5_login": 123456,
        "equity": 0.0, "account_currency": "", "native_positions_verified": 0,
    })
    result = crm_svc.certify_exposure(
        "fp1", equity=10000.0, account_currency="USD",
        native_positions=positions, nonce=nonce)

    assert result["native_positions_verified"] == 1
    assert result["equity"] == 10000.0
    assert result["exposure_nonce"] == nonce
    assert result["exposure_snapshot_count"] == 1
    assert result["exposure_snapshot_digest"] != ""
    assert result["exposure_certified_at"] != ""


def test_publisher_rejects_missing_snapshot(monkeypatch):
    """publish_exposure with no terminal snapshot raises, never certifies."""
    class _Mgr:
        def read_native_exposure(self, cid):
            return None

    monkeypatch.setattr(crm, "engine", types.SimpleNamespace(
        get_account=lambda cid: None, upsert_account=lambda a: None))
    with pytest.raises(CrmError):
        publish_exposure("fp1", manager=_Mgr())


def test_publisher_rejects_forged_nonce(monkeypatch):
    """A snapshot carrying a forged nonce is not authenticated -> rejected."""
    class _Mgr:
        def read_native_exposure(self, cid):
            return {"nonce": "forged-nonce", "equity": 10000.0,
                    "currency": "USD", "positions": []}

    monkeypatch.setattr(crm, "engine", types.SimpleNamespace(
        get_account=lambda cid: None, upsert_account=lambda a: None))
    with pytest.raises(CrmError):
        publish_exposure("fp1", manager=_Mgr())


def test_publisher_certifies_from_authenticated_snapshot(monkeypatch):
    """The full publisher path: valid snapshot -> certify_exposure -> certified."""
    nonce = make_nonce()
    snapshot = {
        "nonce": nonce, "equity": 10000.0, "currency": "USD",
        "positions": [
            {"symbol": "EURUSD.r", "direction": "BUY", "volume": 0.10,
             "entry": 1.10, "stop_loss": 1.09,
             "broker_native_stop_risk_usd": 10.0,
             "broker_native_ordercalcprofit_verified": True},
        ],
    }
    class _Mgr:
        def read_native_exposure(self, cid):
            return snapshot

    store = {
        "connection_id": "fp1", "mt5_login": 123456,
        "equity": 0.0, "account_currency": "", "native_positions_verified": 0,
    }
    monkeypatch.setattr(crm, "engine", types.SimpleNamespace(
        get_account=lambda cid: dict(store) if cid == "fp1" else None,
        upsert_account=lambda a: store.update(a)))

    result = publish_exposure("fp1", manager=_Mgr())
    assert result["native_positions_verified"] == 1
    assert result["exposure_nonce"] == nonce
    assert result["exposure_snapshot_count"] == 1
