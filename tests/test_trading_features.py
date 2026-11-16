"""Tests for the trading-feature additions: brain lot sizing, trailing
stop, self-learning from losses (never enter too early), and MT5 terminal
launch on live connect.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.config import DATA_ROOT
from abutron.brain.brain import TradeMemory
from abutron.apex.sniper import SniperEntryEngine
from abutron.apex.trailing import TrailingStop
from abutron.apex.indicators import atr

os.makedirs(DATA_ROOT, exist_ok=True)


# ---------------------------------------------------------------------------
# Lot sizing — the AI brain computes lots from client equity + risk
# ---------------------------------------------------------------------------
class TestLotSizing:
    def test_lots_from_equity_and_risk(self):
        from abutron.brain import make_brain, default_ensemble
        brain = make_brain(ensemble=default_ensemble(seed=0))
        # equity 10000, 1% risk = 100 USD; stop 100 points; point_value
        # 10 USD/point/lot -> risk_per_lot = 1000 -> lots = 0.1
        sizing = brain.size_position(
            equity=10000.0, risk_pct=0.01, stop_distance_points=100.0,
            point_value=10.0, lot_step=0.01, entry_quality=1.0)
        assert sizing["reason"] == "OK"
        assert sizing["lots"] == pytest.approx(0.1, abs=0.001)
        assert sizing["risk_usd"] == pytest.approx(100.0, abs=0.5)

    def test_entry_quality_scales_lot(self):
        from abutron.brain import make_brain, default_ensemble
        brain = make_brain(ensemble=default_ensemble(seed=0))
        full = brain.size_position(equity=10000.0, risk_pct=0.01,
                                   stop_distance_points=100.0,
                                   point_value=10.0, entry_quality=1.0)
        weak = brain.size_position(equity=10000.0, risk_pct=0.01,
                                   stop_distance_points=100.0,
                                   point_value=10.0, entry_quality=0.0)
        # cleaner entry risks more -> larger lot
        assert full["lots"] > weak["lots"]

    def test_lots_clamped_to_max(self):
        from abutron.brain import make_brain, default_ensemble
        brain = make_brain(ensemble=default_ensemble(seed=0))
        sizing = brain.size_position(equity=10_000_000.0, risk_pct=1.0,
                                     stop_distance_points=1.0,
                                     point_value=1.0, lot_max=50.0,
                                     entry_quality=1.0)
        assert sizing["lots"] <= 50.0

    def test_invalid_inputs(self):
        from abutron.brain import make_brain, default_ensemble
        brain = make_brain(ensemble=default_ensemble(seed=0))
        sizing = brain.size_position(equity=0.0, risk_pct=0.01,
                                     stop_distance_points=100.0,
                                     point_value=10.0)
        assert sizing["lots"] == 0.0


# ---------------------------------------------------------------------------
# Trailing stop
# ---------------------------------------------------------------------------
class TestTrailingStop:
    def test_long_trails_up(self):
        highs = [1.0 + i * 0.001 for i in range(30)]
        lows = [h - 0.0005 for h in highs]
        a = atr(highs, lows, [h - 0.0002 for h in highs], 14)
        plan = TrailingStop().plan(direction="BUY", highs=highs, lows=lows,
                                   atr_val=a, entry_price=1.0)
        # trail stop is above entry (in profit) and below the highest high
        assert plan.trail_stop > 1.0
        assert plan.trail_stop < highs[-1]
        assert plan.activated is True
        assert plan.trail_k == TrailingStop.DEFAULT_K

    def test_short_trails_down(self):
        lows = [1.0 - i * 0.001 for i in range(30)]
        highs = [l + 0.0005 for l in lows]
        a = atr(highs, lows, [l + 0.0002 for l in lows], 14)
        plan = TrailingStop().plan(direction="SELL", highs=highs, lows=lows,
                                   atr_val=a, entry_price=1.0)
        assert plan.trail_stop < 1.0
        assert plan.trail_stop > lows[-1]
        assert plan.activated is True

    def test_not_activated_uses_protective_stop(self):
        # flat market: trail not in profit -> protective invalidation
        highs = [1.0] * 20
        lows = [1.0] * 20
        a = 0.001
        plan = TrailingStop().plan(direction="BUY", highs=highs, lows=lows,
                                   atr_val=a, entry_price=1.0)
        assert plan.activated is False
        assert plan.trail_stop == pytest.approx(1.0 - 0.3 * a, abs=1e-6)


# ---------------------------------------------------------------------------
# Self-learning from losses — never enter too early
# ---------------------------------------------------------------------------
class TestSelfLearning:
    def test_loss_raises_confirm_bias_and_quality_floor(self):
        tm = TradeMemory()
        before = tm.get_entry_bias("GBPUSD.r", "trend_up")
        assert before["confirm_bias"] == 0.0
        tm.learn_from_trade(symbol="GBPUSD.r", regime="trend_up",
                            direction="BUY", entry_type="LIMIT",
                            entry_quality=0.6, pnl=-50.0)
        after = tm.get_entry_bias("GBPUSD.r", "trend_up")
        assert after["confirm_bias"] > before["confirm_bias"]
        assert after["quality_floor"] > before["quality_floor"]
        assert after["losses"] == 1.0

    def test_win_decays_bias(self):
        tm = TradeMemory()
        tm.learn_from_trade(symbol="X", regime="trend_up", direction="BUY",
                            entry_type="LIMIT", entry_quality=0.6, pnl=-30.0)
        bias_after_loss = tm.get_entry_bias("X", "trend_up")["confirm_bias"]
        tm.learn_from_trade(symbol="X", regime="trend_up", direction="BUY",
                            entry_type="LIMIT", entry_quality=0.8, pnl=20.0)
        bias_after_win = tm.get_entry_bias("X", "trend_up")["confirm_bias"]
        assert bias_after_win < bias_after_loss

    def test_high_confirm_bias_forces_confirm_entry(self):
        """After a loss, the brain prefers CONFIRM — never enter too early."""
        closes = [1.2500 + i * 0.0002 for i in range(60)]
        highs = [c + 0.0003 for c in closes]
        lows = [c - 0.0003 for c in closes]
        a = atr(highs, lows, closes, 14)
        sniper = SniperEntryEngine()
        # no bias -> LIMIT pullback
        p_limit = sniper.plan(symbol="X", direction="BUY", closes=closes,
                              highs=highs, lows=lows, atr_val=a,
                              confirm_bias=0.0)
        assert p_limit.entry_type == "LIMIT"
        # high confirm bias (learned from a loss) -> CONFIRM (wait)
        p_confirm = sniper.plan(symbol="X", direction="BUY", closes=closes,
                                highs=highs, lows=lows, atr_val=a,
                                confirm_bias=0.8)
        assert p_confirm.entry_type == "CONFIRM"

    def test_quality_floor_applied(self):
        closes = [1.2500 + i * 0.0002 for i in range(60)]
        highs = [c + 0.0003 for c in closes]
        lows = [c - 0.0003 for c in closes]
        a = atr(highs, lows, closes, 14)
        sniper = SniperEntryEngine()
        p = sniper.plan(symbol="X", direction="BUY", closes=closes,
                        highs=highs, lows=lows, atr_val=a,
                        quality_floor=0.9)
        assert p.entry_quality >= 0.9

    def test_brain_learn_from_trade_wired(self):
        from abutron.brain import make_brain, default_ensemble
        brain = make_brain(ensemble=default_ensemble(seed=0))
        bias = brain.learn_from_trade(symbol="GBPUSD.r", regime="trend_up",
                                      direction="BUY", entry_type="LIMIT",
                                      entry_quality=0.5, pnl=-40.0)
        assert bias["confirm_bias"] > 0.0
        assert bias["losses"] == 1.0


# ---------------------------------------------------------------------------
# Engine integration — candidate carries trail + brain-computed lots
# ---------------------------------------------------------------------------
class TestEngineTradingFeatures:
    def _clear(self):
        from abutron import db as dbmod
        from sqlalchemy import delete
        from abutron.db import CandidateRow
        dbmod.engine_db.session.execute(delete(CandidateRow))
        dbmod.engine_db.session.commit()

    def test_candidate_carries_trail_and_lots(self):
        from abutron.events import EventLog
        from abutron.apex.engine import ApexEngine
        from abutron.feeds.feed_quality import Candle, Quote

        self._clear()
        log = EventLog(os.path.join(DATA_ROOT, "t_tf_engine.jsonl"))
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
        quote = Quote(symbol="TESTUSD", bid=closes[-1], ask=closes[-1] * 1.0001,
                      spread=0.2, tick_size=0.0001, point=0.0001,
                      point_value=10.0, volume_min=0.01, volume_max=50.0)
        account = {"connection_id": "tf_conn", "broker": "deriv",
                   "owner_id": "owner1", "effective_on": True,
                   "desired_on": True, "per_trade_risk_pct": 0.05,
                   "equity": 10000.0}
        result = engine.run_decision_pass(
            connection_id="tf_conn", broker="deriv", owner_id="owner1",
            candles=candles[-1:], quotes=[quote], account=account)
        assert result
        cand = result[0]
        # trailing stop geometry attached
        assert cand.trail_k > 0
        assert cand.trail_stop > 0
        assert cand.trail_plan["trail_k"] == cand.trail_k
        # brain-computed lot size from equity + risk
        assert cand.volume > 0.0
        assert cand.stop_risk_usd > 0.0
