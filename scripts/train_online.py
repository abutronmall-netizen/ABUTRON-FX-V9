"""Online-train the unified brain — self-train the saved brain from outcomes.

Loads the offline-trained unified brain and streams every symbol's M5
history through its online learner (one causal gradient step per bar
from the just-completed move), then saves the refined weights back.
This bakes real online-learned updates into the brain file so the
self-training survives restarts and keeps improving.

This is genuine online learning: each bar's realised move updates the
neural weights via a small-lr Adam step — the same path the live engine
fires on every closed candle. Running it over history pre-warms the
online component so the brain starts live trading already refined.

Usage:
    PYTHONPATH=src python -m scripts.train_online \
        [--data /tmp/market-data] [--timeframe M5] [--lr 1e-3] \
        [--in data/brains/unified_m5] [--out data/brains/unified_m5]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import numpy as np

from abutron.backtest.loader import MarketDataLoader
from abutron.brain.unified import (
    SYMBOL_UNIVERSE, load_unified, save_unified, unified_brain_dir)


def load_symbol_candles(loader: MarketDataLoader, symbol: str, timeframe: str):
    d = loader.load_deriv(symbol=symbol, timeframe=timeframe)
    if d:
        return d
    c, _ = loader.load_fp_markets(symbol=symbol, timeframe=timeframe)
    return c


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="/tmp/market-data")
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--lr", type=float, default=1e-3,
                    help="online learning rate (small, to refine safely)")
    ap.add_argument("--in", dest="in_dir", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--min-bars", type=int, default=30,
                    help="min bars before online learning starts")
    args = ap.parse_args()

    in_dir = args.in_dir or unified_brain_dir()
    out_dir = args.out or in_dir

    brain = load_unified(in_dir)
    if brain is None:
        print(f"ERROR: no unified brain at {in_dir}. "
              f"Run scripts/train_unified.py first.")
        return 1
    print(f"Loaded unified brain: {len(brain.ensemble.members)} members, "
          f"{len(brain.symbol_ids)} symbols")
    print(f"online_enabled={brain.online_enabled} lr={brain.nn_online_lr} "
          f"updates_so_far={brain.online_updates}")
    brain.nn_online_lr = args.lr

    loader = MarketDataLoader(args.data)
    total_updates = 0
    t0 = time.time()
    for sym in SYMBOL_UNIVERSE:
        candles = load_symbol_candles(loader, sym, args.timeframe)
        if len(candles) < args.min_bars:
            continue
        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        # stream bars causally: features as of bar i-1, realised move of bar i
        n = 0
        for i in range(args.min_bars, len(closes)):
            prev_close = closes[i - 1]
            brain.online_learn(
                symbol=sym, closes=closes[:i], highs=highs[:i],
                lows=lows[:i], realised_return=closes[i] - prev_close)
            n += 1
        total_updates += n
        print(f"  {sym:24} online steps={n}")
    dt = time.time() - t0
    print(f"\nOnline training done in {dt:.1f}s: "
          f"{total_updates} updates across the universe")
    print(f"  brain.online_updates now = {brain.online_updates}")

    os.makedirs(out_dir, exist_ok=True)
    path = save_unified(out_dir, brain)
    print(f"Refined brain saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
