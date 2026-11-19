"""Agentic V9 — broker-native M5 market data adapter regression tests.

Covers the P0 implementation handoff: replace empty candles=[]/quotes=[]
with real broker-native, account/session-bound, fresh, closed M5 data,
and prove a real nonempty-input shadow decision pass.

All fixtures are synthetic; no real terminal, credentials or broker data.
"""
from __future__ import annotations

import json
import time

import pytest

from abutron.agent.market_data import (BrokerNativeMarketDataAdapter,
                                       MarketDataError, run_shadow_decision_pass)
from abutron.feeds.feed_quality import Candle, Quote
from abutron.feeds.mt5_terminal import Mt5TerminalManager


CID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BROKER = "fpmarkets"
LOGIN = 7410512
SERVER = "FPMarketsSC-Demo"
NONCE = "ABUTRON_NATIVE_7410512_1795080000"


def _identity():
    return {
        "connection_id": CID, "login": LOGIN, "broker": BROKER,
        "server": SERVER, "nonce": NONCE, "challenge": "chal-123",
        "worker_health_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime(time.time())),
    }


def _bars_payload(closed=True, n=3):
    bars = []
    base = 1795079700  # 2026-11-19T09:55:00Z
    for i in range(n):
        ot = f"2026-11-19T{9 + i // 12:02d}:{(5 + i) % 60:02d}:00Z"
        bars.append({
            "symbol": "EURUSD.r", "timeframe": "M5",
            "open_time": ot,
            "close_time": ot,
            "open": 1.10000, "high": 1.10050, "low": 1.09950,
            "close": 1.10020, "volume": 10.0,
            "bid": 1.10010, "ask": 1.10030, "spread": 0.0002,
            "tick_size": 0.00001, "point": 0.0001,
            "closed": closed,
        })
    return {"bars": bars}


class _FakeMgr:
    """Mock Mt5TerminalManager returning a canned native export."""

    def __init__(self, identity=None, bars=None, export_ts=None):
        self._identity = identity or _identity()
        self._bars = bars if bars is not None else _bars_payload()
        self._export_ts = export_ts if export_ts is not None else time.time()

    def read_native_export(self, connection_id, since=None):
        return {
            "identity": self._identity,
            "bars": self._bars,
            "identity_file": "/tmp/fake_identity.txt",
            "bars_file": "/tmp/fake_bars.txt",
        }


def _adapter(mgr=None, **kw):
    defaults = dict(connection_id=CID, broker=BROKER, login=LOGIN,
                    server=SERVER)
    defaults.update(kw)
    return BrokerNativeMarketDataAdapter(mgr or _FakeMgr(), **defaults)


# ------------------------------------------- success path
def test_adapter_reads_fresh_closed_m5_candles_and_quotes():
    md = _adapter().read()
    assert md.candle_count >= 1
    assert md.quote_count >= 1
    assert md.evaluated_symbols == ["EURUSD.r"]
    assert all(c.closed for c in md.candles)
    assert all(c.open > 0 and c.high > 0 and c.low > 0 and c.close > 0
               for c in md.candles)
    assert md.connection_id == CID
    assert md.broker == BROKER
    assert md.login == LOGIN
    assert md.server == SERVER
    assert md.source_hash != ""


def test_adapter_quotes_have_valid_bid_ask():
    md = _adapter().read()
    for q in md.quotes:
        assert q.bid > 0
        assert q.ask > 0
        assert q.ask >= q.bid
        assert q.spread > 0


# ------------------------------------------- rejection paths
def test_adapter_rejects_wrong_broker():
    mgr = _FakeMgr(identity={**_identity(), "broker": "wrong-broker"})
    with pytest.raises(MarketDataError, match="BROKER_MISMATCH"):
        _adapter(mgr).read()


def test_adapter_rejects_wrong_connection():
    mgr = _FakeMgr(identity={**_identity(),
                             "connection_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"})
    with pytest.raises(MarketDataError, match="CONNECTION_MISMATCH"):
        _adapter(mgr).read()


def test_adapter_rejects_wrong_login():
    mgr = _FakeMgr(identity={**_identity(), "login": LOGIN + 1})
    with pytest.raises(MarketDataError, match="LOGIN_MISMATCH"):
        _adapter(mgr).read()


def test_adapter_rejects_stale_export():
    # The identity heartbeat is in the past; since filters it out.
    stale_hb = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(time.time() - 3600))
    mgr = _FakeMgr(identity={**_identity(), "worker_health_ts": stale_hb})
    with pytest.raises(MarketDataError, match="STALE"):
        _adapter(mgr).read(since=time.time())


def test_adapter_rejects_open_candle():
    mgr = _FakeMgr(bars=_bars_payload(closed=False))
    with pytest.raises(MarketDataError, match="open"):
        _adapter(mgr).read()


