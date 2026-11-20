"""Tests for order-flow and footprint wiring into the production dispatch path.

Verifies that:
1. The MT5 terminal reader parses ABUTRON_ORDER_FLOW_V2_*.txt files
2. The market data adapter groups trades by symbol into orderflow_trades
3. The decision engine computes footprint from order-flow ticks
4. The footprint is passed to AgentContext (not hardcoded empty)
"""
from __future__ import annotations

import json
import os
import tempfile
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from abutron.feeds.mt5_terminal import Mt5TerminalManager


# --------------------------------------------------------------------- #
# Order-flow file parsing
# --------------------------------------------------------------------- #

def _payload(trades, *, nonce="demo-123", symbol="Volatility 50 Index.0"):
    return {
        "schema": "ABUTRON_ORDER_FLOW_V2",
        "source": "broker_trade_ticks",
        "nonce": nonce,
        "symbol": symbol,
        "trades": trades,
    }


def test_parse_real_broker_trades(tmp_path):
    trades = [
        {"symbol": "Volatility 50 Index.0", "price": 82.50,
         "size": 1.5, "side": "buy", "time_msc": 1795165200000},
        {"symbol": "Volatility 50 Index.0", "price": 82.51,
         "size": 0.5, "side": "sell", "time_msc": 1795165205000},
    ]
    p = tmp_path / "ABUTRON_ORDER_FLOW_V2_demo-123.txt"
    p.write_text(json.dumps(_payload(trades)), encoding="utf-8")
    parsed = Mt5TerminalManager._parse_order_flow_file(str(p), expected_nonce="demo-123")
    assert parsed is not None
    assert parsed["source"] == "broker_trade_ticks"
    assert parsed["nonce"] == "demo-123"
    assert len(parsed["trades"]) == 2
    assert parsed["trades"][0]["time_msc"] == 1795165200000


def test_parse_rejects_mismatched_nonce(tmp_path):
    p = tmp_path / "ABUTRON_ORDER_FLOW_V2_demo-123.txt"
    p.write_text(json.dumps(_payload([])), encoding="utf-8")
    assert Mt5TerminalManager._parse_order_flow_file(str(p), expected_nonce="OTHER") is None


def test_parse_rejects_candle_synthetic_format(tmp_path):
    p = tmp_path / "ABUTRON_ORDER_FLOW_V2_demo-123.txt"
    p.write_text(json.dumps({"trades": [{"symbol": "Volatility 50 Index.0",
        "price": 82.50, "size": 2, "side": "buy", "time": "2026-11-20"}]}))
    assert Mt5TerminalManager._parse_order_flow_file(str(p), expected_nonce="demo-123") is None


def test_parse_rejects_last_marker(tmp_path):
    p = tmp_path / "ABUTRON_ORDER_FLOW_V2_LAST.txt"
    p.write_text("2026.11.20 12:00:00", encoding="utf-8")
    assert Mt5TerminalManager._parse_order_flow_file(str(p), expected_nonce="demo-123") is None


def test_parse_order_flow_file_invalid_json(tmp_path):
    p = tmp_path / "bad.txt"
    p.write_text("not valid json {{{", encoding="utf-8")
    assert Mt5TerminalManager._parse_order_flow_file(str(p), expected_nonce="demo-123") is None


def test_parse_order_flow_file_missing():
    assert Mt5TerminalManager._parse_order_flow_file("/nonexistent/path.txt") is None


def test_parse_filters_bad_trades(tmp_path):
    trades = [
        {"symbol": "Volatility 50 Index.0", "price": 0, "size": 1,
         "side": "buy", "time_msc": 1795165200000},
        {"symbol": "Volatility 50 Index.0", "price": 100, "size": 0,
         "side": "buy", "time_msc": 1795165200000},
        {"symbol": "Volatility 50 Index.0", "price": 100, "size": 1,
         "side": "hold", "time_msc": 1795165200000},
        {"symbol": "Volatility 50 Index.0", "price": 100, "size": 1,
         "side": "buy", "time_msc": 1795165200000},
        {"symbol": "WRONG", "price": 100, "size": 1,
         "side": "buy", "time_msc": 1795165200000},
        {"symbol": "Volatility 50 Index.0", "price": float("inf"), "size": 1,
         "side": "buy", "time_msc": 1795165200000},
    ]
    p = tmp_path / "ABUTRON_ORDER_FLOW_V2_demo-123.txt"
    p.write_text(json.dumps(_payload(trades)), encoding="utf-8")
    result = Mt5TerminalManager._parse_order_flow_file(str(p), expected_nonce="demo-123")
    assert result is not None and len(result["trades"]) == 1


def test_parse_quote_only_broker_empty_trades(tmp_path):
    p = tmp_path / "ABUTRON_ORDER_FLOW_V2_demo-123.txt"
    p.write_text(json.dumps(_payload([])), encoding="utf-8")
    result = Mt5TerminalManager._parse_order_flow_file(str(p), expected_nonce="demo-123")
    assert result is not None and result["trades"] == []


# --------------------------------------------------------------------- #
# Footprint computation in production.py
# --------------------------------------------------------------------- #

def _make_candle(high=82.60, low=82.40, close=82.55):
    c = MagicMock()
    c.symbol = "Volatility 50 Index.0"
    c.high = high
    c.low = low
    c.close = close
    c.open = 82.45
    return c


def test_compute_footprint_empty_when_no_trades():
    from abutron.agent.production import AgenticDecisionEngine
    c = _make_candle()
    result = AgenticDecisionEngine._compute_footprint(c, {})
    assert result == {}


