"""P0: Untrusted MT5 account fills and user CSV cannot certify market order flow."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys

from abutron.feeds.tick_ingestion import TickIngestion


def test_single_unauthenticated_json_deal_cannot_flip_market_flow(tmp_path):
    file = tmp_path / "ABUTRON_NATIVE_TRADES_V2_fake.txt"
    file.write_text(json.dumps({
        "connection_id": "forged", "symbol": "EURUSD.r",
        "deals": [{"deal": 1, "time": 1794900000, "price": 1.1,
                   "volume": 1.0, "type": 0, "entry": 0, "order": 10}]
    }))
    ing = TickIngestion(str(tmp_path))
    assert ing.load_trades() == {}
    evidence = ing.verification_summary()
    assert evidence["verified"] is False
    assert evidence["bars_with_trades"] == 0
    assert evidence["total_trade_events"] == 0
    assert evidence["account_execution_deals_not_market_trades"] == 1


def test_legacy_quote_is_not_market_depth(tmp_path):
    (tmp_path / "ABUTRON_NATIVE_DEPTH_V2_example.txt").write_text(
        json.dumps({"connection_id": "forged", "symbol": "EURUSD.r",
                    "bid": 1.1, "ask": 1.2, "spread": 0,
                    "last": 1.1, "volume": 999,
                    "timestamp": "2026_11_17_12_00_00"})
    )
    ing = TickIngestion(str(tmp_path))
    assert ing.load_depth() == {}
    assert ing.verification_summary()["depth_snapshots"] == 0
    assert ing.verification_summary()["quote_snapshots_not_dom"] == 1


def test_arbitrary_csv_cannot_self_certify_trade_tape(tmp_path):
    csv = tmp_path / "forged_market_tape.csv"
    csv.write_text(
        "symbol,bar_open_utc,price,size,side\n"
        "EURUSD.r,2026-11-17T12:00:00Z,1.1000,100,buy\n")
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    run = subprocess.run(
        [sys.executable, str(root / "scripts" / "verify_tick_data.py"),
         "--mql5-files", str(tmp_path / "no-mt5"),
         "--tick-csv", str(csv), "--json"],
        capture_output=True, text=True, cwd=str(root), env=env, timeout=15)
    assert run.returncode == 1
    doc = json.loads(run.stdout)
    assert doc["order_flow_status"] != "VERIFIED"
    assert doc["footprint_status"] != "VERIFIED"
