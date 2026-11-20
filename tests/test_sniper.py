"""Sniper-entry tests — precise, low-drawdown entry geometry.

Entry precision is what controls drawdown: every entry must be a sniper
entry (trigger-based fill + tight invalidation), not a naive market fill.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.config import DATA_ROOT
from abutron.apex.sniper import SniperEntryEngine, sniper_engine
from abutron.apex.indicators import atr

os.makedirs(DATA_ROOT, exist_ok=True)


def _clear_candidates():
    """Clear the shared candidates table so at-most-once replay keys
    don't collide across test runs."""
    from abutron import db as dbmod
    from sqlalchemy import delete
    from abutron.db import CandidateRow
    dbmod.engine_db.session.execute(delete(CandidateRow))
    dbmod.engine_db.session.commit()


def _series(closes, highs=None, lows=None):
    closes = list(closes)
    highs = list(highs) if highs is not None else [
        c + 0.0003 for c in closes]
    lows = list(lows) if lows is not None else [
        c - 0.0003 for c in closes]
    return closes, highs, lows


# ---------------------------------------------------------------------------
# Entry geometry
# ---------------------------------------------------------------------------
class TestSniperGeometry:
    def test_limit_pullback_entry_for_buy(self):
        """A BUY with a clear support level below price -> LIMIT pullback."""
        # gentle uptrend; last close near the high, EMA/swing below price
        closes = [1.2500 + i * 0.0002 for i in range(60)]
        highs = [c + 0.0003 for c in closes]
        lows = [c - 0.0003 for c in closes]
        a = atr(highs, lows, closes, 14) or 0.001
        plan = sniper_engine.plan(
            symbol="GBPUSD.r", direction="BUY",
            closes=closes, highs=highs, lows=lows, atr_val=a)
        assert plan.entry_type == "LIMIT"
        # trigger is at/below the current close (a pullback to support)
        assert plan.trigger_price <= closes[-1]
        # tight invalidation just beyond the trigger (tiny wrong-entry loss)
        assert plan.invalidation < plan.trigger_price
        assert (plan.trigger_price - plan.invalidation) <= 0.4 * a
        # tight TP locks profit fast, above the trigger
        assert plan.take_profit > plan.trigger_price

    def test_limit_pullback_entry_for_sell(self):
        """A SELL with resistance above price -> LIMIT pullback."""
        closes = [1.2600 - i * 0.0002 for i in range(60)]
        highs = [c + 0.0003 for c in closes]
        lows = [c - 0.0003 for c in closes]
        a = atr(highs, lows, closes, 14) or 0.001
        plan = sniper_engine.plan(
            symbol="GBPUSD.r", direction="SELL",
            closes=closes, highs=highs, lows=lows, atr_val=a)
        assert plan.entry_type == "LIMIT"
        assert plan.trigger_price >= closes[-1]
        assert plan.invalidation > plan.trigger_price
        assert plan.take_profit < plan.trigger_price

    def test_confirm_entry_when_extended(self):
        """A sharp gap with no practical pullback level -> CONFIRM entry."""
        # flat base then a sharp jump up: the recent swing is > 1.5 ATR
        # below price, so no practical BUY pullback level exists.
        closes = [1.2500] * 40 + [1.2500 + i * 0.01 for i in range(1, 21)]
        highs = [c + 0.0003 for c in closes]
        lows = [c - 0.0003 for c in closes]
        a = atr(highs, lows, closes, 14) or 0.001
        plan = sniper_engine.plan(
            symbol="GBPUSD.r", direction="BUY",
            closes=closes, highs=highs, lows=lows, atr_val=a)
        # no level within 1.5 ATR on the correct side -> CONFIRM at close
        assert plan.entry_type == "CONFIRM"
        assert plan.trigger_price == closes[-1]
        assert plan.invalidation < plan.trigger_price

    def test_invalidation_is_tight_relative_to_atr(self):
        closes = [1.2500 + i * 0.0002 for i in range(60)]
        highs = [c + 0.0003 for c in closes]
        lows = [c - 0.0003 for c in closes]
        a = atr(highs, lows, closes, 14)
        plan = sniper_engine.plan(
            symbol="GBPUSD.r", direction="BUY",
            closes=closes, highs=highs, lows=lows, atr_val=a)
        # wrong-entry loss bounded to ~0.3 ATR (sniper-tight)
        loss = plan.trigger_price - plan.invalidation
        assert loss <= 0.4 * a


