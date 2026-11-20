#!/usr/bin/env python3
"""Train the brain from PAPER-TRADE (backtest) outcomes — close the loop.

The user directive: *"backtest and forward and paper trades results that
train brain must learn the losses trade and never repeat those losing
trades."*

This script runs the backtest engine over the market data, captures every
trade's decision-time feature vector + realised outcome, and feeds them
back into the brain as an additional online + offline training stage. The
brain therefore learns not only from real broker history and per-bar
moves, but also from its own paper-trade results — including the losing
trades, so it can avoid repeating them.

Usage:
    PYTHONPATH=src python -u scripts/train_from_paper.py \
        --data /tmp/market-data \
        [--symbols EURUSD.r AUDUSD.r ...] \
        [--epochs 15] [--mode online|offline|both] \
        [--in data/brains/unified_m5] [--out data/brains/unified_m5]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import json
from typing import Dict, List, Tuple

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from abutron.brain.unified import load_unified, save_unified, unified_brain_dir
from abutron.backtest.loader import MarketDataLoader
from abutron.backtest.engine import BacktestEngine
from abutron.backtest.run import _print_report  # noqa: F401
from abutron.apex.sniper import SniperEntryEngine


def collect_paper_trades(brain, loader, symbols: List[str], *,
                         equity: float = 1000.0, risk_pct: float = 0.01,
                         htf: str = "H1") -> Tuple[List, List[str]]:
    """Run backtests and collect (features, label) for every closed trade."""
    sniper = SniperEntryEngine()
    engine = BacktestEngine(brain, sniper=sniper, equity=equity,
                            risk_pct=risk_pct)
    feats_list: List[np.ndarray] = []
    labels: List[str] = []
    sym_count = 0
    for sym in symbols:
        # try both brokers
        candles = []
        for broker in ("deriv", "fpmarkets"):
            try:
                if broker == "deriv":
                    c = loader.load_deriv(symbol=sym, timeframe="M5")
                else:
                    c, _ = loader.load_fp_markets(symbol=sym, timeframe="M5")
                if c:
                    candles = c
                    break
            except Exception:
                continue
        if not candles or len(candles) < 60:
            continue
        htf_candles = []
        try:
            if broker == "deriv":
                htf_candles = loader.load_deriv(symbol=sym, timeframe=htf)
            else:
                htf_candles, _ = loader.load_fp_markets(symbol=sym,
                                                        timeframe=htf)
        except Exception:
            htf_candles = []
        result = engine.run(candles, [], symbol=sym, timeframe="M5",
                            htf_candles=htf_candles,
                            forward_test=False, forward_frac=0.2)
        for d in (result.details or []):
            # reconstruct features at entry using the trade's symbol/entry_time
            # (simplified: use the entry price as the close and a short window)
            # The engine captured prop.features at decision time; we re-derive
            # from the trade record for a faithful replay.
            feats = _reconstruct_features(brain, sym, candles, d)
            if feats is None:
                continue
            pnl = d.get("pnl", 0.0)
            feats_list.append(feats)
            labels.append(str(pnl))
        sym_count += 1
        print(f"  {sym:22} trades={len(result.details or [])} "
              f"pnl={result.total_pnl:+.2f}")
    return feats_list, labels


def _reconstruct_features(brain, sym, candles, trade_detail):
    """Reconstruct the decision-time feature vector for a paper trade.

    Uses the trade's entry_time to find the bar and builds the enhanced
    feature vector from the trailing window (same as the engine did at
    decision time). Returns None if the window is too short.
    """
    from abutron.scripts_helper import _find_bar  # may not exist
    return None  # placeholder — replaced by direct feature capture below


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="market-data directory")
    ap.add_argument("--symbols", nargs="*", default=None,
                    help="symbols to paper-trade (default: all 26)")
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--mode", default="both",
                    choices=["online", "offline", "both"])
    ap.add_argument("--in", dest="in_dir", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--risk-pct", type=float, default=0.01)
    args = ap.parse_args(argv)

    in_dir = args.in_dir or unified_brain_dir()
    out_dir = args.out or in_dir
    brain = load_unified(in_dir)
    if brain is None:
        print(f"ERROR: no unified brain at {in_dir}.")
        return 1
    print(f"Loaded brain: {len(brain.ensemble.members)} members, "
          f"{len(brain.symbol_ids)} symbols, "
          f"online_updates={brain.online_updates}")

    loader = MarketDataLoader(args.data)
    symbols = args.symbols or list(brain.symbol_ids)
    print(f"\nRunning paper trades on {len(symbols)} symbols to collect "
          f"feedback trades...")

    # --- Phase 1: run paper trades and capture features+outcomes ---
    # We use the engine's per-trade feature capture directly.
    feats_list, label_list, pnl_list = _run_and_collect(
        brain, loader, symbols, equity=args.equity, risk_pct=args.risk_pct)

    if not feats_list:
        print("No paper trades to learn from. Brain unchanged.")
        return 0

    X = np.asarray(feats_list, dtype=float)
    y = np.asarray([1.0 if float(l) > 0 else 0.0 for l in label_list])
    wins = int(sum(y))
    losses = len(y) - wins
    print(f"\nCollected {len(y)} paper-trade outcomes "
          f"({wins}W / {losses}L, "
          f"win rate {100*wins/len(y):.1f}%)")

    # --- Phase 2: train the brain on these outcomes ---
    if args.mode in ("online", "both"):
        print("\nOnline training on paper-trade outcomes...")
        rng = np.random.default_rng(0)
        n = 0
        for i in range(len(X)):
            brain.online_learn_features(
                list(X[i]), float(y[i]) * 2 - 1, symbol=symbols[i % len(symbols)])
            n += 1
        print(f"  online updates: {n} (brain.online_updates now "
              f"{brain.online_updates})")

    if args.mode in ("offline", "both"):
        print(f"\nOffline batch training on paper-trade outcomes "
              f"({args.epochs} epochs)...")
        rng = np.random.default_rng(0)
        last_loss = 0.0
        for epoch in range(args.epochs):
            order = rng.permutation(len(X))
            for start in range(0, len(X), 32):
                idx = order[start:start + 32]
                for m in brain.ensemble.members:
                    last_loss = m.model.train_step(X[idx], y[idx], rng=rng)
        print(f"  final_loss={last_loss:.4f}")

    os.makedirs(out_dir, exist_ok=True)
    path = save_unified(out_dir, brain)
    print(f"\nBrain saved: {path}")
    print(f"  online_updates={brain.online_updates}")
    return 0


def _run_and_collect(brain, loader, symbols, *, equity, risk_pct):
    """Run backtests and capture (features, label) per closed trade.

    Uses the BacktestEngine which records each trade's decision-time
    features in Position.features; we collect them at settlement.
    """
    sniper = SniperEntryEngine()
    engine = BacktestEngine(brain, sniper=sniper, equity=equity,
                            risk_pct=risk_pct)
    feats_list: List[np.ndarray] = []
    label_list: List[float] = []
    pnl_list: List[float] = []

    for sym in symbols:
        candles = []
        broker = "deriv"
        for b in ("deriv", "fpmarkets"):
            try:
                if b == "deriv":
                    c = loader.load_deriv(symbol=sym, timeframe="M5")
                else:
                    c, _ = loader.load_fp_markets(symbol=sym, timeframe="M5")
                if c:
                    candles = c
                    broker = b
                    break
            except Exception:
                continue
        if not candles or len(candles) < 60:
            continue
        htf_candles = []
        try:
            if broker == "deriv":
                htf_candles = loader.load_deriv(symbol=sym, timeframe="H1")
            else:
                htf_candles, _ = loader.load_fp_markets(symbol=sym,
                                                        timeframe="H1")
        except Exception:
            htf_candles = []

        # The engine's _trade_detail already captures the exact brain
        # feature vector at entry (pos.features). Use it directly — no
        # reconstruction needed, so the feedback is faithful.
        result = engine.run(candles, [], symbol=sym, timeframe="M5",
                            htf_candles=htf_candles,
                            forward_test=False, forward_frac=0.2)

        for d in (result.details or []):
            raw_feats = d.get("features")
            if not raw_feats:
                continue
            feats_list.append(np.asarray(raw_feats, dtype=float))
            pnl = d.get("pnl", 0.0)
            label_list.append(pnl)
            pnl_list.append(pnl)
        print(f"  {sym:22} trades={len(result.details or [])} "
              f"pnl={result.total_pnl:+.2f}")

    return feats_list, label_list, pnl_list


def _parse_dt(s):
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
