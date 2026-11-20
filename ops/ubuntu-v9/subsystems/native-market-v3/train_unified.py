"""Train the UNIFIED brain with every available causal signal channel.

Pools all symbols into one symbol-conditioned ensemble. Unlike the original
script, it does **not** pass empty order-flow / footprint / agent-score dicts:
``build_signal_bundle`` generates true tick/depth features when those files
are supplied, otherwise explicitly-labelled OHLCV proxies, and optionally
applies a Gemma agent-score overlay.

The command fine-tunes the checked-in unified brain by default, preserving
its online-learning history. Use ``--fresh`` to train a new ensemble.

Usage:
    PYTHONPATH=src python -m scripts.train_unified \
        [--data data/links/market-data] [--timeframe M5] [--epochs 12] \
        [--batch-size 256] [--out data/brains/unified_m5] \
        [--ticks path/to/trades.csv.gz] [--depth path/to/depth.csv.gz] \
        [--gemma] [--max-bars-per-symbol 0]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.backtest.loader import MarketDataLoader
from abutron.brain.signals import (SIGNAL_DATA_VERSION, audit_market_features,
                                   build_signal_bundle, load_depth_signals,
                                   load_tick_trades)
from abutron.brain.unified import (
    SYMBOL_UNIVERSE, UnifiedBrain, build_unified_ensemble, load_unified,
    n_unified_features, prepare_unified_samples, save_unified, train_unified,
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


def aggregate_audits(audits: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Combine per-symbol non-zero coverage into one auditable report."""
    rows = sum(int(a.get("rows", 0) or 0) for a in audits.values())
    slots: Dict[str, Dict[str, float]] = {}
    for audit in audits.values():
        for name, stat in (audit.get("slots") or {}).items():
            slot = slots.setdefault(name, {
                "nonzero_rows": 0.0, "mean": 0.0, "min": 1e9, "max": -1e9,
            })
            n = int(audit.get("rows", 0) or 0)
            slot["nonzero_rows"] += n * float(stat.get("nonzero_ratio", 0))
            slot["mean"] += n * float(stat.get("mean", 0))
            slot["min"] = min(slot["min"], float(stat.get("min", 0)))
            slot["max"] = max(slot["max"], float(stat.get("max", 0)))
    out_slots = {}
    for name, stat in slots.items():
        out_slots[name] = {
            "index": audit_market_features([])["slots"][name]["index"],
            "nonzero_ratio": round(stat["nonzero_rows"] / rows if rows else 0.0, 6),
            "mean": round(stat["mean"] / rows if rows else 0.0, 6),
            "min": round(stat["min"] if rows else 0.0, 6),
            "max": round(stat["max"] if rows else 0.0, 6),
        }
    return {
        "version": SIGNAL_DATA_VERSION,
        "rows": rows,
        "symbols": len(audits),
        "slots": out_slots,
        "all_slots_nonzero": all(
            v["nonzero_ratio"] > 0.0 for v in out_slots.values()),
        "per_symbol": audits,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=MARKET_DATA_DIR)
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch-size", type=int, default=256)
    ap.add_argument("--horizon", type=int, default=1)
    ap.add_argument("--out", default=None)
    ap.add_argument("--init", default=None,
                    help="existing unified brain to fine-tune (default: data/brains/unified_m5)")
    ap.add_argument("--fresh", action="store_true",
                    help="build a fresh ensemble instead of fine-tuning")
    ap.add_argument("--top", type=int, default=0,
                    help="pool only the top-N profitable symbols (0 = all)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-bars-per-symbol", type=int, default=0,
                    help="use only the last N bars per symbol (0 = all)")
    ap.add_argument("--ticks", default=TICK_TRADES_PATH,
                    help="optional true aggressor tick trades CSV(.gz)")
    ap.add_argument("--depth", default=DEPTH_SNAPSHOTS_PATH,
                    help="optional depth snapshot CSV(.gz)")
    ap.add_argument("--gemma", action="store_true",
                    help="attempt the configured Gemma score gateway")
    args = ap.parse_args()

    in_dir = args.init or unified_brain_dir()
    out_dir = args.out or in_dir
    if args.fresh:
        brain = UnifiedBrain(ensemble=build_unified_ensemble(seed=args.seed))
        print("Building a FRESH unified ensemble.")
    else:
        brain = load_unified(in_dir)
        if brain is None:
            brain = UnifiedBrain(
                ensemble=build_unified_ensemble(seed=args.seed))
            print("No existing unified brain found; building a fresh one.")
        else:
            print(f"Fine-tuning unified brain from {in_dir} "
                  f"(online_updates={brain.online_updates}).")
    universe = list(brain.symbol_ids)

    loader = MarketDataLoader(args.data)
    print(f"Training UNIFIED brain on {args.timeframe} from {args.data}\n")
    all_candles = load_all_candles(loader, args.timeframe)
    discovered = []
    for sym in SYMBOL_UNIVERSE:
        if sym not in universe:
            continue
        candles = all_candles.get(sym, [])
        if not candles:
            continue
        if args.max_bars_per_symbol > 0:
            candles = candles[-args.max_bars_per_symbol:]
        if candles:
            discovered.append((sym, candles))
    if not discovered:
        print("ERROR: no market data found. Run scripts/link_all.sh or check --data.")
        return 1
    print(f"{len(discovered)} symbols with {args.timeframe} data.")

    if args.top > 0 and args.top < len(discovered):
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
            print(f"Pooling top-{args.top}: "
                  f"{', '.join(s for s, _ in discovered)}")
        except Exception as exc:
            print(f"(top-N ranking skipped: {exc})")

    use_gemma = bool(args.gemma or GEMMA_ENABLED)
    per_symbol: Dict[str, Dict[str, Any]] = {}
    sources: Dict[str, Any] = {}
    audits: Dict[str, Any] = {}
    total = 0
    for sym, candles in discovered:
        ticks = load_tick_trades(args.ticks, symbol=sym) if args.ticks else {}
        depth = load_depth_signals(args.depth, symbol=sym) if args.depth else {}
        bundle = build_signal_bundle(
            candles, symbol=sym, tick_trades=ticks, depth=depth,
            use_gemma=use_gemma)
        per_symbol[sym] = bundle.training_dict()
        sources[sym] = bundle.sources
        audits[sym] = bundle.audit
        total += bundle.n_bars
        cover = bundle.audit["all_slots_nonzero"]
        print(f"  {sym:24} bars={bundle.n_bars:6} "
              f"all_signal_slots_live={cover} "
              f"of={bundle.sources['orderflow']} "
              f"gemma={bundle.sources['gemma']['source']}")
    print(f"Pooled {total} candles across {len(per_symbol)} symbols.\n")

    samples = prepare_unified_samples(
        per_symbol, horizon=args.horizon, universe=universe)
    by_symbol: Dict[str, List[Any]] = {s: [] for s in per_symbol}
    nh = len(universe)
    for smp in samples:
        onehot = smp.features[len(smp.features) - nh:]
        idx = int(np.argmax(onehot))
        sym = universe[idx] if 0 <= idx < nh else ""
        if sym in by_symbol:
            by_symbol[sym].append(smp)

    t0 = time.time()
    report = train_unified(
        brain.ensemble, by_symbol, epochs=args.epochs,
        batch_size=args.batch_size, seed=args.seed)
    dt = time.time() - t0
    audit = aggregate_audits(audits)
    brain.signal_sources = {
        "version": SIGNAL_DATA_VERSION,
        "per_symbol": sources,
        "generated_by": "scripts/train_unified.py",
    }
    brain.feature_audit = audit
    report.__dict__["signal_data_version"] = SIGNAL_DATA_VERSION
    report.__dict__["signal_sources"] = brain.signal_sources
    report.__dict__["feature_audit"] = {
        k: v for k, v in audit.items() if k != "per_symbol"}

    print(f"\nUnified training done in {dt:.1f}s")
    print(f"  samples={report.n_samples} features={n_unified_features(universe)}")
    print(f"  val_acc={report.val_accuracy:.3f} "
          f"val_dir_acc={report.val_direction_accuracy:.3f} "
          f"loss={report.final_loss:.4f}")
    print(f"  all_signal_slots_live={audit['all_slots_nonzero']}")
    for sym, acc in report.per_symbol_accuracy.items():
        print(f"    {sym:24} val_acc={acc:.3f}")

    os.makedirs(out_dir, exist_ok=True)
    path = save_unified(out_dir, brain, report=report)
    with open(os.path.join(out_dir, "report.json"), "w") as fh:
        json.dump(report.to_dict(), fh, indent=2, default=str)
    with open(os.path.join(out_dir, "feature_audit.json"), "w") as fh:
        json.dump(audit, fh, indent=2, default=str)
    with open(os.path.join(out_dir, "signal_sources.json"), "w") as fh:
        json.dump(brain.signal_sources, fh, indent=2, default=str)
    print(f"\nUnified brain saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