# ---------------------------------------------------------------------------
# Entry quality
# ---------------------------------------------------------------------------
class TestEntryQuality:
    def _base(self):
        closes = [1.2500 + i * 0.0002 for i in range(60)]
        highs = [c + 0.0003 for c in closes]
        lows = [c - 0.0003 for c in closes]
        return closes, highs, lows, atr(highs, lows, closes, 14)

    def test_aligned_flow_scores_higher(self):
        closes, highs, lows, a = self._base()
        aligned = {"delta_pct": 0.8, "buy_ratio": 0.8, "imbalance": 0.8}
        misaligned = {"delta_pct": -0.8, "buy_ratio": 0.2, "imbalance": -0.8}
        q_aligned = sniper_engine.plan(
            symbol="X", direction="BUY", closes=closes, highs=highs,
            lows=lows, atr_val=a, of_features=aligned,
            conviction=0.8, uncertainty=0.1).entry_quality
        q_misaligned = sniper_engine.plan(
            symbol="X", direction="BUY", closes=closes, highs=highs,
            lows=lows, atr_val=a, of_features=misaligned,
            conviction=0.8, uncertainty=0.1).entry_quality
        assert q_aligned > q_misaligned

    def test_limit_scores_higher_than_confirm(self):
        """A clean pullback entry is more precise than a chasing confirm."""
        closes, highs, lows, a = self._base()
        q_limit = sniper_engine.plan(
            symbol="X", direction="BUY", closes=closes, highs=highs,
            lows=lows, atr_val=a, conviction=0.8, uncertainty=0.1,
            regime="trend_up").entry_quality
        # extended market -> CONFIRM
        extended_closes = [1.2500] * 40 + [1.2500 + i * 0.01 for i in range(1, 21)]
        eh = [c + 0.0003 for c in extended_closes]
        el = [c - 0.0003 for c in extended_closes]
        ea = atr(eh, el, extended_closes, 14)
        q_confirm = sniper_engine.plan(
            symbol="X", direction="BUY", closes=extended_closes, highs=eh,
            lows=el, atr_val=ea, conviction=0.8, uncertainty=0.1,
            regime="trend_up").entry_quality
        assert q_limit > q_confirm

    def test_quality_bounded(self):
        closes, highs, lows, a = self._base()
        q = sniper_engine.plan(
            symbol="X", direction="BUY", closes=closes, highs=highs,
            lows=lows, atr_val=a, conviction=1.0, uncertainty=0.0,
            of_features={"delta_pct": 1.0, "buy_ratio": 1.0},
            regime="trend_up").entry_quality
        assert 0.0 <= q <= 1.0


