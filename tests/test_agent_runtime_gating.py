"""Agentic V9 — DEMO-gated runtime and order lifecycle regression tests.

Covers the deployed runtime replacement: real broker-native data →
brain → gated dispatch, plus the independent DEMO order lifecycle
verification (OrderCheck → ACK → SL/TP read-back → stop amendment →
OrderCalcProfit).

All fixtures are synthetic; no real terminal, credentials or broker data.
"""
from __future__ import annotations

import time

import pytest

from abutron.agent.runtime import (AgenticRuntimeResult, run_agentic_pass)
from abutron.agent.market_data import (BrokerNativeMarketDataAdapter,
                                       MarketDataError)
from abutron.execution.lifecycle import (LifecycleError,
                                          verify_broker_ack,
                                          verify_full_demo_lifecycle,
                                          verify_order_calc_profit,
                                          verify_order_check,
                                          verify_sltp_readback,
                                          verify_stop_amendment)


CID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BROKER = "native"
LOGIN = 7410512
SERVER = "DerivDemo"
NONCE = "ABUTRON_NATIVE_7410512_1795080000"


def _identity():
    return {
        "connection_id": CID, "login": LOGIN, "broker": BROKER,
        "server": SERVER, "nonce": NONCE, "challenge": "chal-123",
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
            "open": 1.10000, "high": 1.10050, "low": 1.09950,
            "close": 1.10020, "volume": 10.0,
            "bid": 0.0, "ask": 0.0, "spread": 0.0,
            "tick_size": 0.0, "point": 0.0,
            "closed": True,
        })
    return {"bars": bars}


def _quotes_payload():
    return {
        "quotes": [{
            "symbol": "EURUSD.r", "bid": 1.10010, "ask": 1.10030,
            "spread": 20, "tick_size": 0.00001, "point": 0.0001,
            "observed_at": "2026-11-19T10:00:00Z",
        }],
    }


_UNSET = object()


class _FakeMgr:
    """Mock Mt5TerminalManager returning canned native exports."""

    def __init__(self, identity=None, bars=None, quotes=_UNSET):
        self._identity = identity or _identity()
        self._bars = bars if bars is not None else _bars_payload()
        self._quotes = _quotes_payload() if quotes is _UNSET else quotes

    def read_native_export_full(self, connection_id, since=None):
        return {
            "identity": self._identity,
            "bars": self._bars,
            "identity_file": "/tmp/fake_identity.txt",
            "bars_file": "/tmp/fake_bars.txt",
            "quotes": self._quotes,
        }

    def read_native_quotes(self, connection_id, since=None):
        return self._quotes


def _adapter(mgr=None):
    return BrokerNativeMarketDataAdapter(
        mgr or _FakeMgr(), connection_id=CID, broker=BROKER,
        login=LOGIN, server=SERVER)


class _StubEngine:
    """Engine stub: returns no candidates (HOLD)."""

    def __init__(self):
        self.history = {}

    def set_history(self, symbol, timeframe, candles):
        self.history[(symbol, timeframe)] = list(candles)

    def run_decision_pass(self, *, connection_id, broker, owner_id,
                          candles, quotes, account, **kw):
        self.evaluated = [c for c in candles if c.closed]
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


class _PositiveEngine(_StubEngine):
    def run_decision_pass(self, **kwargs):
        self.evaluated = kwargs["candles"]
        return [_Candidate()]


# =========================================================================== #
# Runtime gating tests
# =========================================================================== #

class _FakeDB:
    """Mock engine_db for runtime tests."""

    def __init__(self, account=None):
        from abutron.events import EventLog
        self._account = account or {
            "connection_id": CID, "broker": BROKER,
            "mt5_login": LOGIN, "server": SERVER,
            "owner_id": "owner1", "environment": "demo",
            "effective_on": 1, "equity": 10000.0,
        }
        self.event_log = EventLog(path="/tmp/abutron_runtime_test_audit.jsonl")

    def get_account(self, connection_id):
        return self._account if connection_id == CID else None

    def get_live_intent(self, connection_id):
        return None


def _patch_db(monkeypatch, account):
    import abutron.agent.runtime as rt
    fake = _FakeDB(account)
    monkeypatch.setattr(rt, "engine_db", fake)
    return fake


