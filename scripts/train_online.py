"""Causal online training of the unified brain with full signal features.

Streams each symbol's history through the same 23 market features used at
inference (order flow, footprint, regime and agent/Gemma scores), takes one
small Adam step toward the move that just completed, and persists the refined
weights plus online state. No future bar is used to build a feature row.

Usage:
    PYTHONPATH=src python -m scripts.train_online \
        [--data data/links/market-data] [--timeframe M5] [--lr 1e-3] \
        [--in data/brains/unified_m5] [--out data/brains/unified_m5] \
        [--max-bars-per-symbol 0] [--stride 1]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.backtest.loader import MarketDataLoader
from abutron.brain.signals import (SIGNAL_DATA_VERSION, build_signal_bundle,
                                   load_depth_signals, load_tick_trades,
                                   online_learn_from_bundle)
from abutron.brain.unified import (load_unified, save_unified,
                                   unified_brain_dir)
from abutron.config import (DEPTH_SNAPSHOTS_PATH, GEMMA_ENABLED,
                            MARKET_DATA_DIR, TICK_TRADES_PATH)


def load_all_candles(loader: MarketDataLoader, timeframe: str
                     ) -> Dict[str, List[Any]]:
    """Read both broker gzip streams ONCE and group by symbol."""
    out: Dict[str, List[Any]] = {}
    for candles in (loader.load_deriv(timeframe=timeframe),
                    loader.load_fp_markets(timeframe=timeframe)[0]):
        for c in candles:
            out.setdefault(c.symbol, []).append(c)
    for candles in out.values():
        candles.sort(key=lambda c: c.open_time)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=MARKET_DATA_DIR)
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--in", dest="in_dir", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--min-bars", type=int, default=30)
    ap.add_argument("--max-bars-per-symbol", type=int, default=0)
    ap.add_argument("--stride", type=int, default=1,
                    help="use every Nth closed bar (1 = all bars)")
    ap.add_argument("--ticks", default=TICK_TRADES_PATH)
    ap.add_argument("--depth", default=DEPTH_SNAPSHOTS_PATH)
    ap.add_argument("--gemma", action="store_true")
    args = ap.parse_args()

    in_dir = args.in_dir or unified_brain_dir()
    out_dir = args.out or in_dir
    brain = load_unified(in_dir)
    if brain is None:
        print(f"ERROR: no unified brain at {in_dir}. "
              "Run scripts/train_unified.py first.")
        return 1
    brain.online_enabled = True
    brain.nn_online_lr = args.lr
    print(f"Loaded unified brain: {len(brain.ensemble.members)} members, "
          f"{len(brain.symbol_ids)} symbols")
    print(f"online_enabled={brain.online_enabled} lr={brain.nn_online_lr} "
          f"updates_so_far={brain.online_updates}")

    loader = MarketDataLoader(args.data)
    all_candles = load_all_candles(loader, args.timeframe)
    total_updates = 0
    per_symbol: Dict[str, Any] = {}
    t0 = time.time()
    for sym in brain.symbol_ids:
        candles = all_candles.get(sym, [])
        if args.max_bars_per_symbol > 0:
            candles = candles[-args.max_bars_per_symbol:]
        if len(candles) < args.min_bars + 1:
            continue
        ticks = load_tick_trades(args.ticks, symbol=sym) if args.ticks else {}
        depth = load_depth_signals(args.depth, symbol=sym) if args.depth else {}
        bundle = build_signal_bundle(
            candles, symbol=sym, tick_trades=ticks, depth=depth,
            use_gemma=bool(args.gemma or GEMMA_ENABLED))
        n = 0
        for i in range(max(args.min_bars, 30), len(bundle.closes)):
            if args.stride > 1 and i % args.stride:
                continue
            realised = bundle.closes[i] - bundle.closes[i - 1]
            result = online_learn_from_bundle(
                brain, symbol=sym, bundle=bundle, index=i - 1,
                realised_return=realised)
            if result.get("updated"):
                n += 1
        total_updates += n
        per_symbol[sym] = {
            "bars": bundle.n_bars, "updates": n,
            "signal_sources": bundle.sources,
            "all_signal_slots_live": bundle.audit["all_slots_nonzero"],
        }
        print(f"  {sym:24} online steps={n}")
    dt = time.time() - t0
    print(f"\nOnline training done in {dt:.1f}s: "
          f"{total_updates} updates across the universe")
    print(f"  brain.online_updates now = {brain.online_updates}")

    os.makedirs(out_dir, exist_ok=True)
    path = save_unified(out_dir, brain)
    report = {
        "version": SIGNAL_DATA_VERSION,
        "stage": "causal_online_bar_training",
        "updates": total_updates,
        "lr": args.lr,
        "stride": args.stride,
        "elapsed_s": round(dt, 3),
        "per_symbol": per_symbol,
        "online_updates_total": brain.online_updates,
    }
    with open(os.path.join(out_dir, "online_report.json"), "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"Refined brain saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
