"""Agentic V9 — decision-to-dispatch connection regression tests.

Verifies that brain-generated candidates are persisted into the
dispatch queue (RESERVED state) so NativeDispatcher can pick them up,
closing the gap where decisions were generated but never reached the
dispatch queue.

All fixtures are synthetic; no real terminal, credentials or broker data.
"""
from __future__ import annotations

import json
import time

import pytest

from abutron.agent.runtime import run_agentic_pass
from abutron.agent.operator_override import OperatorOverride
from abutron.apex.engine import Candidate


CID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
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
    """Mock engine_db that tracks persisted candidates."""

    def __init__(self, account=None):
        from abutron.events import EventLog
        self._account = account or {
            "connection_id": CID, "broker": BROKER,
            "mt5_login": LOGIN, "server": SERVER,
            "owner_id": "owner1", "environment": "demo",
            "effective_on": 1, "equity": 10000.0,
        }
        self._candidates: dict[str, dict] = {}
        import os as _os
        _ev_path = "/tmp/abutron_dispatch_test.jsonl"
        if _os.path.exists(_ev_path):
            _os.remove(_ev_path)
        self.event_log = EventLog(path=_ev_path)

    def get_account(self, connection_id):
        return self._account if connection_id == CID else None

    def get_live_intent(self, connection_id):
        return None

    def list_accounts(self, owner_id=None):
        acct = dict(self._account)
        acct["equity"] = float(acct.get("equity", 0) or 0)
        acct["owner_risk_certified"] = True
        acct["cap_exceeded"] = False
        return [acct]

    def add_candidate(self, cand):
        cid = cand.get("candidate_id", "")
        rk = cand.get("replay_key", "")
        if cid in self._candidates:
            return False
        if rk in {c.get("replay_key") for c in self._candidates.values()}:
            return False
        self._candidates[cid] = dict(cand)
        return True

    def get_candidate(self, candidate_id):
        return self._candidates.get(candidate_id)

    def set_candidate_state(self, candidate_id, state, reason=""):
        if candidate_id in self._candidates:
            self._candidates[candidate_id]["execution_state"] = state
            self._candidates[candidate_id]["reason_code"] = reason


def _patch_db(monkeypatch, account):
    import abutron.agent.runtime as rt
    import abutron.db as abdb
    fake = _FakeDB(account)
    monkeypatch.setattr(rt, "engine_db", fake)
    monkeypatch.setattr(abdb, "engine_db", fake)
    return fake


def _make_candidate(candidate_id="cand-001", symbol="EURUSD.r",
                    direction="BUY", replay_key="EURUSD.r:2026-11-19T10:00:00Z:BUY"):
    return Candidate(
        candidate_id=candidate_id,
        connection_id=CID,
        owner_id="owner1",
        broker=BROKER,
        symbol=symbol,
        timeframe="M5",
        bar_open="2026-11-19T10:00:00Z",
        bar_close="1.15",
        replay_key=replay_key,
        direction=direction,
        conviction=0.8,
        regime="trend",
        agent_votes={"signal_source": "brain", "reasoning_backend": "rule"},
        risk_fraction=0.15,
        stop_risk_usd=15.0,
        volume=1.0,
        stop_loss=1.0990 if direction == "BUY" else 1.1010,
        take_profit=1.1030 if direction == "BUY" else 1.0970,
        price=1.1002,
        entry_type="CONFIRM",
        trigger_price=1.1002,
        invalidation=1.0980,
        entry_quality=0.7,
        sniper_plan={},
        trail_k=2.0,
        trail_stop=1.0990,
        trail_plan={},
        reason_code="all_invariants_passed",
    )


class _CandidateEngine:
    """Engine stub that returns real Candidate objects."""

    def __init__(self, candidates):
        self._candidates = candidates
        self.history = {}

    def set_history(self, symbol, timeframe, candles):
        self.history[(symbol, timeframe)] = list(candles)

    def run_decision_pass(self, **kwargs):
        return self._candidates


