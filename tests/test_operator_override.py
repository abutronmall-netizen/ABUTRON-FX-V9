"""Agentic V9 — operator dispatch override regression tests.

Covers the explicit, logged, scoped operator override that allows
dispatch past the DEMO_NOT_EFFECTIVE_ON and LIVE_NOT_AUTHORIZED policy
gates, while NOT bypassing market data errors or exposure certification.

All fixtures are synthetic; no real terminal, credentials or broker data.
"""
from __future__ import annotations

import json
import time

import pytest

from abutron.agent.operator_override import (OperatorOverride, OverrideError,
                                             load_override, revoke_override)
from abutron.agent.runtime import run_agentic_pass


CID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER_CID = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
BROKER = "native"
LOGIN = 7410512
SERVER = "DerivDemo"


def _identity():
    return {
        "connection_id": CID, "login": LOGIN, "broker": BROKER,
        "server": SERVER, "nonce": "N", "challenge": "c",
        "worker_health_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime(time.time())),
    }


def _bars_payload(n=3):
    bars = []
    for i in range(n):
        ot = f"2026-11-19T{9 + i // 12:02d}:{(5 + i) % 60:02d}:00Z"
        bars.append({
            "symbol": "EURUSD.r", "timeframe": "M5",
            "open_time": ot, "close_time": ot,
            "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15,
            "volume": 10.0, "bid": 0.0, "ask": 0.0, "spread": 0.0,
            "tick_size": 0.0, "point": 0.0, "closed": True,
        })
    return {"bars": bars}


def _quotes_payload():
    return {
        "quotes": [{
            "symbol": "EURUSD.r", "bid": 1.1001, "ask": 1.1003,
            "spread": 20, "tick_size": 0.00001, "point": 0.0001,
            "observed_at": "2026-11-19T10:00:00Z",
        }],
    }


_UNSET = object()


class _FakeMgr:
    def __init__(self, identity=None, bars=None, quotes=_UNSET):
        self._identity = identity or _identity()
        self._bars = bars if bars is not None else _bars_payload()
        self._quotes = _quotes_payload() if quotes is _UNSET else quotes

    def read_native_export_full(self, connection_id, since=None):
        return {
            "identity": self._identity, "bars": self._bars,
            "identity_file": "/tmp/i", "bars_file": "/tmp/b",
            "quotes": self._quotes,
        }

    def read_native_quotes(self, connection_id, since=None):
        return self._quotes


class _FakeDB:
    def __init__(self, account=None):
        from abutron.events import EventLog
        self._account = account or {
            "connection_id": CID, "broker": BROKER,
            "mt5_login": LOGIN, "server": SERVER,
            "owner_id": "owner1", "environment": "demo",
            "effective_on": 0, "equity": 10000.0,
        }
        # Clear any leftover events from previous runs.
        import os as _os
        _ev_path = "/tmp/abutron_override_test.jsonl"
        if _os.path.exists(_ev_path):
            _os.remove(_ev_path)
        self.event_log = EventLog(path=_ev_path)

    def get_account(self, connection_id):
        return self._account if connection_id == CID else None

    def get_live_intent(self, connection_id):
        return None

    def list_accounts(self, owner_id=None):
        # Return the account as uncertified so the risk engine fails closed.
        acct = dict(self._account)
        acct["equity"] = float(acct.get("equity", 0) or 0)
        acct["owner_risk_certified"] = False
        acct["cap_exceeded"] = False
        return [acct]


def _patch_db(monkeypatch, account):
    import abutron.agent.runtime as rt
    import abutron.db as abdb
    fake = _FakeDB(account)
    monkeypatch.setattr(rt, "engine_db", fake)
    # Also patch the real db so NativeDispatcher can find the account.
    monkeypatch.setattr(abdb, "engine_db", fake)
    return fake


class _StubEngine:
    def set_history(self, *a, **kw):
        pass

    def run_decision_pass(self, **kw):
        return []


class _Candidate:
    symbol = "EURUSD.r"
    direction = "BUY"
    conviction = 0.8
    reason_code = "all_invariants_passed"
    candidate_id = "cand-001"
    volume = 1.0
    stop_loss = 1.0990
    take_profit = 1.1030
    trigger_price = 1.1002
    price = 1.1002
    risk_fraction = 0.15



class _MockDispatcher:
    """Mock dispatcher that returns a simple success result."""

    def __init__(self, result=None):
        self._result = result or {
            "connection_id": CID, "sent": 1, "ack": 1,
            "unknown": 0, "rejected": 0, "details": [],
        }

    def dispatch_account(self, connection_id):
        return self._result


class _PositiveEngine(_StubEngine):
    def run_decision_pass(self, **kwargs):
        return [_Candidate()]


