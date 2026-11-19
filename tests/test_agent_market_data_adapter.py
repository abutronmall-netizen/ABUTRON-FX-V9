"""Agentic V9 — broker-native M5 market data adapter regression tests.

Covers the P0 implementation handoff: replace empty candles=[]/quotes=[]
with real broker-native, account/session-bound, fresh, closed M5 data,
and prove a real nonempty-input shadow decision pass.

Tests cover both the JSON format (ApexNativeWriter) and the real
tab-delimited text format produced by the deployed Deriv MT5 writer.
All fixtures are synthetic; no real terminal, credentials or broker data.
"""
from __future__ import annotations

import json
import time

import pytest

from abutron.agent.market_data import (BrokerNativeMarketDataAdapter,
                                       MarketDataError, run_shadow_decision_pass)
from abutron.feeds.feed_quality import Candle, Quote


CID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BROKER = "native"
LOGIN = 7410512
SERVER = "DerivDemo"
NONCE = "ABUTRON_NATIVE_7410512_1795080000"

_UNSET = object()


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
            "bid": 0.0, "ask": 0.0, "spread": 0.0,
            "tick_size": 0.0, "point": 0.0,
            "closed": closed,
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


def _current_nonce():
    return f"ABUTRON_NATIVE_{LOGIN}_{int(time.time())}"


def _tab_delimited_bars(closed=True, n=3, nonce=None):
    """Real Deriv MT5 writer format: tab-delimited, no bid/ask columns."""
    now_epoch = int(time.time())
    lines = ["ABUTRON_NATIVE_BARS_V2", nonce or _current_nonce()]
    base = now_epoch - n * 300
    for i in range(n):
        epoch = base + i * 300
        lines.append(
            f"EURUSD.r\tM5\t{epoch}\t1.10000\t1.10050\t1.09950\t1.10020")
    return "\n".join(lines) + "\n"


def _text_identity(nonce=None):
    """Real Deriv MT5 writer 7-line identity format."""
    nonce = nonce or _current_nonce()
    return "\n".join([
        "ABUTRON_NATIVE_ACCOUNT_V2",
        nonce,
        str(LOGIN),
        "10000.00",
        "10050.00",
        "USD",
        "0",
    ]) + "\n"


class _FakeMgr:
    """Mock Mt5TerminalManager returning canned native exports."""

    def __init__(self, identity=None, bars=None, quotes=_UNSET,
                 identity_text=None, bars_text=None):
        self._identity = identity or _identity()
        self._bars = bars if bars is not None else _bars_payload()
        self._quotes = _quotes_payload() if quotes is _UNSET else quotes
        self._identity_text = identity_text
        self._bars_text = bars_text

    def read_native_export(self, connection_id, since=None):
        return {
            "identity": self._identity,
            "bars": self._bars,
            "identity_file": "/tmp/fake_identity.txt",
            "bars_file": "/tmp/fake_bars.txt",
        }

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


def _adapter(mgr=None, **kw):
    defaults = dict(connection_id=CID, broker=BROKER, login=LOGIN,
                    server=SERVER)
    defaults.update(kw)
    return BrokerNativeMarketDataAdapter(mgr or _FakeMgr(), **defaults)


# ------------------------------------------- success path (JSON format)
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


# ------------------------------------------- success path (tab-delimited)
def test_adapter_parses_real_tab_delimited_format(tmp_path):
    """The real Deriv MT5 writer exports tab-delimited OHLC, no JSON."""
    # Write real-format files to a temp export_dir.
    from abutron.feeds.mt5_terminal import Mt5TerminalManager
    import types

    # The real deployed legacy writer uses a stable opaque 32-character
    # nonce; all three export filenames must carry this exact nonce.
    nonce = "a" * 32
    bars_file = tmp_path / f"ABUTRON_NATIVE_BARS_V2_{nonce}.txt"
    ident_file = tmp_path / f"ABUTRON_NATIVE_ACCOUNT_V2_{nonce}.txt"
    quotes_file = tmp_path / f"ABUTRON_NATIVE_QUOTES_V2_{nonce}.txt"
    bars_file.write_text(_tab_delimited_bars(nonce=nonce))
    ident_file.write_text(_text_identity(nonce=nonce))
    quotes_file.write_text('{"quotes":[{"symbol":"EURUSD.r",'
                           '"bid":1.10010,"ask":1.10030,"spread":20,'
                           '"tick_size":0.00001,"point":0.0001,'
                           '"observed_at":"2026-11-19T10:00:00Z"}]}')

    # Build a fake terminal entry pointing at the temp dir.
    term = types.SimpleNamespace(export_dir=str(tmp_path),
                                 is_running=True,
                                 external_evidence_only=False)
    mgr = Mt5TerminalManager()
    mgr._terminals[CID] = term

    # The legacy source has no server field; the DEMO terminal registration
    # must still carry an independent required server binding.
    adapter = BrokerNativeMarketDataAdapter(
        mgr, connection_id=CID, broker=BROKER, login=LOGIN, server=SERVER)
    md = adapter.read()
    assert md.candle_count >= 1
    assert md.quote_count >= 1
    assert md.evaluated_symbols == ["EURUSD.r"]
    # The tab-delimited bars have no bid/ask; quotes come from the file.
    assert md.quotes[0].bid == 1.10010
    assert md.quotes[0].ask == 1.10030


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


