#!/usr/bin/env python3
"""V9 Tick Data Verification — prove the PROXY → VERIFIED transition.

Checks whether true broker tick/trade data is available and flows
through the signal pipeline, replacing candle-derived proxies.

The signal pipeline in ``brain/signals.py`` already supports real
tick trades via ``tick_trades`` and depth via ``depth``. This script:

1. Checks for MT5 export files in the MQL5/Files directory
2. If found, loads them via TickIngestion and reports true_tick_bars
3. If no MT5 export, checks for a CSV tick/trade file
4. Reports the honest status: VERIFIED (true tick data) or PROXY
   (candle-derived fallback)

Usage:
    PYTHONPATH=src python -u scripts/verify_tick_data.py \
        [--mql5-files MQL5/Files] \
        [--tick-csv data/links/tick_trades.csv] \
        [--symbol EURUSD.r]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from abutron.feeds.tick_ingestion import TickIngestion
from abutron.brain.signals import load_tick_trades, load_depth_signals


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mql5-files", default="MQL5/Files",
                    help="MQL5 Files directory with native exports")
    ap.add_argument("--tick-csv", default=None,
                    help="CSV of true tick/trade prints")
    ap.add_argument("--depth-csv", default=None,
                    help="CSV of market depth snapshots")
    ap.add_argument("--symbol", default="",
                    help="Filter by symbol (optional)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = {
        "order_flow_status": "PROXY",
        "footprint_status": "PROXY",
        "detail": {},
    }

    # --- Check MT5 native exports ---
    ingestion = TickIngestion(args.mql5_files)
    mt5_summary = ingestion.verification_summary()
    result["detail"]["mt5_export"] = mt5_summary

    tick_trades = {}
    depth_signals = {}

    if mt5_summary["verified"]:
        # True tick data from MT5 export
        tick_trades = ingestion.load_trades()
        depth_signals = ingestion.load_depth()
        result["order_flow_status"] = "VERIFIED"
        result["footprint_status"] = "VERIFIED"
        result["detail"]["source"] = "native_mt5_export"
        result["detail"]["tick_trades"] = {
            "bars_with_trades": len(tick_trades),
            "total_trades": sum(len(t) for t in tick_trades.values()),
        }
        result["detail"]["depth_snapshots"] = len(depth_signals)
    else:
        # --- Check CSV tick data ---
        if args.tick_csv and os.path.isfile(args.tick_csv):
            tick_trades = load_tick_trades(args.tick_csv,
                                           symbol=args.symbol)
            total = sum(len(v) for v in tick_trades.values())
            if total > 0:
                result["order_flow_status"] = "VERIFIED"
                result["footprint_status"] = "VERIFIED"
                result["detail"]["source"] = "csv_tick_data"
                result["detail"]["tick_trades"] = {
                    "bars_with_trades": len(tick_trades),
                    "total_trades": total,
                }
            else:
                result["detail"]["tick_csv"] = "no trades loaded"

        if args.depth_csv and os.path.isfile(args.depth_csv):
            depth_signals = load_depth_signals(args.depth_csv,
                                               symbol=args.symbol)
            result["detail"]["depth_snapshots"] = len(depth_signals)
            if depth_signals and result["order_flow_status"] == "PROXY":
                result["order_flow_status"] = "PARTIAL"
                result["detail"]["source"] = "depth_only"

    if not args.json:
        print(f"=== V9 Tick Data Verification ===")
        print(f"MT5 export dir: {args.mql5_files}")
        if mt5_summary["verified"]:
            print(f"  MT5 exports: {mt5_summary['bars_with_trades']} bars "
                  f"with trades, {mt5_summary['total_trade_events']} "
                  f"trade events")
        else:
            print(f"  MT5 exports: none found")
        if tick_trades:
            print(f"  Tick trades: {len(tick_trades)} bars, "
                  f"{sum(len(t) for t in tick_trades.values())} trades")
        if depth_signals:
            print(f"  Depth snapshots: {len(depth_signals)}")
        print(f"\nOrder flow / imbalance / delta : {result['order_flow_status']}")
        print(f"Volume footprint / POC         : {result['footprint_status']}")
    else:
        print(json.dumps(result, indent=2))

    # Exit non-zero if still PROXY (indicates verification not yet done)
    return 0 if result["order_flow_status"] == "VERIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