def test_runtime_demo_effective_on_runs_shadow_pass(monkeypatch):
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 1, "equity": 10000.0,
    }
    _patch_db(monkeypatch, acct)

    # No candidates → no dispatch, but shadow pass ran.
    result = run_agentic_pass(
        CID, broker=BROKER, owner_id="owner1",
        engine=_StubEngine(), adapter=_adapter())
    assert result.decision_mode == "BROKER_NATIVE_M5_SHADOW_PASS"
    assert result.shadow_decision_evaluated is True
    assert result.candle_count >= 1
    assert result.candidates_generated == 0
    assert result.broker_native_signal_certified is False


def test_runtime_demo_not_effective_on_blocks_dispatch(monkeypatch):
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 0, "equity": 10000.0,
    }
    _patch_db(monkeypatch, acct)

    # Candidate produced but DEMO not effective_on → dispatch blocked.
    result = run_agentic_pass(
        CID, broker=BROKER, owner_id="owner1",
        engine=_PositiveEngine(), adapter=_adapter())
    assert result.candidates_generated == 1
    assert result.dispatch_blocked is True
    assert result.dispatch_reason == "DEMO_NOT_EFFECTIVE_ON"


def test_runtime_live_not_authorized_blocks_dispatch(monkeypatch):
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "live",
        "effective_on": 1, "equity": 10000.0,
    }
    _patch_db(monkeypatch, acct)

    # LIVE effective_on=1 but no live intent → dispatch blocked.
    result = run_agentic_pass(
        CID, broker=BROKER, owner_id="owner1",
        engine=_PositiveEngine(), adapter=_adapter())
    assert result.candidates_generated == 1
    assert result.dispatch_blocked is True
    assert result.dispatch_reason == "LIVE_NOT_AUTHORIZED"


def test_runtime_market_data_error_fails_closed(monkeypatch):
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 1, "equity": 10000.0,
    }
    _patch_db(monkeypatch, acct)

    class _EmptyMgr:
        def read_native_export_full(self, cid, since=None):
            return None

    adapter = BrokerNativeMarketDataAdapter(
        _EmptyMgr(), connection_id=CID, broker=BROKER,
        login=LOGIN, server=SERVER)
    result = run_agentic_pass(
        CID, broker=BROKER, owner_id="owner1",
        engine=_StubEngine(), adapter=adapter)
    assert result.decision_mode == "MARKET_DATA_ERROR"
    assert result.error != ""


def test_runtime_hold_no_dispatch(monkeypatch):
    """HOLD/reject → no dispatch, even if effective_on."""
    acct = {
        "connection_id": CID, "broker": BROKER, "mt5_login": LOGIN,
        "server": SERVER, "owner_id": "owner1", "environment": "demo",
        "effective_on": 1, "equity": 10000.0,
    }
    _patch_db(monkeypatch, acct)
    result = run_agentic_pass(
        CID, broker=BROKER, owner_id="owner1",
        engine=_StubEngine(), adapter=_adapter())
    assert result.candidates_generated == 0
    assert result.dispatch_blocked is False


# =========================================================================== #
# Lifecycle verification tests
# =========================================================================== #

# --- OrderCheck ---

