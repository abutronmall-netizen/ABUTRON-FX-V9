"""Tests for true tick/trade data ingestion (PROXY → VERIFIED path)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from abutron.feeds.tick_ingestion import TickIngestion, DepthSnapshot
from abutron.analytics.orderflow import Trade
from abutron.brain.signals import load_tick_trades, load_depth_signals


class TickIngestionTest(unittest.TestCase):
    """Verify TickIngestion loads MT5 export files into Trade objects."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_export(self, filename, data):
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w") as fh:
            json.dump(data, fh)
        return path

    def test_load_trades_from_mt5_export(self):
        self._write_export(
            "ABUTRON_NATIVE_TRADES_V2_2026_11_17_120000.txt",
            {
                "connection_id": "conn1",
                "symbol": "EURUSD.r",
                "from": 1234567890,
                "deal_count": 3,
                "deals": [
                    {"deal": 1, "time": 1234567895, "price": 1.1000,
                     "volume": 0.10, "type": 0, "entry": 0,
                     "order": 1001},
                    {"deal": 2, "time": 1234567900, "price": 1.1001,
                     "volume": 0.20, "type": 1, "entry": 3,
                     "order": 1002},
                    {"deal": 3, "time": 1234567905, "price": 1.1002,
                     "volume": 0.15, "type": 0, "entry": 2,
                     "order": 1003},
                ],
            })
        ingestion = TickIngestion(self.tmpdir)
        self.assertEqual(ingestion.load_trades(), {})
        trades = ingestion.load_account_execution_deals()
        self.assertEqual(len(trades), 1)  # all in one M5 bar
        total = sum(len(t) for t in trades.values())
        self.assertEqual(total, 3)
        # Check trade objects
        for t in trades[list(trades.keys())[0]]:
            self.assertIsInstance(t, Trade)
            self.assertIn(t.side, ("buy", "sell"))
            self.assertGreater(t.price, 0)
            self.assertGreater(t.size, 0)

    def test_load_depth_from_mt5_export(self):
        self._write_export(
            "ABUTRON_NATIVE_DEPTH_V2_2026_11_17_120000.txt",
            {
                "connection_id": "conn1",
                "symbol": "EURUSD.r",
                "bid": 1.1000,
                "ask": 1.1002,
                "spread": 2,
                "last": 1.1001,
                "volume": 500,
                "timestamp": "2026_11_17_12_00_00",
                "source": "ApexOrderFlowExport",
            })
        ingestion = TickIngestion(self.tmpdir)
        self.assertEqual(ingestion.load_depth(), {})
        depth = ingestion.load_quote_snapshots()
        self.assertEqual(len(depth), 1)
        snap = list(depth.values())[0]
        self.assertEqual(snap["bid"], 1.1000)
        self.assertEqual(snap["ask"], 1.1002)
        self.assertEqual(snap["source"], "mt5_latest_bid_ask_quote_not_dom")

    def test_verification_summary_verified(self):
        self._write_export(
            "ABUTRON_NATIVE_TRADES_V2_2026_11_17_120000.txt",
            {
                "connection_id": "conn1",
                "symbol": "EURUSD.r",
                "deal_count": 1,
                "deals": [
                    {"deal": 1, "time": 1234567895, "price": 1.1000,
                     "volume": 0.10, "type": 0, "entry": 0,
                     "order": 1001},
                ],
            })
        ingestion = TickIngestion(self.tmpdir)
        summary = ingestion.verification_summary()
        self.assertFalse(summary["verified"])
        self.assertEqual(summary["total_trade_events"], 0)
        self.assertEqual(summary["account_execution_deals_not_market_trades"], 1)
        self.assertIn("PROXY", summary["honest_status"])

    def test_verification_summary_no_data(self):
        ingestion = TickIngestion(self.tmpdir)
        summary = ingestion.verification_summary()
        self.assertFalse(summary["verified"])
        self.assertIn("NO_DATA", summary["honest_status"])

    def test_load_bars_from_mt5_export(self):
        self._write_export(
            "ABUTRON_NATIVE_BARS_V2_2026_11_17_120000.txt",
            {
                "connection_id": "conn1",
                "symbol": "EURUSD.r",
                "timeframe": "M5",
                "bar_count": 1,
                "bars": [
                    {"time": 1234567890, "open": 1.1000, "high": 1.1005,
                     "low": 1.0995, "close": 1.1002,
                     "tick_volume": 100, "spread": 2},
                ],
            })
        ingestion = TickIngestion(self.tmpdir)
        bars = ingestion.load_bars()
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["symbol"], "EURUSD.r")
        self.assertEqual(bars[0]["open"], 1.1000)


class LoadTickTradesCSVTest(unittest.TestCase):
    """Verify CSV tick loading (alternative to MT5 export)."""

    def test_load_tick_trades_csv(self):
        import tempfile
        tmpdir = tempfile.mkdtemp()
        try:
            csv_path = os.path.join(tmpdir, "tick_trades.csv")
            with open(csv_path, "w") as fh:
                fh.write("symbol,bar_open_utc,price,size,side\n")
                fh.write("EURUSD.r,2026-11-17T12:00:00Z,1.1000,0.10,buy\n")
                fh.write("EURUSD.r,2026-11-17T12:00:00Z,1.1001,0.20,sell\n")
            trades = load_tick_trades(csv_path, symbol="EURUSD.r")
            self.assertEqual(len(trades), 1)
            self.assertEqual(len(trades[list(trades.keys())[0]]), 2)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class BacktestEngineTickDataTest(unittest.TestCase):
    """Verify BacktestEngine accepts and uses tick_trades."""

    def test_engine_accepts_tick_trades(self):
        from abutron.backtest.engine import BacktestEngine
        from abutron.brain.brain import make_brain
        brain = make_brain()
        engine = BacktestEngine(
            brain, equity=10000, risk_pct=0.05,
            tick_trades={}, depth_signals={})
        self.assertEqual(engine.tick_trades, {})
        self.assertEqual(engine.depth_signals, {})


if __name__ == "__main__":
    unittest.main()