# ---------------------------------------------------------------------------
# Engine integration — candidates carry sniper geometry
# ---------------------------------------------------------------------------
class TestSniperEngineIntegration:
    def test_candidate_carries_sniper_geometry(self):
        from abutron.events import EventLog
        from abutron.apex.engine import ApexEngine
        from abutron.feeds.feed_quality import Candle

        _clear_candidates()
        log = EventLog(os.path.join(DATA_ROOT, "t_sniper_engine.jsonl"))
        engine = ApexEngine(log)
        closes = [1.2500 + i * 0.0005 for i in range(200)]
        candles = []
        t = 0
        for price in closes:
            candles.append(Candle(symbol="TESTUSD", timeframe="M5",
                                  open_time=str(t), close_time=str(t + 300),
                                  open=price, high=price * 1.001,
                                  low=price * 0.999, close=price,
                                  volume=1.0, spread=0.0001,
                                  tick_size=0.0001))
            t += 300
        for c in candles:
            engine.set_history(c.symbol, c.timeframe, candles)
        account = {"connection_id": "sniper_conn1", "broker": "deriv",
                   "owner_id": "owner1", "effective_on": True,
                   "desired_on": True, "per_trade_risk_pct": 0.5,
                   "equity": 10000.0}
        result = engine.run_decision_pass(
            connection_id="sniper_conn1", broker="deriv", owner_id="owner1",
            candles=candles[-1:], quotes=[], account=account)
        assert result, "expected at least one sniper candidate"
        cand = result[0]
        # sniper geometry is attached
        assert cand.entry_type in ("LIMIT", "CONFIRM")
        assert cand.trigger_price > 0
        assert cand.invalidation > 0
        assert 0.0 <= cand.entry_quality <= 1.0
        assert cand.sniper_plan["entry_type"] == cand.entry_type
        # stops come from the sniper plan (tight invalidation, not wide ATR)
        assert cand.stop_loss == cand.invalidation
        assert cand.take_profit == pytest.approx(cand.sniper_plan["take_profit"])

    def test_candidate_persists_sniper_plan(self):
        """The sniper plan survives the candidate -> DB round-trip."""
        from abutron.events import EventLog
        from abutron.apex.engine import ApexEngine
        from abutron.feeds.feed_quality import Candle
        from abutron import db as dbmod

        _clear_candidates()
        log = EventLog(os.path.join(DATA_ROOT, "t_sniper_persist.jsonl"))
        engine = ApexEngine(log)
        closes = [1.2500 + i * 0.0005 for i in range(200)]
        candles = [Candle(symbol="TESTUSD", timeframe="M5",
                          open_time=str(i * 300), close_time=str((i + 1) * 300),
                          open=closes[i], high=closes[i] * 1.001,
                          low=closes[i] * 0.999, close=closes[i],
                          volume=1.0, spread=0.0001, tick_size=0.0001)
                   for i in range(len(closes))]
        for c in candles:
            engine.set_history(c.symbol, c.timeframe, candles)
        account = {"connection_id": "cper", "broker": "deriv",
                   "owner_id": "owner1", "effective_on": True,
                   "desired_on": True, "per_trade_risk_pct": 0.5,
                   "equity": 10000.0}
        result = engine.run_decision_pass(
            connection_id="cper", broker="deriv", owner_id="owner1",
            candles=candles[-1:], quotes=[], account=account)
        assert result
        cid = result[0].candidate_id
        stored = dbmod.engine_db.get_candidate(cid)
        assert stored is not None
        assert stored["entry_type"] in ("LIMIT", "CONFIRM")
        assert stored["trigger_price"] > 0
        assert stored["entry_quality"] >= 0.0
        assert "sniper_plan" in stored and stored["sniper_plan"]