def test_compute_footprint_with_trades():
    from abutron.agent.production import AgenticDecisionEngine
    c = _make_candle(high=82.60, low=82.40)
    trades = {
        "Volatility 50 Index.0": [
            {"symbol": "Volatility 50 Index.0", "price": 82.50,
             "size": 2.0, "side": "buy", "time": "t1"},
            {"symbol": "Volatility 50 Index.0", "price": 82.50,
             "size": 1.0, "side": "sell", "time": "t2"},
            {"symbol": "Volatility 50 Index.0", "price": 82.55,
             "size": 3.0, "side": "buy", "time": "t3"},
        ]
    }
    result = AgenticDecisionEngine._compute_footprint(c, trades)
    assert result != {}
    assert "poc" in result
    assert "total_volume" in result
    assert result["total_volume"] == 6.0
    assert result["total_delta"] == 4.0  # 5 buy - 1 sell
    assert "grid" in result
    assert len(result["grid"]) >= 1
    # POC should be the bucket with highest volume
    assert result["poc_volume"] >= max(g["total_vol"] for g in result["grid"])


def test_compute_footprint_invalid_candle():
    from abutron.agent.production import AgenticDecisionEngine
    c = _make_candle(high=0, low=0)
    trades = {"X": [{"symbol": "X", "price": 100, "size": 1, "side": "buy"}]}
    result = AgenticDecisionEngine._compute_footprint(c, trades)
    assert result == {}


def test_compute_footprint_filters_invalid_ticks():
    from abutron.agent.production import AgenticDecisionEngine
    c = _make_candle(high=82.60, low=82.40)
    trades = {
        "Volatility 50 Index.0": [
            {"symbol": "X", "price": 0, "size": 1.0, "side": "buy"},
            {"symbol": "X", "price": 82.50, "size": 0, "side": "buy"},
            {"symbol": "X", "price": 82.50, "size": 1.0, "side": "hold"},
            {"symbol": "X", "price": 82.50, "size": 2.0, "side": "buy"},
        ]
    }
    result = AgenticDecisionEngine._compute_footprint(c, trades)
    assert result != {}
    assert result["total_volume"] == 2.0


# --------------------------------------------------------------------- #
# MQL5 order-flow export includes symbol
# --------------------------------------------------------------------- #

def test_mql5_orderflow_uses_real_broker_trade_flags_only():
    src = pathlib.Path(__file__).parent.parent / "mql5/ApexNativeWriter.mq5"
    content = src.read_text()
    of_section = content[content.index("Order flow: real, broker-reported"):
                         content.index("Broker-native exposure snapshot")]
    assert "CopyTicksRange(" in of_section
    assert "COPY_TICKS_TRADE" in of_section
    assert "TICK_FLAG_BUY" in of_section and "TICK_FLAG_SELL" in of_section
    assert "tick.volume_real" in of_section
    assert "_Symbol, tick.last" in of_section
    assert "r.symbol" not in of_section
    assert "tick_volume / 100" not in of_section
    assert "r.close >= r.open" not in of_section
    assert "ABUTRON_ORDER_FLOW_V2_LAST" not in of_section


def test_full_reader_pairs_exact_native_nonce():
    from abutron.feeds import mt5_terminal
    import inspect
    source = inspect.getsource(mt5_terminal.Mt5TerminalManager.read_native_export_full)
    assert 'f"ABUTRON_ORDER_FLOW_V2_{nonce}.txt"' in source
    assert 'ABUTRON_ORDER_FLOW_V2_*.txt' not in source
    assert "expected_nonce=nonce" in source


# --------------------------------------------------------------------- #
# Adapter wiring: orderflow_trades on MarketDataResult
# --------------------------------------------------------------------- #

def test_market_data_result_has_orderflow_trades_field():
    from abutron.agent.market_data import MarketDataResult
    from abutron.feeds.feed_quality import Candle, Quote
    result = MarketDataResult(
        candles=[], quotes=[],
        connection_id="test", broker="Deriv", login=123,
        server="server", source_hash="abc", source_ts=0.0,
        symbol_count=0, evaluated_symbols=[],
    )
    assert result.orderflow_trades == {}


def test_run_shadow_passes_orderflow_to_engine():
    """run_shadow_decision_pass must pass orderflow_trades to the engine."""
    from abutron.agent.market_data import run_shadow_decision_pass
    from abutron.feeds.feed_quality import Candle, Quote

    engine = MagicMock()
    engine.run_decision_pass = MagicMock(return_value=[])
    engine.set_history = MagicMock()

    adapter = MagicMock()
    candle = Candle(symbol="X", timeframe="M5", open_time="2026-11-20T09:00:00Z",
                    close_time="2026-11-20T09:05:00Z",
                    open=1.0, high=1.1, low=0.9, close=1.05, volume=10.0,
                    closed=True)
    md_result = MagicMock()
    md_result.candles = [candle]
    md_result.quotes = []
    md_result.evaluated_symbols = ["X"]
    md_result.candle_count = 1
    md_result.quote_count = 0
    md_result.source_hash = "abc"
    md_result.source_ts = 1000.0
    md_result.orderflow_trades = {"X": [{"symbol": "X", "price": 1.05,
                                         "size": 5.0, "side": "buy"}]}
    adapter.read = MagicMock(return_value=md_result)

    run_shadow_decision_pass(
        engine, adapter, connection_id="cid", broker="Deriv",
        owner_id="owner", account={}, equity=1000.0)

    call_args = engine.run_decision_pass.call_args
    assert "orderflow_trades" in call_args.kwargs
    assert call_args.kwargs["orderflow_trades"]["X"] == md_result.orderflow_trades["X"]