def _adapter(mgr=None):
    from abutron.agent.market_data import BrokerNativeMarketDataAdapter
    return BrokerNativeMarketDataAdapter(
        mgr or _FakeMgr(), connection_id=CID, broker=BROKER,
        login=LOGIN, server=SERVER)


# =========================================================================== #
# Override loading and validation
# =========================================================================== #

def test_load_override_from_file(tmp_path):
    config = {
        "authorized_by": "operator-1",
        "connections": [CID],
        "reason": "owner authorized DEMO dispatch",
    }
    p = tmp_path / "override.json"
    p.write_text(json.dumps(config))
    ov = load_override(config_path=str(p))
    assert ov is not None
    assert ov.authorized_by == "operator-1"
    assert CID in ov.connections
    assert ov.active is True


def test_load_override_from_env(monkeypatch):
    config = json.dumps({
        "authorized_by": "operator-1",
        "connections": [CID],
        "reason": "env override",
    })
    monkeypatch.setenv("ABUTRON_OPERATOR_OVERRIDE", config)
    ov = load_override()
    assert ov is not None
    assert ov.authorized_by == "operator-1"


def test_load_override_none_when_not_configured(monkeypatch):
    monkeypatch.delenv("ABUTRON_OPERATOR_OVERRIDE", raising=False)
    monkeypatch.delenv("ABUTRON_OPERATOR_OVERRIDE_FILE", raising=False)
    assert load_override() is None


def test_override_rejects_missing_authorized_by(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"connections": [CID], "reason": "r"}))
    with pytest.raises(OverrideError, match="authorized_by"):
        load_override(config_path=str(p))


def test_override_rejects_empty_connections(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({
        "authorized_by": "op", "connections": [], "reason": "r"}))
    with pytest.raises(OverrideError, match="connections"):
        load_override(config_path=str(p))


def test_override_rejects_missing_reason(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({
        "authorized_by": "op", "connections": [CID]}))
    with pytest.raises(OverrideError, match="reason"):
        load_override(config_path=str(p))


def test_override_rejects_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not valid json{")
    with pytest.raises(OverrideError, match="invalid JSON"):
        load_override(config_path=str(p))


def test_override_scoped_to_connections():
    ov = OperatorOverride(
        authorized_by="op", authorized_at=time.time(),
        connections={CID}, reason="test")
    assert ov.covers(CID) is True
    assert ov.covers(OTHER_CID) is False


def test_override_revoked_does_not_cover():
    ov = OperatorOverride(
        authorized_by="op", authorized_at=time.time(),
        connections={CID}, reason="test")
    revoke_override(ov)
    assert ov.covers(CID) is False


# =========================================================================== #
# Runtime gating with override
# =========================================================================== #

def test_override_bypasses_demo_not_effective_on(monkeypatch):
    """Override allows dispatch past DEMO_NOT_EFFECTIVE_ON gate."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 0, "equity": 10000.0,
    }
    fake = _patch_db(monkeypatch, acct)

    ov = OperatorOverride(
        authorized_by="owner", authorized_at=time.time(),
        connections={CID}, reason="owner activated DEMO dispatch")

    result = run_agentic_pass(
        CID, broker=BROKER, owner_id="owner1",
        engine=_PositiveEngine(), adapter=_adapter(),
        override=ov, dispatcher=_MockDispatcher())
    # Override bypassed the DEMO_NOT_EFFECTIVE_ON policy gate.
    assert result.dispatch_reason != "DEMO_NOT_EFFECTIVE_ON"


def test_override_does_not_bypass_other_connection(monkeypatch):
    """Override scoped to CID does NOT cover OTHER_CID."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 0, "equity": 10000.0,
    }
    _patch_db(monkeypatch, acct)

    ov = OperatorOverride(
        authorized_by="owner", authorized_at=time.time(),
        connections={OTHER_CID}, reason="wrong scope")

    result = run_agentic_pass(
        CID, broker=BROKER, owner_id="owner1",
        engine=_PositiveEngine(), adapter=_adapter(), override=ov)
    # Override doesn't cover CID — gate still blocks.
    assert result.dispatch_blocked is True
    assert result.dispatch_reason == "DEMO_NOT_EFFECTIVE_ON"


