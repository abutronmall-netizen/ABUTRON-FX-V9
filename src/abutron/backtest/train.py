"""CLI: train the NexusBrain on market data and save the trained model.

Trains the brain's ensemble on historical candles (walk-forward), reports
out-of-sample direction accuracy, then persists the trained model so it can
be loaded back into a NexusBrain for backtest, forward-test, or live use.

Examples
--------
Train Deriv Boom 1000 on M5 (single walk-forward split):
    python -m abutron.backtest.train --data /path/to/market-data \\
        --broker deriv --symbol "Boom 1000 Index.0" --timeframe M5 \\
        --epochs 120

Train FP Markets AUDUSD on H1 with 5-fold walk-forward OOS estimate:
    python -m abutron.backtest.train --data /path/to/market-data \\
        --broker fpmarkets --symbol AUDUSD.r --timeframe H1 \\
        --epochs 120 --folds 5 --out data/brains/audusd_h1

Then use the saved brain in a backtest:
    brain = make_brain(ensemble=load_brain("data/brains/audusd_h1"))
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

from ..brain import default_ensemble, make_brain
from ..brain.trainer import (TrainReport, prepare_samples, save_brain,
                             walk_forward_train)
from .loader import MarketDataLoader


def _print_report(label: str, rep: TrainReport) -> None:
    print(f"  [{label}] samples={rep.n_samples} "
          f"train={rep.n_train} val={rep.n_val}")
    if rep.notes and "INSUFFICIENT_DATA" in rep.notes:
        print(f"    -> {rep.notes[0]}")
        return
    print(f"    final_loss={rep.final_loss:.5f} "
          f"OOS_dir_acc={rep.val_direction_accuracy:.4f} "
          f"calibrated={rep.calibrated}")


def _multi_fold(ensemble, samples, *, epochs, batch_size, val_fraction,
                seed, folds) -> TrainReport:
    """Chronological K-fold walk-forward: train on expanding window,
    validate on the next unseen block, collect OOS direction accuracy.
    Returns a TrainReport averaging the folds and the final all-data
    training report."""
    from ..brain.trainer import _to_arrays
    X, y = _to_arrays(samples)
    n = len(X)
    if n < folds * 40:
        folds = max(1, n // 40)
    # split into `folds` chronological blocks
    block = n // folds
    accs, losses, sizes = [], [], []
    for k in range(folds):
        tr_end = n - (folds - 1 - k) * block
        if tr_end <= 40:
            continue
        Xtr, ytr = X[:tr_end], y[:tr_end]
        Xva, yva = X[tr_end:tr_end + block], y[tr_end:tr_end + block]
        if len(Xva) < 8 or len(Xtr) < 40:
            continue
        # fresh ensemble per fold for a clean OOS estimate
        rng = np.random.default_rng(seed + k)
        ens = default_ensemble(seed=seed + k)
        # train inline (same protocol as walk_forward_train)
        order = rng.permutation(len(Xtr))
        last_loss = 0.0
        for _ in range(epochs):
            o = rng.permutation(len(Xtr))
            for s in range(0, len(Xtr), batch_size):
                idx = o[s:s + batch_size]
                for m in ens.members:
                    last_loss = m.model.train_step(Xtr[idx], ytr[idx], rng=rng)
        val_preds = np.asarray([m.predict(Xva) for m in ens.members])
        blend = np.mean(val_preds, axis=0)
        buy_idx = 1 if blend.shape[-1] == 2 else 2
        edge = blend[..., buy_idx] - (1.0 - blend[..., buy_idx])
        pred_dir = np.where(edge > 0, 1, 0)
        accs.append(float(np.mean(pred_dir == yva)))
        losses.append(last_loss)
        sizes.append((len(Xtr), len(Xva)))
    # refit the REAL ensemble on ALL samples for the saved model
    full = walk_forward_train(
        ensemble, samples, epochs=epochs, batch_size=batch_size,
        val_fraction=val_fraction, seed=seed)
    if accs:
        full.notes.append(f"oos_folds={len(accs)}")
        full.notes.append(f"oos_mean_acc={np.mean(accs):.4f}")
        full.notes.append(f"oos_min_acc={np.min(accs):.4f}")
        full.notes.append(f"oos_max_acc={np.max(accs):.4f}")
    return full


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Train the NexusBrain on market data and save the model")
    ap.add_argument("--data", required=True, help="market-data directory")
    ap.add_argument("--broker", default="deriv",
                    choices=["deriv", "fpmarkets"])
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", required=True)
    ap.add_argument("--epochs", type=int, default=120,
                    help="training epochs (more = better fit; watch OOS)")
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--val-fraction", type=float, default=0.2)
    ap.add_argument("--horizon", type=int, default=1,
                    help="label horizon in bars (1 = next bar direction)")
    ap.add_argument("--folds", type=int, default=1,
                    help="walk-forward folds for OOS estimate (>=1)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=None,
                    help="output dir (default data/brains/<symbol>_<tf>)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    loader = MarketDataLoader(args.data)
    if args.broker == "deriv":
        candles = loader.load_deriv(symbol=args.symbol,
                                    timeframe=args.timeframe)
    else:
        candles, _ = loader.load_fp_markets(symbol=args.symbol,
                                            timeframe=args.timeframe)
    if not candles:
        print("No candles loaded. Check --broker/--symbol/--timeframe or run "
              "the backtest CLI with --list.")
        return 1

    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    samples = prepare_samples(closes, highs, lows, horizon=args.horizon)
    print(f"Loaded {len(candles)} candles -> {len(samples)} training samples "
          f"(horizon={args.horizon}).")

    ensemble = default_ensemble(seed=args.seed)
    if args.folds and args.folds > 1:
        print(f"\nWalk-forward training: {args.folds} folds, "
              f"{args.epochs} epochs each...")
        report = _multi_fold(
            ensemble, samples, epochs=args.epochs,
            batch_size=args.batch_size, val_fraction=args.val_fraction,
            seed=args.seed, folds=args.folds)
    else:
        print(f"\nTraining: {args.epochs} epochs (single split)...")
        report = walk_forward_train(
            ensemble, samples, epochs=args.epochs,
            batch_size=args.batch_size, val_fraction=args.val_fraction,
            seed=args.seed)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        _print_report("final", report)
        for n in report.notes:
            print(f"    {n}")

    # assemble a brain carrying the trained ensemble
    brain = make_brain(ensemble=ensemble, size_cap=1.0)
    out_dir = args.out or os.path.join(
        "data", "brains", f"{args.symbol.replace('.', '_')}_{args.timeframe}")
    path = save_brain(out_dir, ensemble, report)
    print(f"\nBrain trained: OOS dir acc = "
          f"{report.val_direction_accuracy:.4f}")
    print(f"Saved trained model -> {path}")
    print(f"\nLoad it back with:\n"
          f"  from abutron.brain import make_brain\n"
          f"  from abutron.brain.trainer import load_brain\n"
          f"  brain = make_brain(ensemble=load_brain('{out_dir}'))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
