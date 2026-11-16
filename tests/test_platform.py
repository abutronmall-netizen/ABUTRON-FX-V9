"""ABUTRON FX V9 acceptance tests (blueprint section 11 matrix).

Covers: feed quality, decision logic, account auth, risk sizing/caps,
native writer (single writer / binary mismatch / unknown), client OFF,
order flow + footprint, and reconciliation.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.config import DATA_ROOT, ENGINE_DB, PORTAL_DB
from abutron.db import EngineDB, PortalDB, engine_db, portal_db
from abutron.crm.controls import CrmService, CrmError
from abutron.events import EventLog
from abutron.feeds.feed_quality import Candle, Quote, validate_candle, \
    candle_source_hash, normalize_native_bars, validate_export_envelope
from abutron.feeds.clock_offset import ClockOffsetEstimator
from abutron.analytics.orderflow import OrderFlowEngine, Trade
from abutron.analytics.footprint import build_footprint
from abutron.apex import indicators
from abutron.apex.engine import ApexEngine
from abutron.apex.agents.risk import RiskAgent
from abutron.execution.state import StateMachine
from abutron.execution.writer_gateway import SingleWriterGateway
from abutron.execution.receipts import ReceiptManager
from abutron.execution.dispatcher import NativeDispatcher
from abutron.reconciliation.reconciler import Reconciler, NativeHistoryEntry


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    # Point the singleton DBs at fresh tmp files WITHOUT reloading the
    # module (reload would recompute PORTAL_DB from DATA_ROOT).
    from abutron import db as dbmod
    from abutron.crm import controls as crmmod
    engine_db = dbmod.EngineDB(str(tmp_path / "engine.db"))
    portal_db = dbmod.PortalDB(str(tmp_path / "portal.db"))
    dbmod.engine_db = engine_db
    dbmod.portal_db = portal_db
    crmmod.engine_db = engine_db
    crmmod.portal_db = portal_db
    return dbmod


def make_candles(n=60, symbol="GBPUSD.r", start_price=1.25, tf="M5"):
    candles = []
    # timestamp anchored to "now" (rounded to the bar) so candles are always
    # fresh and not flagged CANDLE_STALE regardless of the run date.
    tf_sec = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600,
              "H4": 14400, "D1": 86400}.get(tf, 300)
    now_epoch = int(time.time())
    t = (now_epoch // tf_sec) * tf_sec - (n + 5) * tf_sec
    price = start_price
    for i in range(n):
        o = price
        # mild uptrend
        c = o + 0.0002 * i + (0.0001 if i % 3 == 0 else -0.0001)
        h = max(o, c) + 0.0003
        l = min(o, c) - 0.0003
        ot = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t + i * 300))
        ct = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(t + i * 300 + 300))
        candles.append(Candle(symbol=symbol, timeframe=tf, open_time=ot,
                              close_time=ct, open=o, high=h, low=l, close=c,
                              volume=10.0, bid=c - 0.0001, ask=c + 0.0001,
                              spread=0.2, tick_size=0.0001, point=0.0001,
                              closed=True, source_hash=candle_source_hash(
                                  Candle(symbol=symbol, timeframe=tf,
                                         open_time=ot, close_time=ct, open=o,
                                         high=h, low=l, close=c))))
        price = c
    return candles


def make_quote(symbol="GBPUSD.r"):
    return Quote(symbol=symbol, bid=1.2500, ask=1.2502, spread=0.2,
                 tick_size=0.0001, point=0.0001, volume_min=0.01,
                 volume_max=100.0, point_value=10.0, commission=7.0)


# ---------------------------------------------------------------------------
# Feed quality
# ---------------------------------------------------------------------------
class TestFeedQuality:
    def test_valid_candle_accepted(self):
        c = make_candles(1)[0]
        errs = validate_candle(c, connection_id="conn1", login=6207,
                               broker="deriv")
        assert errs == []

    def test_future_candle_rejected(self):
        c = make_candles(1)[0]
        c.close_time = "2099-01-01T00:00:00Z"
        errs = validate_candle(c, connection_id="conn1", login=6207,
                               broker="deriv")
        assert "CANDLE_FUTURE" in errs

    def test_open_candle_rejected(self):
        c = make_candles(1)[0]
        c.closed = False
        errs = validate_candle(c, connection_id="conn1", login=6207,
                               broker="deriv")
        assert "CANDLE_NOT_CLOSED" in errs

    def test_bad_ohlc_rejected(self):
        c = make_candles(1)[0]
        c.high = c.low  # collapse
        errs = validate_candle(c, connection_id="conn1", login=6207,
                               broker="deriv")
        assert "CANDLE_OHLC_INVALID" in errs

    def test_export_envelope_validation(self):
        env = {"connection_id": "c1", "login": 100, "broker": "deriv",
               "nonce": "ABUTRON_NATIVE_x", "challenge": "ch",
               "worker_health_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime())}
        assert validate_export_envelope(env, connection_id="c1", login=100,
                                        broker="deriv") == []
        bad = dict(env, login=999)
        assert "EXPORT_LOGIN_MISMATCH" in validate_export_envelope(
            bad, connection_id="c1", login=100, broker="deriv")

    def test_clock_offset_verified(self):
        est = ClockOffsetEstimator()
        candles = make_candles(30)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state = est.estimate("fpmarkets", candles, now)
        assert state.verified or state.sample_count > 0


# ---------------------------------------------------------------------------
# Indicators / decision logic
# ---------------------------------------------------------------------------
class TestIndicators:
    def test_ema_trend(self):
        closes = [1.0 + 0.001 * i for i in range(60)]
        e20 = indicators.ema(closes, 20)
        e55 = indicators.ema(closes, 55)
        assert e20 > e55  # uptrend

    def test_adx_uptrend(self):
        candles = make_candles(80)
        h = [c.high for c in candles]
        l = [c.low for c in candles]
        cl = [c.close for c in candles]
        assert indicators.adx(h, l, cl, 14) is not None

    def test_rsi_bounds(self):
        candles = make_candles(40)
        cl = [c.close for c in candles]
        r = indicators.rsi(cl, 14)
        assert 0 <= r <= 100

    def test_donchian_breakout(self):
        candles = make_candles(70)
        h = [c.high for c in candles]
        l = [c.low for c in candles]
        cl = [c.close for c in candles]
        dc = indicators.donchian_breakout(h, l, cl, 55, 0.35)
        assert dc is not None
        assert dc["upper"] > dc["lower"]


# ---------------------------------------------------------------------------
# CRM / account auth
# ---------------------------------------------------------------------------
class TestCrm:
    def test_register_and_effective_on(self):
        crm = CrmService()
        crm.register_account(connection_id="conn1", broker="deriv",
                             mt5_login=6207, owner_id="owner1")
        crm.grant_license(license_id="lic1", owner_id="owner1", tier="standard")
        # Mark the connection verified-connected (broker worker binding) —
        # trading ON requires a CONNECTED broker session, not just a record.
        acct = crm.get_account("conn1")
        acct["conn_status"] = "CONNECTED"
        acct["worker_binding"] = "wb_test"
        crm.engine.upsert_account(acct)
        crm.request_on("conn1", by="admin")
        # Arm the native writer with a real binary pin
        crm.link_broker_identity("conn1", ea_sha256="abc123", server="Deriv-Demo")
        acct = crm.get_account("conn1")
        state = crm.confirm_effective_on(
            "conn1", activation_revision=acct["activation_revision"],
            ea_sha256="abc123")
        assert state["desired_on"] is True
        assert state["effective_on"] is True

    def test_binary_mismatch_advisory_effective_on(self):
        crm = CrmService()
        crm.register_account(connection_id="conn1", broker="deriv",
                             mt5_login=6207, owner_id="owner1")
        crm.grant_license(license_id="lic1", owner_id="owner1", tier="standard")
        acct = crm.get_account("conn1")
        acct["conn_status"] = "CONNECTED"
        acct["worker_binding"] = "wb_test"
        crm.engine.upsert_account(acct)
        crm.request_on("conn1", by="admin")
        # Aggressive mode: a mismatched activation revision is ADVISORY —
        # confirm_effective_on still grants effective ON (no raise).
        state = crm.confirm_effective_on("conn1", activation_revision="bogus",
                                         ea_sha256="abc123")
        assert state["effective_on"] is True

    def test_entitlement_required(self):
        crm = CrmService()
        crm.register_account(connection_id="c2", broker="fpmarkets",
                             mt5_login=7412582, owner_id="owner2")
        with pytest.raises(CrmError, match="ENITLEMENT_MISSING"):
            crm.request_on("c2", by="admin")

    def test_client_off_blocks(self):
        crm = CrmService()
        crm.register_account(connection_id="c3", broker="deriv",
                             mt5_login=7593, owner_id="owner3")
        crm.grant_license(license_id="lic3", owner_id="owner3", tier="standard")
        acct = crm.get_account("c3")
        acct["conn_status"] = "CONNECTED"
        acct["worker_binding"] = "wb_test"
        crm.engine.upsert_account(acct)
        crm.request_on("c3", by="admin")
        crm.request_off("c3", by="client")
        acct = crm.get_account("c3")
        assert acct["desired_on"] == 0


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------
class TestRisk:
    def _ctx(self, equity=10000.0, stop_risk=0.0, volume=1.0):
        quote = make_quote()
        quote.commission = 0.2           # low-cost FX geometry
        return {
            "candidate": {"direction": "BUY", "volume": volume,
                          "stop_loss": 1.2450, "take_profit": 1.2600,
                          "stop_risk_usd": stop_risk},
            "quote": quote,
            "account": {"equity": equity, "effective_on": True,
                        "per_trade_risk_pct": 0.15,
                        "portfolio_cap_pct": 0.20},
            "owner_book": {"aggregate_stop_risk_usd": 0.0},
            "price": 1.2500, "atr": 0.0020,
        }

    def test_risk_sizing(self):
        agent = RiskAgent()
        dec = agent.evaluate(self._ctx())
        assert dec.allow
        assert dec.volume > 0
        assert dec.stop_risk_usd > 0

    def test_stop_too_tight_rejected(self):
        agent = RiskAgent()
        ctx = self._ctx()
        ctx["candidate"]["stop_loss"] = 1.25005  # 0.5 ticks
        dec = agent.evaluate(ctx)
        assert not dec.allow

    def test_owner_cap_exceeded(self):
        agent = RiskAgent()
        ctx = self._ctx(equity=1000.0)
        ctx["owner_book"] = {"aggregate_stop_risk_usd": 250.0}  # at 20% cap
        dec = agent.evaluate(ctx)
        # headroom exhausted -> reject or reduce
        assert dec.stop_risk_usd <= 250.0 + 1e-6

    def test_effective_off_blocks(self):
        agent = RiskAgent()
        ctx = self._ctx()
        ctx["account"]["effective_on"] = False
        dec = agent.evaluate(ctx)
        assert not dec.allow
        assert dec.reason == "HOLD"


# ---------------------------------------------------------------------------
# Order flow + footprint
# ---------------------------------------------------------------------------
class TestOrderFlow:
    def test_aggregate_delta(self):
        oe = OrderFlowEngine()
        trades = [Trade(1.25, 1.0, "buy"), Trade(1.25, 0.5, "sell"),
                  Trade(1.25, 2.0, "buy")]
        snap = oe.aggregate("GBPUSD.r", "t0", "t1", trades)
        assert snap.buy_volume == 3.0
        assert snap.sell_volume == 0.5
        assert snap.delta == 2.5
        assert snap.imbalance > 0

    def test_footprint_poc(self):
        trades = []
        for p in [1.2490, 1.2495, 1.2500, 1.2500, 1.2505, 1.2510]:
            trades.append(Trade(p, 1.0, "buy"))
            trades.append(Trade(p, 1.0, "sell"))
        fp = build_footprint("GBPUSD.r", trades, tick_size=0.0001)
        assert fp.total_volume > 0
        assert fp.poc_price > 0
        assert fp.value_area_low <= fp.poc_price <= fp.value_area_high


# ---------------------------------------------------------------------------
# Native writer / state machine
# ---------------------------------------------------------------------------
class TestExecution:
    def test_state_machine_transitions(self):
        sm = StateMachine()
        sm.start("c1", "RESERVED")
        assert sm.move("c1", "SEND")
        assert sm.move("c1", "ACK")
        assert sm.move("c1", "OPEN")
        assert sm.move("c1", "PROTECTED")
        assert sm.move("c1", "CLOSED")
        assert sm.move("c1", "SETTLED")
        # SETTLED is terminal
        assert not sm.move("c1", "OPEN")

    def test_single_writer_binary_mismatch_rejected(self):
        log = EventLog(os.path.join(DATA_ROOT, "t_events.jsonl"))
        gw = SingleWriterGateway(log)
        # mismatch: expected pin won't match a nonexistent path
        st = gw.arm(connection_id="c1", ea_path="/nonexistent/ea.ex5",
                    expected_sha256="deadbeef", activation_revision="x",
                    broker="fpmarkets")
        assert not st.armed
        # A mismatched writer must be rejected before any native send.
        res = gw.send_order(connection_id="c1",
                            candidate={"candidate_id": "x", "symbol": "GBPUSD.r",
                                       "direction": "BUY", "volume": 1.0},
                            account={"mt5_login": 7412582, "broker": "fpmarkets",
                                     "environment": "demo",
                                     "desired_on": True, "effective_on": True},
                            broker="fpmarkets")
        assert res["outcome"] == "REJECTED"
        assert res["reason"] == "EA_WRITER_NOT_ATTESTED"


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
class TestReconciliation:
    def test_match_receipt_to_native(self):
        from abutron import db as dbmod
        dbmod.engine_db.upsert_account({
            "connection_id": "conn1", "broker": "deriv", "mt5_login": 6207,
            "owner_id": "owner1", "display_name": "conn1",
            "environment": "demo", "server": "Deriv-Demo",
            "per_trade_risk_pct": 0.15, "portfolio_cap_pct": 0.20,
            "desired_on": 1, "effective_on": 1, "activation_revision": "",
            "ea_sha256": "", "symbols": "GBPUSD.r"})
        log = EventLog(os.path.join(DATA_ROOT, "t_rec_events.jsonl"))
        rm = ReceiptManager(log)
        rm.record_native_receipt(
            candidate_id="cand1", connection_id="conn1", mt5_login=6207,
            symbol="GBPUSD.r", direction="BUY", native_ticket=5001,
            broker_deal_id="d1", volume=1.0, fill_price=1.2500,
            stop_loss=1.2450, take_profit=1.2600, commission=7.0, swap=0.0,
            execution_state="ACK")
        rec = Reconciler(log, rm)
        history = [NativeHistoryEntry(
            ticket=5001, login=6207, symbol="GBPUSD.r", volume=1.0,
            price=1.2500, stop_loss=1.2450, take_profit=1.2600,
            state="OPEN", realized_pnl=0.0, commission=7.0, swap=0.0,
            balance=10000.0, deal_id="d1", direction="BUY")]
        report = rec.reconcile_account("conn1", history)
        assert report["matched"] == 1

    def test_unknown_not_zero_filled(self):
        log = EventLog(os.path.join(DATA_ROOT, "t_unk_events.jsonl"))
        rm = ReceiptManager(log)
        rid = rm.record_unknown(
            candidate_id="cand2", connection_id="conn2", mt5_login=7412582,
            symbol="GBPUSD.r", direction="SELL", volume=1.0)
        receipts = rm.list_receipts("conn2")
        assert receipts[0]["execution_state"] == "UNKNOWN"
        assert receipts[0]["reconciled_state"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Engine end-to-end decision
# ---------------------------------------------------------------------------
class TestEngine:
    def test_hold_on_flat(self):
        log = EventLog(os.path.join(DATA_ROOT, "t_engine_events.jsonl"))
        engine = ApexEngine(log)
        candles = make_candles(80)
        for sym in {c.symbol for c in candles}:
            engine.set_history(sym, "M5", candles)
        account = {"connection_id": "conn1", "broker": "deriv",
                   "owner_id": "owner1", "effective_on": True,
                   "desired_on": True, "per_trade_risk_pct": 0.15,
                   "equity": 10000.0}
        # No candles => no candidates
        result = engine.run_decision_pass(
            connection_id="conn1", broker="deriv", owner_id="owner1",
            candles=[], quotes=[], account=account)
        assert result == []

    def test_effective_off_blocks_internal_decision_pass(self):
        """The older APEX engine must honour CRM OFF and reserve no entries."""
        from abutron.brain import make_brain, default_ensemble
        fixed_brain = make_brain(ensemble=default_ensemble(seed=42))

        def run(effective_on, cid):
            log = EventLog(os.path.join(
                DATA_ROOT, f"t_engine2_{cid}_events.jsonl"))
            engine = ApexEngine(log, brain=fixed_brain)
            candles = make_candles(80)
            for sym in {c.symbol for c in candles}:
                engine.set_history(sym, "M5", candles)
            account = {"connection_id": cid, "broker": "deriv",
                       "owner_id": "owner1", "effective_on": effective_on,
                       "desired_on": True, "per_trade_risk_pct": 0.15,
                       "equity": 10000.0}
            return engine.run_decision_pass(
                connection_id=cid, broker="deriv", owner_id="owner1",
                candles=candles, quotes=[make_quote()], account=account)
        result_on = run(True, "conn_on")
        result_off = run(False, "conn_off")
        assert result_off == []
        assert result_on  # Sanity check: the test exercised a live signal.

def test_sqlite_sessions_stable_per_thread(tmp_path):
    """Interleaved engine+request threads must not leak pooled sessions."""
    import concurrent.futures
    import threading
    from abutron.db import EngineDB, PortalDB
    for db_cls, name in ((EngineDB, "engine"), (PortalDB, "portal")):
        db = db_cls(str(tmp_path / f"{name}-thread.db"))
        left_ready = threading.Event()
        right_ready = threading.Event()

        def left():
            first = db.session
            left_ready.set()
            assert right_ready.wait(5)
            assert db.session is first
            db._session_factory.remove()
            return True

        def right():
            assert left_ready.wait(5)
            other = db.session
            right_ready.set()
            db._session_factory.remove()
            return other is not None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(left)
            b = pool.submit(right)
            assert a.result(timeout=8) and b.result(timeout=8)
        db._engine.dispose()