class _HoldEngine:
    """Engine stub that returns no candidates (HOLD)."""

    def __init__(self):
        self.history = {}

    def set_history(self, symbol, timeframe, candles):
        self.history[(symbol, timeframe)] = list(candles)

    def run_decision_pass(self, **kwargs):
        return []


def _adapter(mgr=None):
    from abutron.agent.market_data import BrokerNativeMarketDataAdapter
    return BrokerNativeMarketDataAdapter(
        mgr or _FakeMgr(), connection_id=CID, broker=BROKER,
        login=LOGIN, server=SERVER)


class _MockDispatcher:
    def dispatch_account(self, connection_id, candidate_ids=None):
        return {
            "connection_id": connection_id, "sent": 1, "ack": 1,
            "unknown": 0, "rejected": 0, "details": [],
        }


def _native_spec(symbol):
    return {"symbol": symbol, "bid":1.1001, "ask":1.1003,
            "tick_age_ms":0, "tick_size":0.0001, "point":0.0001,
            "min_lots":0.01, "max_lots":10, "volume_step":0.01,
            "stops_level_points":2, "freeze_level_points":2,
            "pnl_probe_displacement_ticks":10,
            "buy_probe_native_ordercalcprofit_usd":-1.0,
            "sell_probe_native_ordercalcprofit_usd":-1.0,
            "broker_tick_value_loss":1.0}


def _run_native_reservation(*args, **kw):
    # Synthetic broker contract evidence only: no native writer attached.
    return run_agentic_pass(*args, native_specs={
        "EURUSD.r":_native_spec("EURUSD.r"),
        "GBPUSD.r":_native_spec("GBPUSD.r")},
        owner_open_risk_usd=0.0, owner_equity_usd=10000.0, **kw)


# =========================================================================== #
# Decision-to-dispatch connection tests
# =========================================================================== #

def test_candidates_persisted_as_reserved(monkeypatch):
    """Brain-generated candidates are persisted in RESERVED state."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 1, "equity": 10000.0,
    }
    fake = _patch_db(monkeypatch, acct)

    cands = [_make_candidate("cand-001")]
    engine = _CandidateEngine(cands)

    ov = OperatorOverride(
        authorized_by="owner", authorized_at=time.time(),
        connections={CID}, reason="test persistence")

    result = _run_native_reservation(
        CID, broker=BROKER, owner_id="owner1",
        engine=engine, adapter=_adapter(),
        override=ov, dispatcher=_MockDispatcher())

    # Candidate was persisted.
    assert "cand-001" in result.persisted_candidate_ids
    assert "cand-001" in fake._candidates

    # Persisted with RESERVED state.
    assert fake._candidates["cand-001"]["execution_state"] == "RESERVED"


def test_persisted_candidate_has_correct_fields(monkeypatch):
    """Persisted candidate row has all required dispatch fields."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 1, "equity": 10000.0,
    }
    fake = _patch_db(monkeypatch, acct)

    cand = _make_candidate("cand-002")
    engine = _CandidateEngine([cand])

    ov = OperatorOverride(
        authorized_by="owner", authorized_at=time.time(),
        connections={CID}, reason="test fields")

    _run_native_reservation(
        CID, broker=BROKER, owner_id="owner1",
        engine=engine, adapter=_adapter(),
        override=ov, dispatcher=_MockDispatcher())

    row = fake._candidates["cand-002"]
    assert row["symbol"] == "EURUSD.r"
    assert row["direction"] == "BUY"
    assert 0 < row["volume"] < 1.0
    assert row["stop_loss"] < row["price"]
    assert row["take_profit"] > row["price"]
    assert row["connection_id"] == CID
    assert row["owner_id"] == "owner1"
    assert row["replay_key"] == CID + ":EURUSD.r:2026-11-19T10:00:00Z:BUY"


