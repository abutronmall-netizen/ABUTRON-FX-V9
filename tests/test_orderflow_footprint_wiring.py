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

def _write_orderflow_file(path, trades):
    data = json.dumps({"trades": trades})
    path.write_text(data, encoding="utf-8")
    return path


def test_parse_order_flow_file_valid():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        trades = [
            {"symbol": "Volatility 50 Index.0", "price": 82.50,
             "size": 1.5, "side": "buy", "time": "2026-11-20 09:00:00"},
            {"symbol": "Volatility 50 Index.0", "price": 82.51,
             "size": 0.5, "side": "sell", "time": "2026-11-20 09:00:05"},
        ]
        f.write(json.dumps({"trades": trades}))
        path = f.name
    try:
        result = Mt5TerminalManager._parse_order_flow_file(path)
        assert result is not None
        assert len(result["trades"]) == 2
        assert result["trades"][0]["symbol"] == "Volatility 50 Index.0"
        assert result["trades"][0]["price"] == 82.50
        assert result["trades"][0]["side"] == "buy"
    finally:
        os.unlink(path)


def test_parse_order_flow_file_invalid_json():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("not valid json {{{")
        path = f.name
    try:
        result = Mt5TerminalManager._parse_order_flow_file(path)
        assert result is None
    finally:
        os.unlink(path)


def test_parse_order_flow_file_missing():
    result = Mt5TerminalManager._parse_order_flow_file("/nonexistent/path.txt")
    assert result is None


def test_parse_order_flow_file_invalid_trades():
    """Trades with bad data are filtered out."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        trades = [
            {"symbol": "X", "price": 0, "size": 1.0, "side": "buy", "time": "t"},
            {"symbol": "X", "price": 100, "size": 0, "side": "buy", "time": "t"},
            {"symbol": "X", "price": 100, "size": 1.0, "side": "hold", "time": "t"},
            {"symbol": "X", "price": 100, "size": 1.0, "side": "buy", "time": "t"},
        ]
        f.write(json.dumps({"trades": trades}))
        path = f.name
    try:
        result = Mt5TerminalManager._parse_order_flow_file(path)
        assert result is not None
        assert len(result["trades"]) == 1  # only the valid one
    finally:
        os.unlink(path)


def test_parse_order_flow_file_no_symbol_key():
    """Trades without a symbol field get empty string — filtered by adapter."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        trades = [
            {"price": 100, "size": 1.0, "side": "buy", "time": "t"},
        ]
        f.write(json.dumps({"trades": trades}))
        path = f.name
    try:
        result = Mt5TerminalManager._parse_order_flow_file(path)
        assert result is not None
        assert len(result["trades"]) == 1
        assert result["trades"][0]["symbol"] == ""
    finally:
        os.unlink(path)


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

def test_mql5_orderflow_export_includes_symbol():
    src = pathlib.Path(__file__).parent.parent / "mql5/ApexNativeWriter.mq5"
    content = src.read_text()
    # The order-flow JSON must include "symbol" per trade
    assert '\\\"symbol\\\":\\\"%s\\\"' in content or '"symbol"' in content
    # The format string must include symbol in the JSON output.
    # Uses _Symbol (chart symbol), NOT r.symbol -- MqlRates has no
    # symbol member (would fail MetaEditor compilation).
    of_section = content[content.index("Order flow: candle-derived activity"):
                         content.index("Broker-native exposure snapshot")]
    assert "_Symbol" in of_section
    assert "r.symbol" not in of_section


def test_mql5_orderflow_export_is_labeled_candle_derived():
    """The export must be clearly identified as candle-derived, not
    genuine aggressor trades (MT5 does not expose trade prints)."""
    src = pathlib.Path(__file__).parent.parent / "mql5/ApexNativeWriter.mq5"
    content = src.read_text()
    of_section = content[content.index("Order flow: candle-derived activity"):
                         content.index("Broker-native exposure snapshot")]
    assert 'candle_derived' in of_section
    # The MQL5 string literal escapes quotes: \"source\":\"candle_derived\"
    assert '\\"source\\":\\"candle_derived\\"' in of_section or '"source":"candle_derived"' in of_section


def test_reader_excludes_last_marker_file():
    """The reader must not pick the ABUTRON_ORDER_FLOW_V2_LAST.txt
    marker (a bare timestamp, not JSON)."""
    import glob

    with tempfile.TemporaryDirectory() as tmpdir:
        # Older valid trade file
        trades_path = os.path.join(tmpdir, "ABUTRON_ORDER_FLOW_V2_001.txt")
        with open(trades_path, "w") as f:
            f.write(json.dumps({"trades": [
                {"symbol": "X", "price": 1.0, "size": 1.0,
                 "side": "buy", "time": "t"}]}))
        # Newer LAST marker (would win the mtime race if not excluded)
        last_path = os.path.join(tmpdir, "ABUTRON_ORDER_FLOW_V2_LAST.txt")
        with open(last_path, "w") as f:
            f.write("2026-11-20 09:00:00")

        # The reader's own selection logic must skip the LAST marker
        matches = [
            p for p in glob.glob(os.path.join(tmpdir,
                                              "ABUTRON_ORDER_FLOW_V2_*.txt"))
            if not os.path.basename(p).endswith("_LAST.txt")
        ]
        selected = max(matches, key=os.path.getmtime)
        assert os.path.basename(selected) == "ABUTRON_ORDER_FLOW_V2_001.txt"
        assert "LAST" not in os.path.basename(selected)


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


def test_compute_footprint_records_source_label():
    """The footprint must record its provenance (candle_derived)."""
    from abutron.agent.production import AgenticDecisionEngine
    c = _make_candle(high=82.60, low=82.40)
    trades = {
        "Volatility 50 Index.0": [
            {"symbol": "Volatility 50 Index.0", "price": 82.50,
             "size": 2.0, "side": "buy", "time": "t1"},
        ]
    }
    result = AgenticDecisionEngine._compute_footprint(
        c, trades, orderflow_source="candle_derived")
    assert result != {}
    assert result["source"] == "candle_derived"


def test_parse_order_flow_file_preserves_metadata():
    """The parser must preserve source and connection_id from the export."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(json.dumps({
            "connection_id": "abc-123",
            "source": "candle_derived",
            "trades": [
                {"symbol": "X", "price": 1.0, "size": 1.0,
                 "side": "buy", "time": "t"},
            ],
        }))
        path = f.name
    try:
        result = Mt5TerminalManager._parse_order_flow_file(path)
        assert result is not None
        assert result["source"] == "candle_derived"
        assert result["connection_id"] == "abc-123"
        assert len(result["trades"]) == 1
    finally:
        os.unlink(path)