# ---------------------------------------------------------------------------
# Execution path — the sniper entry geometry reaches the order request
# ---------------------------------------------------------------------------
class TestSniperExecutionPath:
    def test_gateway_sends_sniper_entry_geometry(self, monkeypatch):
        from abutron.events import EventLog
        from abutron.execution.writer_gateway import SingleWriterGateway

        log = EventLog(os.path.join(DATA_ROOT, "t_sniper_gw.jsonl"))
        gw = SingleWriterGateway(log)
        from abutron.execution.writer_gateway import WriterStatus
        monkeypatch.setenv("ABUTRON_ACTIVATION_SECRET", "local-fixture-value")
        gw._armed["c1"] = WriterStatus("c1", True, "fixture-sha", "fixture-sha", "rev", True, False, "now")
        captured = {}

        def _fake_deliver(request, timeout):
            captured.update(request)
            return "ACK", {"status": "ack", "ticket": 4242}

        gw._deliver_to_ea = _fake_deliver
        cand = {
            "candidate_id": "cand_snip", "symbol": "GBPUSD.r",
            "direction": "BUY", "volume": 0.5,
            "entry_type": "LIMIT", "trigger_price": 1.2540,
            "stop_loss": 1.2532, "take_profit": 1.2560,
            "price": 1.2550,
        }
        res = gw.send_order(connection_id="c1", candidate=cand,
                            account={"mt5_login": 6207}, broker="fpmarkets")
        assert res["outcome"] == "ACK"
        # the sniper entry geometry is carried into the native order request
        assert captured["entry_type"] == "LIMIT"
        assert captured["trigger_price"] == 1.2540
        assert captured["stop_loss"] == 1.2532
        assert captured["take_profit"] == 1.2560

    def test_dispatcher_receipt_uses_trigger_price(self, monkeypatch):
        from abutron.events import EventLog
        from abutron.execution.dispatcher import NativeDispatcher
        from abutron.execution.writer_gateway import SingleWriterGateway
        from abutron.execution.receipts import ReceiptManager
        from abutron import db as dbmod

        _clear_candidates()
        log = EventLog(os.path.join(DATA_ROOT, "t_sniper_disp.jsonl"))
        gw = SingleWriterGateway(log)
        from abutron.execution.writer_gateway import WriterStatus
        monkeypatch.setenv("ABUTRON_ACTIVATION_SECRET", "local-fixture-value")
        gw._armed["snip_disp"] = WriterStatus("snip_disp", True, "fixture-sha", "fixture-sha", "rev", True, False, "now")

        def _fake_deliver(request, timeout):
            return "ACK", {"status": "ack", "ticket": 7007}

        gw._deliver_to_ea = _fake_deliver
        rm = ReceiptManager(log)
        disp = NativeDispatcher(log, gw, rm)

        # seed an account + a reserved candidate with sniper geometry
        dbmod.engine_db.upsert_account({
            "connection_id": "snip_disp", "broker": "fpmarkets",
            "mt5_login": 6207, "owner_id": "owner1",
            "environment": "demo", "desired_on": 1, "effective_on": 1,
            "per_trade_risk_pct": 0.5, "portfolio_cap_pct": 1.0,
            "ea_sha256": "", "activation_revision": "", "symbols": "GBPUSD.r"})
        dbmod.engine_db.add_candidate({
            "candidate_id": "cand_disp1", "connection_id": "snip_disp",
            "owner_id": "owner1", "broker": "fpmarkets",
            "symbol": "GBPUSD.r", "timeframe": "M5",
            "bar_open": "2026-11-13T08:00:00Z", "replay_key":
            "snip_disp|GBPUSD.r|M5|2026-11-13T08:00:00Z|BUY",
            "direction": "BUY", "conviction": 0.7, "regime": "trend_up",
            "agent_votes": "{}", "risk_fraction": 0.5,
            "stop_risk_usd": 0.0, "volume": 0.5,
            "stop_loss": 1.2532, "take_profit": 1.2560, "price": 1.2550,
            "entry_type": "LIMIT", "trigger_price": 1.2540,
            "invalidation": 1.2532, "entry_quality": 0.8,
            "sniper_plan": "{}", "execution_state": "RESERVED",
            "reason_code": "", "created_at": "", "updated_at": ""})
        # Unit-test native ACK receipt geometry with a mocked *certified*
        # broker owner inventory. Production must never use mock evidence.
        from unittest.mock import patch
        native_owner = {"owner_risk_certified": True, "cap_exceeded": False,
                        "aggregate_stop_risk_usd": 0.0}
        with patch("abutron.risk.risk_engine.RiskEngine.aggregate_owner_risk",
                   return_value=native_owner):
            res = disp.dispatch_account("snip_disp")
        assert res["ack"] == 1
        receipts = dbmod.engine_db.list_receipts("snip_disp")
        receipt = next(r for r in receipts if r.get("candidate_id") == "cand_disp1")
        # the recorded fill price is the sniper trigger, not the close
        assert receipt["fill_price"] == 1.2540