def test_multiple_candidates_all_persisted(monkeypatch):
    """Multiple candidates from one decision pass are all persisted."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 1, "equity": 10000.0,
    }
    fake = _patch_db(monkeypatch, acct)

    cands = [
        _make_candidate("cand-001", replay_key="EURUSD.r:2026-11-19T10:00:00Z:BUY"),
        _make_candidate("cand-002", symbol="GBPUSD.r", direction="SELL",
                        replay_key="GBPUSD.r:2026-11-19T10:00:00Z:SELL"),
    ]
    engine = _CandidateEngine(cands)

    ov = OperatorOverride(
        authorized_by="owner", authorized_at=time.time(),
        connections={CID}, reason="test multiple")

    result = _run_native_reservation(
        CID, broker=BROKER, owner_id="owner1",
        engine=engine, adapter=_adapter(),
        override=ov, dispatcher=_MockDispatcher())

    assert len(result.persisted_candidate_ids) == 2
    assert "cand-001" in fake._candidates
    assert "cand-002" in fake._candidates


def test_duplicate_replay_key_skipped(monkeypatch):
    """Duplicate replay_key is skipped (at-most-once semantics)."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 1, "equity": 10000.0,
    }
    fake = _patch_db(monkeypatch, acct)

    # First candidate persisted.
    cand1 = _make_candidate("cand-001",
                            replay_key="EURUSD.r:2026-11-19T10:00:00Z:BUY")
    engine1 = _CandidateEngine([cand1])
    ov = OperatorOverride(
        authorized_by="owner", authorized_at=time.time(),
        connections={CID}, reason="test dup")

    _run_native_reservation(
        CID, broker=BROKER, owner_id="owner1",
        engine=engine1, adapter=_adapter(),
        override=ov, dispatcher=_MockDispatcher())

    # Second candidate with same replay_key — should be skipped.
    cand2 = _make_candidate("cand-002",
                            replay_key="EURUSD.r:2026-11-19T10:00:00Z:BUY")
    engine2 = _CandidateEngine([cand2])

    result2 = _run_native_reservation(
        CID, broker=BROKER, owner_id="owner1",
        engine=engine2, adapter=_adapter(),
        override=ov, dispatcher=_MockDispatcher())

    assert "cand-002" not in result2.persisted_candidate_ids
    assert "cand-002" not in fake._candidates


def test_hold_no_candidates_no_persistence(monkeypatch):
    """HOLD decision produces no candidates and no persistence."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 1, "equity": 10000.0,
    }
    fake = _patch_db(monkeypatch, acct)

    result = _run_native_reservation(
        CID, broker=BROKER, owner_id="owner1",
        engine=_HoldEngine(), adapter=_adapter(),
        override=OperatorOverride(
            authorized_by="owner", authorized_at=time.time(),
            connections={CID}, reason="hold test"),
        dispatcher=_MockDispatcher())

    assert result.candidates_generated == 0
    assert result.persisted_candidate_ids == []
    assert fake._candidates == {}


def test_persistence_logged(monkeypatch):
    """Candidate persistence is logged with candidate details."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 1, "equity": 10000.0,
    }
    fake = _patch_db(monkeypatch, acct)

    engine = _CandidateEngine([_make_candidate("cand-003")])
    ov = OperatorOverride(
        authorized_by="owner", authorized_at=time.time(),
        connections={CID}, reason="test log")

    _run_native_reservation(
        CID, broker=BROKER, owner_id="owner1",
        engine=engine, adapter=_adapter(),
        override=ov, dispatcher=_MockDispatcher())

    events = [json.loads(line) for line in open(fake.event_log.path)]
    persist_events = [e for e in events
                      if e.get("event_type") == "agentic.candidate_persisted"]
    assert len(persist_events) >= 1
    ev = persist_events[0]
    assert ev["payload"]["symbol"] == "EURUSD.r"
    assert ev["payload"]["direction"] == "BUY"
    assert ev["reason_code"] == "DECISION_TO_DISPATCH_CONNECTED"