def test_order_check_verified():
    r = verify_order_check(
        connection_id=CID, candidate_id="c1", symbol="EURUSD.r",
        direction="BUY", volume=1.0, stop_loss=1.0990, take_profit=1.1030,
        broker_order_check={
            "retcode": 0, "symbol": "EURUSD.r",
            "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        })
    assert r.verified is True
    assert r.reason_code == "ORDER_CHECK_VERIFIED"


def test_order_check_rejected_retcode():
    with pytest.raises(LifecycleError, match="retcode"):
        verify_order_check(
            connection_id=CID, candidate_id="c1", symbol="EURUSD.r",
            direction="BUY", volume=1.0, stop_loss=1.099, take_profit=1.103,
            broker_order_check={"retcode": -1})


def test_order_check_rejected_volume_below_min():
    with pytest.raises(LifecycleError, match="minimum"):
        verify_order_check(
            connection_id=CID, candidate_id="c1", symbol="EURUSD.r",
            direction="BUY", volume=0.001, stop_loss=1.099, take_profit=1.103,
            broker_order_check={
                "retcode": 0, "volume_min": 0.01, "volume_max": 100.0,
                "volume_step": 0.01})


def test_order_check_rejected_volume_step():
    with pytest.raises(LifecycleError, match="multiple"):
        verify_order_check(
            connection_id=CID, candidate_id="c1", symbol="EURUSD.r",
            direction="BUY", volume=1.005, stop_loss=1.099, take_profit=1.103,
            broker_order_check={
                "retcode": 0, "volume_min": 0.01, "volume_max": 100.0,
                "volume_step": 0.01})


def test_order_check_rejected_symbol_mismatch():
    with pytest.raises(LifecycleError, match="symbol"):
        verify_order_check(
            connection_id=CID, candidate_id="c1", symbol="EURUSD.r",
            direction="BUY", volume=1.0, stop_loss=1.099, take_profit=1.103,
            broker_order_check={
                "retcode": 0, "symbol": "GBPUSD.r",
                "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01})


# --- Broker ACK ---

def test_broker_ack_verified():
    r = verify_broker_ack(
        connection_id=CID, candidate_id="c1",
        broker_ack={
            "candidate_id": "c1", "deal_id": "DEAL123",
            "connection_id": CID, "activation_revision": "rev-1",
        }, expected_revision="rev-1")
    assert r.verified is True
    assert r.reason_code == "BROKER_ACK_RECONCILED"


def test_broker_ack_rejected_candidate_mismatch():
    with pytest.raises(LifecycleError, match="candidate"):
        verify_broker_ack(
            connection_id=CID, candidate_id="c1",
            broker_ack={"candidate_id": "c2", "deal_id": "D1"})


def test_broker_ack_rejected_revision_mismatch():
    """Historical match cannot be certified as a new revision."""
    with pytest.raises(LifecycleError, match="revision"):
        verify_broker_ack(
            connection_id=CID, candidate_id="c1",
            broker_ack={
                "candidate_id": "c1", "deal_id": "D1",
                "activation_revision": "old-rev",
            }, expected_revision="new-rev")


def test_broker_ack_rejected_missing_deal():
    with pytest.raises(LifecycleError, match="deal"):
        verify_broker_ack(
            connection_id=CID, candidate_id="c1",
            broker_ack={"candidate_id": "c1"})


# --- SL/TP read-back ---

def test_sltp_readback_verified():
    r = verify_sltp_readback(
        connection_id=CID, candidate_id="c1",
        expected_stop_loss=1.0990, expected_take_profit=1.1030,
        broker_position={"stop_loss": 1.0990, "take_profit": 1.1030})
    assert r.verified is True
    assert r.reason_code == "SLTP_READBACK_VERIFIED"


def test_sltp_readback_rejected_sl_mismatch():
    with pytest.raises(LifecycleError, match="SL"):
        verify_sltp_readback(
            connection_id=CID, candidate_id="c1",
            expected_stop_loss=1.0990, expected_take_profit=1.1030,
            broker_position={"stop_loss": 1.0980, "take_profit": 1.1030})


def test_sltp_readback_rejected_missing_sl():
    with pytest.raises(LifecycleError, match="stop_loss"):
        verify_sltp_readback(
            connection_id=CID, candidate_id="c1",
            expected_stop_loss=1.0990, expected_take_profit=1.1030,
            broker_position={"stop_loss": 0, "take_profit": 1.1030})


# --- Stop amendment ---

def test_stop_amendment_verified():
    r = verify_stop_amendment(
        connection_id=CID, candidate_id="c1",
        previous_stop=1.0980, desired_stop=1.0990, direction="BUY",
        broker_amendment={"retcode": 0, "stop_loss": 1.0990})
    assert r.verified is True
    assert r.reason_code == "STOP_AMENDMENT_VERIFIED"


def test_stop_amendment_rejected_retcode():
    with pytest.raises(LifecycleError, match="retcode"):
        verify_stop_amendment(
            connection_id=CID, candidate_id="c1",
            previous_stop=1.0980, desired_stop=1.0990, direction="BUY",
            broker_amendment={"retcode": -1, "stop_loss": 1.0990})


def test_stop_amendment_rejected_readback_mismatch():
    with pytest.raises(LifecycleError, match="read-back"):
        verify_stop_amendment(
            connection_id=CID, candidate_id="c1",
            previous_stop=1.0980, desired_stop=1.0990, direction="BUY",
            broker_amendment={"retcode": 0, "stop_loss": 1.0970})


def test_stop_amendment_rejected_non_monotonic_buy():
    """BUY stop must tighten (increase), never loosen."""
    with pytest.raises(LifecycleError, match="non-monotonic"):
        verify_stop_amendment(
            connection_id=CID, candidate_id="c1",
            previous_stop=1.0990, desired_stop=1.0980, direction="BUY",
            broker_amendment={"retcode": 0, "stop_loss": 1.0980})


def test_stop_amendment_rejected_non_monotonic_sell():
    """SELL stop must tighten (decrease), never loosen."""
    with pytest.raises(LifecycleError, match="non-monotonic"):
        verify_stop_amendment(
            connection_id=CID, candidate_id="c1",
            previous_stop=1.1010, desired_stop=1.1020, direction="SELL",
            broker_amendment={"retcode": 0, "stop_loss": 1.1020})


# --- OrderCalcProfit ---

def test_order_calc_profit_verified():
    r = verify_order_calc_profit(
        connection_id=CID, candidate_id="c1", symbol="EURUSD.r",
        direction="BUY", volume=1.0, entry_price=1.1002,
        stop_loss=1.0990, take_profit=1.1030,
        broker_calc={
            "symbol": "EURUSD.r", "volume": 1.0,
            "profit_at_stop_loss": -10.0, "profit_at_take_profit": 30.0,
        })
    assert r.verified is True
    assert r.reason_code == "ORDER_CALC_PROFIT_VERIFIED"
    assert r.details["risk_reward"] == 3.0


def test_order_calc_profit_rejected_rr_below_invariant():
    """R:R must be >= 1.50."""
    with pytest.raises(LifecycleError, match="1.50"):
        verify_order_calc_profit(
            connection_id=CID, candidate_id="c1", symbol="EURUSD.r",
            direction="BUY", volume=1.0, entry_price=1.1002,
            stop_loss=1.0990, take_profit=1.1015,
            broker_calc={
                "symbol": "EURUSD.r", "volume": 1.0,
                "profit_at_stop_loss": -20.0, "profit_at_take_profit": 25.0,
            })


def test_order_calc_profit_rejected_sl_not_negative():
    with pytest.raises(LifecycleError, match="negative"):
        verify_order_calc_profit(
            connection_id=CID, candidate_id="c1", symbol="EURUSD.r",
            direction="BUY", volume=1.0, entry_price=1.1002,
            stop_loss=1.0990, take_profit=1.1030,
            broker_calc={
                "symbol": "EURUSD.r", "volume": 1.0,
                "profit_at_stop_loss": 5.0, "profit_at_take_profit": 30.0,
            })


def test_order_calc_profit_rejected_volume_mismatch():
    with pytest.raises(LifecycleError, match="volume"):
        verify_order_calc_profit(
            connection_id=CID, candidate_id="c1", symbol="EURUSD.r",
            direction="BUY", volume=1.0, entry_price=1.1002,
            stop_loss=1.0990, take_profit=1.1030,
            broker_calc={
                "symbol": "EURUSD.r", "volume": 2.0,
                "profit_at_stop_loss": -10.0, "profit_at_take_profit": 30.0,
            })


# --- Full lifecycle ---

def test_full_demo_lifecycle_verified():
    results = verify_full_demo_lifecycle(
        connection_id=CID, candidate_id="c1", symbol="EURUSD.r",
        direction="BUY", volume=1.0, stop_loss=1.0990,
        take_profit=1.1030, entry_price=1.1002,
        broker_order_check={
            "retcode": 0, "symbol": "EURUSD.r",
            "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        },
        broker_ack={
            "candidate_id": "c1", "deal_id": "D1",
            "connection_id": CID, "activation_revision": "rev-1",
        },
        broker_position={"stop_loss": 1.0990, "take_profit": 1.1030},
        broker_calc={
            "symbol": "EURUSD.r", "volume": 1.0,
            "profit_at_stop_loss": -10.0, "profit_at_take_profit": 30.0,
        },
        expected_revision="rev-1")
    assert len(results) == 4
    for stage, r in results.items():
        assert r.verified is True, f"{stage} not verified: {r.reason_code}"


def test_full_demo_lifecycle_fails_closed_on_ack_revision():
    """Fail-closed: a historical ACK revision breaks the full lifecycle."""
    with pytest.raises(LifecycleError, match="revision"):
        verify_full_demo_lifecycle(
            connection_id=CID, candidate_id="c1", symbol="EURUSD.r",
            direction="BUY", volume=1.0, stop_loss=1.0990,
            take_profit=1.1030, entry_price=1.1002,
            broker_order_check={
                "retcode": 0, "symbol": "EURUSD.r",
                "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
            },
            broker_ack={
                "candidate_id": "c1", "deal_id": "D1",
                "activation_revision": "old-rev",
            },
            broker_position={"stop_loss": 1.0990, "take_profit": 1.1030},
            broker_calc={
                "symbol": "EURUSD.r", "volume": 1.0,
                "profit_at_stop_loss": -10.0, "profit_at_take_profit": 30.0,
            },
            expected_revision="new-rev")