def test_override_bypasses_live_not_authorized(monkeypatch):
    """Override allows dispatch past LIVE_NOT_AUTHORIZED gate."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "live",
        "effective_on": 1, "equity": 10000.0,
    }
    _patch_db(monkeypatch, acct)

    ov = OperatorOverride(
        authorized_by="owner", authorized_at=time.time(),
        connections={CID}, reason="owner activated LIVE dispatch")

    result = run_agentic_pass(
        CID, broker=BROKER, owner_id="owner1",
        engine=_PositiveEngine(), adapter=_adapter(),
        override=ov, dispatcher=_MockDispatcher())
    # Override bypassed the LIVE_NOT_AUTHORIZED policy gate.
    assert result.dispatch_reason != "LIVE_NOT_AUTHORIZED"


def test_override_does_not_bypass_market_data_error(monkeypatch):
    """Override CANNOT bypass market data errors — non-negotiable."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 0, "equity": 10000.0,
    }
    _patch_db(monkeypatch, acct)

    class _EmptyMgr:
        def read_native_export_full(self, cid, since=None):
            return None

    from abutron.agent.market_data import BrokerNativeMarketDataAdapter
    adapter = BrokerNativeMarketDataAdapter(
        _EmptyMgr(), connection_id=CID, broker=BROKER,
        login=LOGIN, server=SERVER)

    ov = OperatorOverride(
        authorized_by="owner", authorized_at=time.time(),
        connections={CID}, reason="owner override")

    result = run_agentic_pass(
        CID, broker=BROKER, owner_id="owner1",
        engine=_StubEngine(), adapter=adapter, override=ov)
    # Market data error is NOT bypassable.
    assert result.decision_mode == "MARKET_DATA_ERROR"
    assert result.error != ""


def test_no_override_fails_closed(monkeypatch):
    """Without an override, the gate blocks (fail-closed default)."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 0, "equity": 10000.0,
    }
    _patch_db(monkeypatch, acct)
    monkeypatch.delenv("ABUTRON_OPERATOR_OVERRIDE", raising=False)

    result = run_agentic_pass(
        CID, broker=BROKER, owner_id="owner1",
        engine=_PositiveEngine(), adapter=_adapter())
    assert result.dispatch_blocked is True
    assert result.dispatch_reason == "DEMO_NOT_EFFECTIVE_ON"


def test_revoked_override_fails_closed(monkeypatch):
    """A revoked override (active=False) does not bypass."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 0, "equity": 10000.0,
    }
    _patch_db(monkeypatch, acct)

    ov = OperatorOverride(
        authorized_by="owner", authorized_at=time.time(),
        connections={CID}, reason="revoked")
    revoke_override(ov)

    result = run_agentic_pass(
        CID, broker=BROKER, owner_id="owner1",
        engine=_PositiveEngine(), adapter=_adapter(), override=ov)
    assert result.dispatch_blocked is True
    assert result.dispatch_reason == "DEMO_NOT_EFFECTIVE_ON"


def test_override_bypass_is_logged(monkeypatch):
    """Every override bypass must be logged with operator identity."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 0, "equity": 10000.0,
    }
    fake = _patch_db(monkeypatch, acct)

    ov = OperatorOverride(
        authorized_by="owner-bob", authorized_at=time.time(),
        connections={CID}, reason="test bypass log")

    run_agentic_pass(
        CID, broker=BROKER, owner_id="owner1",
        engine=_PositiveEngine(), adapter=_adapter(),
        override=ov, dispatcher=_MockDispatcher())

    # Check the event log for the bypass event.
    events = [json.loads(line) for line in open(fake.event_log.path)]
    bypass_events = [e for e in events
                     if e.get("event_type") == "agentic.override_bypass"]
    assert len(bypass_events) >= 1
    ev = bypass_events[0]
    assert ev["payload"]["authorized_by"] == "owner-bob"
    assert ev["payload"]["gate_bypassed"] == "DEMO_NOT_EFFECTIVE_ON"
    assert ev["payload"]["reason"] == "test bypass log"


def test_override_does_not_bypass_exposure_certification(monkeypatch):
    """Override bypasses the policy gate but NOT exposure certification.

    The override only bypasses DEMO_NOT_EFFECTIVE_ON / LIVE_NOT_AUTHORIZED.
    The exposure certification inside NativeDispatcher is a non-negotiable
    risk invariant that the override cannot remove."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 0, "equity": 10000.0,
    }
    _patch_db(monkeypatch, acct)

    ov = OperatorOverride(
        authorized_by="owner", authorized_at=time.time(),
        connections={CID}, reason="exposure test")

    # Mock dispatcher simulating the exposure gate blocking.
    blocked_dispatcher = _MockDispatcher(result={
        "connection_id": CID, "sent": 0, "ack": 0,
        "unknown": 0, "rejected": 0,
        "error": "BLOCKED_INCOMPLETE_EXPOSURE",
        "blocked": ["cand-001"],
        "details": [],
    })

    result = run_agentic_pass(
        CID, broker=BROKER, owner_id="owner1",
        engine=_PositiveEngine(), adapter=_adapter(),
        override=ov, dispatcher=blocked_dispatcher)
    # Policy gate was bypassed (not DEMO_NOT_EFFECTIVE_ON), but
    # exposure certification still blocked the dispatch.
    assert result.dispatch_reason != "DEMO_NOT_EFFECTIVE_ON"
    assert result.dispatch_reason == "BLOCKED_INCOMPLETE_EXPOSURE"
