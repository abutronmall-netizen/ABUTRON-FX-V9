"""Train the UNIFIED brain — all symbols as ONE symbol-conditioned brain.

Pools every symbol's M5 candles into a single training set, augments each
sample with a symbol one-hot, and trains one 3-member MLP ensemble that
handles all 26 instruments. The result (``data/brains/unified_m5/
nexus_brain.json``) is loaded by the live engine in place of the 26
per-symbol brains — "all brains must work as one brain."

Usage:
    PYTHONPATH=src python -m scripts.train_unified \
        [--data /tmp/market-data] [--timeframe M5] [--epochs 120] \
        [--out data/brains/unified_m5] [--top N]

``--top N`` optionally restricts pooling to the N highest-profit symbols
(by forward-test expectancy) to focus the unified brain where the edge
is; default pools the full universe.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.backtest.loader import MarketDataLoader
from abutron.brain.unified import (
    SYMBOL_UNIVERSE, UnifiedBrain, build_unified_ensemble,
    n_unified_features, prepare_unified_samples, save_unified,
    train_unified, unified_brain_dir)


def load_symbol_candles(loader: MarketDataLoader, symbol: str,
                        timeframe: str):
    """Load M5 candles for one symbol from either broker."""
    d = loader.load_deriv(symbol=symbol, timeframe=timeframe)
    if d:
        return d
    c, _ = loader.load_fp_markets(symbol=symbol, timeframe=timeframe)
    return c


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="/tmp/market-data")
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--epochs", type=int, default=120)
    ap.add_argument("--out", default=None)
    ap.add_argument("--top", type=int, default=0,
                    help="pool only the top-N profitable symbols (0 = all)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    loader = MarketDataLoader(args.data)
    print(f"Training UNIFIED brain on {args.timeframe} ...\n")

    # discover available symbols
    discovered = []
    for sym in SYMBOL_UNIVERSE:
        candles = load_symbol_candles(loader, sym, args.timeframe)
        if candles:
            discovered.append((sym, candles))
    if not discovered:
        print("ERROR: no market data found. Check --data path.")
        return 1
    print(f"{len(discovered)} symbols with {args.timeframe} data.")

    # optionally focus on the top-N most-profitable symbols
    if args.top > 0 and args.top < len(discovered):
        # rank by simple forward-test expectancy from the registry if present
        try:
            from abutron.apex.ranker import expectancy_r
            from abutron.apex.eligibility import SYMBOL_REGISTRY
            keys = {}
            for s in SYMBOL_UNIVERSE:
                prof = SYMBOL_REGISTRY.get(s)
                if prof is not None:
                    keys[s] = expectancy_r(
                        prof.validation.test_win_rate,
                        prof.validation.test_pf)
            available = {s for s, _ in discovered}
            ranked = sorted(available, key=lambda s: keys.get(s, 0.0),
                            reverse=True)[:args.top]
            discovered = [(s, c) for s, c in discovered if s in ranked]
            print(f"Pooling top-{args.top} most-profitable symbols: "
                  f"{', '.join(s for s, _ in discovered)}")
        except Exception as exc:  # pragma: no cover
            print(f"(top-N ranking skipped: {exc})")

    per_symbol = {}
    total = 0
    for sym, candles in discovered:
        per_symbol[sym] = {
            "closes": [c.close for c in candles],
            "highs": [c.high for c in candles],
            "lows": [c.low for c in candles],
        }
        total += len(candles)
    print(f"Pooled {total} candles across {len(per_symbol)} symbols.\n")

    ensemble = build_unified_ensemble(seed=args.seed)
    brain = UnifiedBrain(ensemble=ensemble)
    samples = prepare_unified_samples(per_symbol)
    # group the pooled samples back by symbol for per-symbol walk-forward
    # (each sample's symbol one-hot is the trailing len(SYMBOL_UNIVERSE)
    # block of its feature vector).
    import numpy as np
    by_symbol: dict[str, list] = {s: [] for s in per_symbol}
    nh = len(SYMBOL_UNIVERSE)
    for smp in samples:
        onehot = smp.features[len(smp.features) - nh:]
        idx = int(np.argmax(onehot))
        by_symbol[SYMBOL_UNIVERSE[idx]].append(smp)

    t0 = time.time()
    report = train_unified(ensemble, by_symbol, epochs=args.epochs,
                           seed=args.seed)
    dt = time.time() - t0

    print(f"\nUnified training done in {dt:.1f}s")
    print(f"  samples={report.n_samples} features={n_unified_features()}")
    print(f"  val_acc={report.val_accuracy:.3f} "
          f"val_dir_acc={report.val_direction_accuracy:.3f} "
          f"loss={report.final_loss:.4f}")
    for sym, acc in report.per_symbol_accuracy.items():
        print(f"    {sym:24} val_acc={acc:.3f}")

    out_dir = args.out or unified_brain_dir()
    os.makedirs(out_dir, exist_ok=True)
    path = save_unified(out_dir, brain, report=report)
    print(f"\nUnified brain saved: {path}")
    with open(os.path.join(out_dir, "report.json"), "w") as fh:
        json.dump(report.to_dict(), fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