def test_adapter_rejects_incomplete_ohlc():
    bars = _bars_payload()
    bars["bars"][0]["close"] = 0  # incomplete bar
    mgr = _FakeMgr(bars=bars)
    with pytest.raises(MarketDataError, match="incomplete"):
        _adapter(mgr).read()


def test_adapter_rejects_missing_quote():
    # All bars have zero bid/ask -> no quotes can be built.
    bars = _bars_payload()
    for b in bars["bars"]:
        b["bid"] = 0
        b["ask"] = 0
    mgr = _FakeMgr(bars=bars)
    with pytest.raises(MarketDataError, match="quote"):
        _adapter(mgr).read()


def test_adapter_rejects_insufficient_bars():
    mgr = _FakeMgr(bars=_bars_payload(n=1))
    adapter = _adapter(mgr, min_bars=5)
    with pytest.raises(MarketDataError, match="insufficient"):
        adapter.read()


def test_adapter_rejects_missing_export():
    class _EmptyMgr:
        def read_native_export(self, connection_id, since=None):
            return None
    with pytest.raises(MarketDataError, match="no native"):
        _adapter(_EmptyMgr()).read()


# ------------------------------------------- shadow decision pass
class _StubEngine:
    """Minimal engine stub that records set_history and returns no candidates."""

    def __init__(self):
        self.history = {}

    def set_history(self, symbol, timeframe, candles):
        self.history[(symbol, timeframe)] = list(candles)

    def run_decision_pass(self, *, connection_id, broker, owner_id,
                          candles, quotes, account, **kw):
        # Simulate the real engine: evaluate closed candles, return no
        # candidates (HOLD/reject — does not force BUY/SELL).
        self.evaluated = [c for c in candles if c.closed]
        return []


def test_shadow_decision_pass_uses_real_nonempty_candles():
    engine = _StubEngine()
    result = run_shadow_decision_pass(
        engine, _adapter(), connection_id=CID, broker=BROKER,
        owner_id="owner1", account={"equity": 10000.0})
    assert result["decision_mode"] == "BROKER_NATIVE_M5_SHADOW_PASS"
    assert result["evaluated_symbol_count"] >= 1
    assert result["candle_count"] >= 1
    assert result["quote_count"] >= 1
    assert result["candidates_generated"] == 0  # HOLD, no forced fill
    assert result["broker_native_signal_certified"] is False
    # The engine received real history.
    assert ("EURUSD.r", "M5") in engine.history
    assert len(engine.history[("EURUSD.r", "M5")]) >= 1


def test_shadow_decision_pass_preserves_hold_decision():
    """A model HOLD must produce zero candidates — no fabricated fills."""
    engine = _StubEngine()
    result = run_shadow_decision_pass(
        engine, _adapter(), connection_id=CID, broker=BROKER,
        owner_id="owner1", account={"equity": 10000.0})
    assert result["candidates"] == []
    assert result["candidates_generated"] == 0


def test_shadow_uses_latest_closed_candle_not_first_warmup_bar():
    engine = _StubEngine()
    payload = _bars_payload(n=3)
    # The real decision engine deduplicates symbols and evaluates the
    # FIRST candle it receives. Make input deliberately reverse-ordered.
    payload["bars"] = list(reversed(payload["bars"]))
    result = run_shadow_decision_pass(
        engine, _adapter(_FakeMgr(bars=payload)),
        connection_id=CID, broker=BROKER, owner_id="owner1",
        account={"equity": 10000.0})
    latest = max(b["open_time"] for b in payload["bars"])
    assert len(engine.evaluated) == 1
    assert engine.evaluated[0].open_time == latest
    assert len(engine.history[("EURUSD.r", "M5")]) == 3
    assert result["candidates_generated"] == 0
    assert result["broker_native_signal_certified"] is False


def test_wrong_server_rejected_even_with_matching_connection():
    ident = {**_identity(), "server": "FPMarketsSC-Live"}
    with pytest.raises(MarketDataError, match="server"):
        _adapter(_FakeMgr(identity=ident)).read()


def test_future_identity_timestamp_rejected():
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() + 3600))
    ident = {**_identity(), "worker_health_ts": future}
    with pytest.raises(MarketDataError, match="future"):
        _adapter(_FakeMgr(identity=ident)).read()


def test_shadow_candidate_does_not_imply_broker_fill_certification():
    class _Candidate:
        symbol = "EURUSD.r"
        direction = "BUY"
        conviction = 0.8
        reason_code = "synthetic-positive-qa-only"

    class _PositiveStub(_StubEngine):
        def run_decision_pass(self, **kwargs):
            self.evaluated = kwargs["candles"]
            return [_Candidate()]

    result = run_shadow_decision_pass(
        _PositiveStub(), _adapter(),
        connection_id=CID, broker=BROKER, owner_id="owner1",
        account={"equity": 10000.0})
    assert result["candidates_generated"] == 1
    assert result["broker_native_signal_certified"] is False
    assert result["shadow_decision_evaluated"] is True