def test_adapter_rejects_wrong_server():
    ident = {**_identity(), "server": "FPMarketsSC-Live"}
    with pytest.raises(MarketDataError, match="server"):
        _adapter(_FakeMgr(identity=ident)).read()


def test_adapter_rejects_stale_export():
    stale_hb = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(time.time() - 3600))
    mgr = _FakeMgr(identity={**_identity(), "worker_health_ts": stale_hb})
    with pytest.raises(MarketDataError, match="STALE"):
        _adapter(mgr).read(since=time.time())


def test_adapter_rejects_future_heartbeat():
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() + 3600))
    mgr = _FakeMgr(identity={**_identity(), "worker_health_ts": future})
    with pytest.raises(MarketDataError, match="future"):
        _adapter(mgr).read()


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


def test_adapter_rejects_missing_quote_snapshot():
    """No quote file — quotes must not be derived from OHLC."""
    mgr = _FakeMgr(quotes=None)
    with pytest.raises(MarketDataError, match="quote"):
        _adapter(mgr).read()


def test_adapter_rejects_missing_quote_symbol():
    """Quote snapshot doesn't cover a symbol that has candles."""
    mgr = _FakeMgr(quotes={"quotes": []})
    with pytest.raises(MarketDataError, match="missing"):
        _adapter(mgr).read()


def test_adapter_rejects_invalid_bid_ask_in_quote():
    quotes = {"quotes": [{"symbol": "EURUSD.r", "bid": 0, "ask": 0,
                          "spread": 0, "tick_size": 0, "point": 0}]}
    mgr = _FakeMgr(quotes=quotes)
    with pytest.raises(MarketDataError, match="invalid"):
        _adapter(mgr).read()


def test_adapter_rejects_insufficient_bars():
    mgr = _FakeMgr(bars=_bars_payload(n=1))
    adapter = _adapter(mgr, min_bars=5)
    with pytest.raises(MarketDataError, match="insufficient"):
        adapter.read()


def test_adapter_rejects_missing_export():
    class _EmptyMgr:
        def read_native_export_full(self, connection_id, since=None):
            return None
        def read_native_quotes(self, connection_id, since=None):
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


def test_shadow_candidate_does_not_imply_broker_fill_certification():
    class _Candidate:
        symbol = "EURUSD.r"
        direction = "BUY"
        conviction = 0.8
        reason_code = "synthetic-positive-qa-only"
        candidate_id = "qa-decision-id"
        price = 1.1001
        stop_loss = 1.0990
        take_profit = 1.1023
        volume = 0.6
        risk_fraction = 0.6

    class _PositiveStub(_StubEngine):
        def run_decision_pass(self, **kwargs):
            self.evaluated = kwargs["candles"]
            return [_Candidate()]

    result = run_shadow_decision_pass(
        _PositiveStub(), _adapter(),
        connection_id=CID, broker=BROKER, owner_id="owner1",
        account={"equity": 10000.0})
    assert result["candidates_generated"] == 1
    assert result["candidates"][0]["risk_fraction"] == 0.6
    assert result["candidates"][0]["stop_loss"] == 1.0990
    assert result["broker_native_signal_certified"] is False
    assert result["shadow_decision_evaluated"] is True


