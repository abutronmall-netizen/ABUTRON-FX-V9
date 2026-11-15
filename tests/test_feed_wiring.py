"""Feed → decision wiring regression tests.

These cover the transport defects that made the live engine a no-op even
though every component passed its own unit tests:

1. `Mt5TerminalManager.read_native_export` returned the raw identity blob, so
   the EA's envelope fields (connection_id / login / broker / nonce /
   challenge / worker_health_ts) were never visible to the feed-quality
   contract and every export failed validation.
2. `normalize_native_bars` only accepted a `{"bars": [...]}` mapping, but the
   reader normalises the bars file to a list — an AttributeError escaped the
   engine tick loop.
3. `ClockOffsetEstimator` compared the last *closed* bar's close-time to
   wall-clock now, which is 0–5 minutes by construction, so a perfectly
   aligned broker clock was reported BROKER_CLOCK_OFFSET_UNVERIFIED forever.
4. Account equity was never ingested, so the candidate contract could not
   size a position (BROKER_EQUITY_OR_OPEN_RISK_UNVERIFIED).
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.feeds import mt5_terminal as MT
from abutron.feeds.broker_feeds import get_feed
from abutron.feeds.clock_offset import ClockOffsetEstimator
from abutron.feeds.feed_quality import Candle, normalize_native_bars
from abutron.events import EventLog
from abutron.config import DATA_ROOT, utc_now


def _ident(cid="c1", login=6207, **kw):
    d = {"connection_id": cid, "login": login, "broker": "deriv",
         "server": "Deriv-Demo", "nonce": "n1", "challenge": "ch1",
         "worker_health_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    d.update(kw)
    return d


def _bars(n=130, end=None, tf=300):
    end = end or (int(time.time()) // tf) * tf
    out = []
    for i in range(n):
        t = end - (n - 1 - i) * tf
        o = 100.0 + i * 0.05
        c = o + 0.10
        out.append({
            "symbol": "Volatility 100 Index.0", "timeframe": "M5",
            "open_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t - tf)),
            "close_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
            "open": o, "high": c + 0.05, "low": o - 0.05, "close": c,
            "volume": 10.0, "bid": c - 0.01, "ask": c + 0.01, "spread": 2.0,
            "tick_size": 0.01, "point": 0.01, "closed": True,
        })
    return out


class _Term:
    def __init__(self, export_dir):
        self.is_running = True
        self.broker = "deriv"
        self.login = 6207
        self.export_dir = export_dir
        self.started_at = time.time()
        self.health_ts = ""


_seq = __import__("itertools").count()


@pytest.fixture
def native_export(tmp_path):
    """Write a native export exactly as the EA does and register a terminal.

    Each invocation gets a unique connection id: the engine DB is shared for
    the whole test session, so a fixed id would leak account rows between
    tests.
    """
    cid = f"conn-test-{next(_seq)}"
    ident = _ident(cid, 6207, equity=10000.0, balance=10000.0)
    (tmp_path / "ABUTRON_NATIVE_ACCOUNT_V2_a.txt").write_text(json.dumps(ident))
    (tmp_path / "ABUTRON_NATIVE_BARS_V2_a.txt").write_text(
        json.dumps({"bars": _bars()}))
    MT.terminal_manager._terminals[cid] = _Term(str(tmp_path))
    try:
        yield cid, ident
    finally:
        MT.terminal_manager._terminals.pop(cid, None)


# ---------------------------------------------------------------- reader ---
def test_reader_lifts_envelope_fields(native_export):
    cid, ident = native_export
    raw = MT.terminal_manager.read_native_export(cid)
    assert raw is not None
    # The envelope fields the feed-quality contract validates must be
    # present at the top level, not buried inside `identity`.
    for key in ("connection_id", "login", "broker", "nonce", "challenge",
                "worker_health_ts"):
        assert raw[key] == ident[key]
    assert isinstance(raw["bars"], list)
    assert len(raw["bars"]) == 130


def test_reader_accepts_bar_file_as_bare_list(tmp_path):
    cid = "conn-test-2"
    (tmp_path / "ABUTRON_NATIVE_ACCOUNT_V2_a.txt").write_text(
        json.dumps(_ident(cid)))
    (tmp_path / "ABUTRON_NATIVE_BARS_V2_a.txt").write_text(
        json.dumps(_bars(3)))
    MT.terminal_manager._terminals[cid] = _Term(str(tmp_path))
    try:
        raw = MT.terminal_manager.read_native_export(cid)
        assert raw is not None and len(raw["bars"]) == 3
    finally:
        MT.terminal_manager._terminals.pop(cid, None)


# ---------------------------------------------------------- normalisation ---
def test_normalize_accepts_envelope_and_list():
    bars = _bars(5)
    a = normalize_native_bars({"bars": bars}, connection_id="c1", login=1,
                              broker="deriv")
    b = normalize_native_bars(bars, connection_id="c1", login=1,
                              broker="deriv")
    assert len(a) == len(b) == 5


def test_normalize_skips_malformed_rows():
    bars = _bars(3)
    bars[1] = {"symbol": "X"}  # missing OHLC -> must be skipped, not raised
    out = normalize_native_bars(bars, connection_id="c1", login=1,
                               broker="deriv")
    assert len(out) == 2


def test_normalize_rejects_unknown_payload():
    assert normalize_native_bars("nonsense", connection_id="c1", login=1,
                                 broker="deriv") == []


# ------------------------------------------------------------ feed read ---
def test_feed_accepts_aligned_export(native_export):
    cid, _ = native_export
    feed = get_feed("deriv", EventLog(os.path.join(DATA_ROOT, "t_feed.jsonl")))
    res = feed.read(cid, 6207, ["Volatility 100 Index.0"])
    assert res.accepted, f"violations={res.violations}"
    assert len(res.candles) == 130
    assert res.clock_verified


def test_feed_persists_attested_equity(native_export):
    """The candidate contract sizes from broker-attested equity; the feed
    must persist it or every candidate is blocked."""
    from abutron.crm.controls import crm
    from abutron.db import engine_db

    cid, _ = native_export
    crm.register_account(connection_id=cid, owner_id="o1", broker="deriv",
                         mt5_login=6207, display_name="T",
                         environment="demo", server="Deriv-Demo",
                         symbols=["Volatility 100 Index.0"])
    assert float(engine_db.get_account(cid).get("equity") or 0.0) == 0.0

    feed = get_feed("deriv", EventLog(os.path.join(DATA_ROOT, "t_feed2.jsonl")))
    feed.read(cid, 6207, ["Volatility 100 Index.0"])
    row = engine_db.get_account(cid)
    assert float(row["equity"]) == 10000.0
    assert float(row["balance"]) == 10000.0


# --------------------------------------------------------------- clock ---
def _candle(end_ts, tf="M5"):
    return Candle(
        symbol="X", timeframe=tf,
        open_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end_ts - 300)),
        close_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end_ts)),
        open=1.0, high=1.1, low=0.9, close=1.0, volume=1.0,
        bid=1.0, ask=1.01, spread=0.01, tick_size=0.01, point=0.01, closed=True)


def test_clock_verifies_aligned_boundary():
    """A healthy broker clock puts closed bars exactly on M5 boundaries. The
    estimator must call that VERIFIED (it previously compared against
    wall-clock now and never verified)."""
    est = ClockOffsetEstimator()
    end = (int(time.time()) // 300) * 300
    state = est.estimate("deriv", [_candle(end)] * 130, utc_now())
    assert state.verified
    assert abs(state.offset_seconds) < 5
    assert state.confidence > 0.5


def test_clock_rejects_misaligned_boundary():
    est = ClockOffsetEstimator()
    end = (int(time.time()) // 300) * 300 + 120  # 2 minutes off-boundary
    state = est.estimate("fpmarkets", [_candle(end)] * 130, utc_now())
    assert not state.verified
    assert state.reason == "OFFSET_OUT_OF_RANGE"


def test_clock_rejects_stale_sample():
    est = ClockOffsetEstimator()
    end = (int(time.time()) // 300) * 300 - 3 * 86400
    state = est.estimate("deriv", [_candle(end)] * 130, utc_now())
    assert not state.verified
    assert state.reason == "STALE_CLOCK_SAMPLE"


def test_clock_requires_samples():
    est = ClockOffsetEstimator()
    state = est.estimate("deriv", [], utc_now())
    assert not state.verified
    assert state.reason == "NO_CLOCK_SAMPLE"


# ---------------------------------------------------- candidate gate -----
def test_gate_defaults_fail_closed_for_unknown_account():
    """With no CRM licence, no worker binding and no armed writer, every
    attestation flag must be False (fail-closed)."""
    from abutron.main import _gate_overrides_for
    from abutron.execution.writer_gateway import SingleWriterGateway

    gw = SingleWriterGateway(EventLog(os.path.join(DATA_ROOT, "t_gw.jsonl")))
    gate = _gate_overrides_for({"connection_id": "nobody", "owner_id": "",
                                "broker": "deriv", "effective_on": 0,
                                "conn_status": "NOT_LINKED"}, gw)
    assert gate["account_bound"] is False
    assert gate["worker_nonce_bound"] is False
    assert gate["fresh_broker_session"] is False
    assert gate["crm_entitlement_approved"] is False
    assert gate["native_ea_account_match"] is False
    assert gate["broker_trade_allowed"] is False
    assert gate["account_equity_usd"] == 0.0


def test_gate_derives_flags_from_real_evidence(tmp_path):
    """A licensed, connected, armed account must produce a passing gate —
    otherwise no trade can ever execute."""
    from abutron.crm.controls import crm
    from abutron.db import engine_db
    from abutron.main import _gate_overrides_for
    from abutron.execution.writer_gateway import SingleWriterGateway
    import abutron.crypto as C

    cid = "gate-acct"
    crm.grant_license(license_id="gate-lic", owner_id="gate-owner", tier="pro")
    crm.register_account(connection_id=cid, owner_id="gate-owner",
                         broker="deriv", mt5_login=6207, display_name="G",
                         environment="demo", server="Deriv-Demo",
                         symbols=["Volatility 100 Index.0"])
    row = engine_db.get_account(cid)
    row["worker_binding"] = "w-1"
    row["conn_status"] = "CONNECTED"
    row["conn_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime())
    row["equity"] = 10000.0
    row["balance"] = 10000.0
    row["effective_on"] = 1
    row["desired_on"] = 1
    engine_db.upsert_account(row)

    ea = tmp_path / "EA.ex5"
    ea.write_text("fake-ea-binary")
    gw = SingleWriterGateway(EventLog(os.path.join(DATA_ROOT, "t_gw2.jsonl")))
    gw.arm(connection_id=cid, ea_path=str(ea),
           expected_sha256=C.pin_binary(str(ea)),
           activation_revision="rev-1", broker="deriv")

    gate = _gate_overrides_for(engine_db.get_account(cid), gw)
    assert gate["account_bound"]
    assert gate["worker_nonce_bound"]
    assert gate["fresh_broker_session"]
    assert gate["native_ea_account_match"]
    assert gate["terminal_trade_allowed"]
    assert gate["broker_trade_allowed"]
    assert gate["crm_entitlement_approved"]
    assert gate["single_writer_exclusive"]
    assert gate["account_equity_usd"] == 10000.0
    assert gate["order_history_uncertain"] is False


def test_gate_flags_unknown_receipt_as_unreconciled(tmp_path):
    """An UNKNOWN send means exposure is not reconciled: new entries must be
    blocked until it resolves."""
    from abutron.crm.controls import crm
    from abutron.db import engine_db
    from abutron.main import _gate_overrides_for
    from abutron.execution.writer_gateway import SingleWriterGateway
    from abutron.execution.receipts import ReceiptManager

    cid = "gate-unknown"
    crm.grant_license(license_id="gate-lic2", owner_id="gate-owner2", tier="pro")
    crm.register_account(connection_id=cid, owner_id="gate-owner2",
                         broker="deriv", mt5_login=6208, display_name="G2",
                         environment="demo", server="Deriv-Demo",
                         symbols=["Volatility 100 Index.0"])
    log = EventLog(os.path.join(DATA_ROOT, "t_gw3.jsonl"))
    ReceiptManager(log).record_unknown(
        candidate_id="cand_unknown", connection_id=cid, mt5_login=6208,
        symbol="Volatility 100 Index.0", direction="BUY", volume=1.0,
        reason_code="SEND_UNKNOWN")

    gw = SingleWriterGateway(log)
    gate = _gate_overrides_for(engine_db.get_account(cid), gw)
    assert gate["order_history_uncertain"] is True
    assert gate["broker_exposure_reconciled"] is False
    assert gate["broker_positions_protected"] is False