# ------------------------------------------- native text-format parser tests
class _NativeParserTests:
    """Tests for the real tab-delimited format parsers."""

    def test_parse_bars_file_tab_delimited(self, tmp_path):
        from abutron.feeds.mt5_terminal import Mt5TerminalManager
        p = tmp_path / "bars.txt"
        nonce = _current_nonce()
        p.write_text(_tab_delimited_bars(nonce=nonce))
        result = Mt5TerminalManager._parse_bars_file(str(p))
        assert result is not None
        assert "_nonce" in result
        assert result["_nonce"] == nonce
        assert len(result["bars"]) == 3
        bar = result["bars"][0]
        assert bar["symbol"] == "EURUSD.r"
        assert bar["timeframe"] == "M5"
        assert bar["closed"] is True
        assert bar["open"] == 1.10000
        assert bar["bid"] == 0.0  # no bid/ask in tab format

    def test_parse_bars_file_json(self, tmp_path):
        from abutron.feeds.mt5_terminal import Mt5TerminalManager
        p = tmp_path / "bars.json"
        p.write_text('{"bars":[{"symbol":"X","timeframe":"M5"}]}')
        result = Mt5TerminalManager._parse_bars_file(str(p))
        assert result is not None
        assert len(result["bars"]) == 1

    def test_parse_bars_file_truncated(self, tmp_path):
        from abutron.feeds.mt5_terminal import Mt5TerminalManager
        p = tmp_path / "bars.txt"
        p.write_text("ABUTRON_NATIVE_BARS_V2\n" + NONCE + "\n")
        result = Mt5TerminalManager._parse_bars_file(str(p))
        assert result is None

    def test_parse_bars_file_malformed_row(self, tmp_path):
        from abutron.feeds.mt5_terminal import Mt5TerminalManager
        content = "ABUTRON_NATIVE_BARS_V2\n" + NONCE + "\n"
        content += "EURUSD.r\tM5\t123\t1.1\n"  # only 4 fields, not 7
        content += "EURUSD.r\tM5\t123\t1.1\t1.2\t1.0\t1.15"
        p = tmp_path / "bars.txt"
        p.write_text(content)
        result = Mt5TerminalManager._parse_bars_file(str(p))
        assert result is not None
        assert len(result["bars"]) == 1  # malformed row skipped

    def test_parse_identity_file_text(self, tmp_path):
        from abutron.feeds.mt5_terminal import Mt5TerminalManager
        p = tmp_path / "id.txt"
        nonce = _current_nonce()
        p.write_text(_text_identity(nonce=nonce))
        result = Mt5TerminalManager._parse_identity_file(str(p))
        assert result is not None
        assert result["login"] == LOGIN
        assert result["nonce"] == nonce
        assert result["equity"] == 10050.0

    def test_parse_identity_file_json(self, tmp_path):
        from abutron.feeds.mt5_terminal import Mt5TerminalManager
        p = tmp_path / "id.json"
        p.write_text(json.dumps(_identity()))
        result = Mt5TerminalManager._parse_identity_file(str(p))
        assert result is not None
        assert result["connection_id"] == CID

    def test_nonce_mismatch_rejected(self, tmp_path):
        """Bars nonce must match identity nonce."""
        from abutron.feeds.mt5_terminal import Mt5TerminalManager
        import types as _types

        bars_file = tmp_path / "ABUTRON_NATIVE_BARS_V2_test.txt"
        ident_file = tmp_path / "ABUTRON_NATIVE_ACCOUNT_V2_test.txt"
        quotes_file = tmp_path / "ABUTRON_NATIVE_QUOTES_V2_test.txt"
        # Different nonces in bars vs identity.
        nonce = _current_nonce()
        bars_file.write_text(
            _tab_delimited_bars(nonce="DIFFERENT_NONCE"))
        ident_file.write_text(_text_identity(nonce=nonce))
        quotes_file.write_text(json.dumps(_quotes_payload()))

        term = _types.SimpleNamespace(export_dir=str(tmp_path),
                                     is_running=True,
                                     external_evidence_only=False)
        mgr = Mt5TerminalManager()
        mgr._terminals[CID] = term
        export = mgr.read_native_export(CID)
        assert export is None  # nonce mismatch



def test_real_opaque_nonce_never_reinterpreted_as_timestamp(tmp_path):
    from abutron.feeds.mt5_terminal import Mt5TerminalManager
    nonce = "e" * 32
    p = tmp_path / "ABUTRON_NATIVE_ACCOUNT_V2_x.txt"
    p.write_text(_text_identity(nonce=nonce))
    parsed = Mt5TerminalManager._parse_identity_file(str(p))
    assert parsed["nonce"] == nonce
    assert parsed["connection_id"] == ""
    assert parsed["_legacy_text_unattested"] is True
    assert parsed["worker_health_ts"].endswith("Z")


def test_native_quote_spread_is_a_price_distance_not_points():
    q = _adapter().read().quotes[0]
    assert q.spread == pytest.approx(q.ask - q.bid)
    assert q.spread_in_points() < 100


@pytest.mark.parametrize("bad_bid,bad_ask", [
    (float("nan"), 1.01), (1.05, 1.01),
    (float("inf"), float("inf")),
])
def test_quote_rejects_nan_inf_crossed_market(bad_bid, bad_ask):
    q = _quotes_payload()
    q["quotes"][0].update(bid=bad_bid, ask=bad_ask)
    with pytest.raises(MarketDataError, match="invalid bid/ask"):
        _adapter(_FakeMgr(quotes=q)).read()


def test_native_files_must_share_same_nonce_across_three_sources(tmp_path):
    from abutron.feeds.mt5_terminal import Mt5TerminalManager
    from types import SimpleNamespace
    nonce = "f" * 32
    idp = tmp_path / f"ABUTRON_NATIVE_ACCOUNT_V2_{nonce}.txt"
    barp = tmp_path / f"ABUTRON_NATIVE_BARS_V2_{nonce}.txt"
    qp = tmp_path / f"ABUTRON_NATIVE_QUOTES_V2_{nonce}.txt"
    idp.write_text(_text_identity(nonce=nonce))
    barp.write_text(_tab_delimited_bars(nonce=nonce))
    qp.write_text(json.dumps(_quotes_payload()))
    m = Mt5TerminalManager()
    m._terminals[CID] = SimpleNamespace(
        export_dir=str(tmp_path), is_running=True,
        external_evidence_only=False)
    assert m.read_native_export_full(CID) is not None
    qp.rename(tmp_path / f"ABUTRON_NATIVE_QUOTES_V2_{'b'*32}.txt")
    assert m.read_native_export_full(CID) is None
