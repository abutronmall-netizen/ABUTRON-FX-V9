"""Replay paper/backtest/forward results into the unified brain.

The old provenance file recorded a paper-trade *aggregate*, but paper trades
were never an actual training input. This stage closes that loop: every
exported result must carry ``features`` (the exact 23 or 49 feature vector
captured at entry), ``symbol``, ``direction``, ``regime`` and ``pnl``.

Accepted inputs:
  * JSON list of trade dicts
  * JSON object with ``trades``/``details``/``results`` list
  * JSON object keyed by symbol with lists of trades
  * JSONL / gzipped JSONL (one trade per line)
  * CSV / CSV.gz with a ``features`` JSON column

For 23-length feature rows, the proper symbol one-hot is appended. Rows that
lack entry features are reported, not guessed — that would misrepresent the
result as trained. Losing patterns are persisted with ``LossPatternGuard``.

Usage:
    PYTHONPATH=src python -m scripts.replay_results \
        --results data/results/paper_trades.json \
        --results data/results/forward_trades.json.gz \
        [--in data/brains/unified_m5] [--out data/brains/unified_m5] \
        [--mode online|offline] [--epochs 8]
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import math
import os
import sys
import time
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import numpy as np

from abutron.brain.signals import SIGNAL_DATA_VERSION, features_for_brain
from abutron.brain.unified import (load_unified, n_unified_features,
                                   save_unified, unified_brain_dir)


def _open_text(path: str):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "rt", encoding="utf-8")


def _coerce_features(value: Any) -> Optional[List[float]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if isinstance(value, dict):
        value = value.get("features") or value.get("market_features")
    if not isinstance(value, (list, tuple)):
        return None
    out = []
    for v in value:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f):
            return None
        out.append(f)
    return out or None


def _iter_rows(data: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                yield row
    elif isinstance(data, dict):
        for key in ("trades", "details", "results", "paper_trades"):
            value = data.get(key)
            if isinstance(value, list):
                for row in value:
                    if isinstance(row, dict):
                        yield row
                return
        for value in data.values():
            if isinstance(value, list):
                for row in value:
                    if isinstance(row, dict):
                        yield row


def load_result_file(path: str) -> List[Dict[str, Any]]:
    with _open_text(path) as fh:
        text = fh.read()
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        if "\n" in stripped and stripped.startswith("{"):
            # JSONL object stream
            rows = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
            if rows:
                return rows
        data = json.loads(text)
        return list(_iter_rows(data))
    if stripped.startswith("symbol") or "features" in stripped.splitlines()[0]:
        reader = csv.DictReader(text.splitlines())
        return [dict(r) for r in reader]
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def collect_paths(specs: List[str]) -> List[str]:
    out = []
    for spec in specs:
        matches = glob.glob(spec, recursive=True)
        if not matches and os.path.isfile(spec):
            matches = [spec]
        for path in matches:
            if os.path.isdir(path):
                out.extend(sorted(glob.glob(os.path.join(path, "*.json*"))))
                out.extend(sorted(glob.glob(os.path.join(path, "*.csv*"))))
            elif os.path.isfile(path):
                out.append(path)
    return sorted(set(out))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", action="append", default=[],
                    help="result file/glob/directory (repeatable)")
    ap.add_argument("--in", dest="in_dir", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--mode", choices=("online", "offline"),
                    default="online")
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--lr", type=float, default=5e-4)
    ap.add_argument("--max-results", type=int, default=0)
    args = ap.parse_args()

    paths = collect_paths(args.results or [])
    if not paths:
        print("ERROR: no result files provided. Use --results at least once.")
        return 1
    in_dir = args.in_dir or unified_brain_dir()
    out_dir = args.out or in_dir
    brain = load_unified(in_dir)
    if brain is None:
        print(f"ERROR: no unified brain at {in_dir}.")
        return 1
    brain.online_enabled = True
    brain.nn_online_lr = args.lr
    expected_features = n_unified_features(brain.symbol_ids)

    raw: List[Dict[str, Any]] = []
    for path in paths:
        rows = load_result_file(path)
        for row in rows:
            row = dict(row)
            row.setdefault("_source_file", os.path.relpath(path))
            raw.append(row)
        print(f"  {path}: {len(rows)} result rows")
    if args.max_results > 0:
        raw = raw[:args.max_results]

    X: List[np.ndarray] = []
    y: List[float] = []
    meta: List[Dict[str, Any]] = []
    skipped_no_features = 0
    skipped_no_symbol = 0
    skipped_bad_vector = 0
    for row in raw:
        feats = _coerce_features(row.get("features"))
        symbol = str(row.get("symbol", "")).strip()
        if not feats:
            skipped_no_features += 1
            continue
        if not symbol or symbol not in brain.symbol_ids:
            skipped_no_symbol += 1
            continue
        try:
            full = np.asarray(features_for_brain(brain, feats, symbol=symbol),
                              dtype=float)
        except Exception:
            skipped_bad_vector += 1
            continue
        if full.size != expected_features:
            skipped_bad_vector += 1
            continue
        direction = str(row.get("direction", "BUY")).upper()
        pnl = float(row.get("pnl", row.get("profit", 0.0)) or 0.0)
        # Trade PnL is direction relative; convert to market up/down label.
        up = pnl > 0 if direction == "BUY" else pnl < 0
        label = 1.0 if up else 0.0
        regime = str(row.get("regime", "") or "")
        X.append(full)
        y.append(label)
        meta.append({
            "symbol": symbol, "direction": direction, "regime": regime,
            "entry_type": str(row.get("entry_type", "REPLAY") or "REPLAY"),
            "entry_quality": float(row.get("entry_quality", 0.5) or 0.5),
            "pnl": pnl, "features": full,
        })
    print(f"\nReplayable result rows: {len(X)} / {len(raw)}")
    print(f"  skipped: no_features={skipped_no_features} "
          f"unknown_symbol={skipped_no_symbol} "
          f"bad_vector={skipped_bad_vector}")
    if not X:
        print("ERROR: no replayable rows. Export backtests with "
              "BacktestResult.details (they include entry features).")
        return 1

    # Losing result patterns teach both the neural ensemble and the explicit
    # per-trade guard. A win clears the matching guard signature.
    for row in meta:
        brain.learn_from_trade(
            symbol=row["symbol"], regime=row["regime"],
            direction=row["direction"], entry_type=row["entry_type"],
            entry_quality=row["entry_quality"], pnl=row["pnl"],
            features=row["features"].tolist())

    Xa = np.asarray(X, dtype=float)
    ya = np.asarray(y, dtype=float)
    loss_idx = np.where(ya == 0.0)[0]
    # Oversample losses once for the offline objective; online uses one step
    # per result in chronological input order.
    Xb = (np.concatenate((Xa, Xa[loss_idx]), axis=0)
          if len(loss_idx) else Xa)
    yb = (np.concatenate((ya, ya[loss_idx]), axis=0)
          if len(loss_idx) else ya)
    t0 = time.time()
    last_loss = 0.0
    rng = np.random.default_rng(0)
    if args.mode == "online":
        # One causal single-result update per member, preserving input order.
        for features, label, row in zip(Xa, ya, meta):
            result = brain.online_learn_features(
                features.tolist(), 1.0 if label > 0 else -1.0,
                symbol=row["symbol"])
            last_loss = float(result.get("loss", last_loss))
    else:
        epochs = max(1, args.epochs)
        for _ in range(epochs):
            order = rng.permutation(len(Xb))
            for start in range(0, len(order), args.batch_size):
                idx = order[start:start + args.batch_size]
                batch_x = Xb[idx]
                batch_y = yb[idx]
                for m in brain.ensemble.members:
                    m.model.optimizer.lr = args.lr
                    last_loss = m.model.train_step(batch_x, batch_y, rng=rng)
        brain.online_updates += len(meta)
    dt = time.time() - t0
    print(f"\nReplay training [{args.mode}] done in {dt:.1f}s | "
          f"loss={last_loss:.5f} | online_updates={brain.online_updates}")

    os.makedirs(out_dir, exist_ok=True)
    path = save_unified(out_dir, brain)
    report = {
        "version": SIGNAL_DATA_VERSION,
        "stage": f"paper_forward_result_replay_{args.mode}",
        "inputs": [os.path.relpath(p) for p in paths],
        "rows_loaded": len(raw),
        "rows_trained": len(meta),
        "rows_skipped": {
            "no_features": skipped_no_features,
            "unknown_symbol": skipped_no_symbol,
            "bad_vector": skipped_bad_vector,
        },
        "loss_rows": int(len(loss_idx)),
        "loss_rows_oversampled": bool(len(loss_idx)),
        "mode": args.mode,
        "epochs": args.epochs if args.mode == "offline" else 1,
        "final_loss": last_loss,
        "lr": args.lr,
        "elapsed_s": round(dt, 3),
        "loss_guard": brain.loss_guard.stats(),
        "online_updates_total": brain.online_updates,
    }
    filename = f"result_replay_report_{args.mode}.json"
    with open(os.path.join(out_dir, filename), "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    with open(os.path.join(out_dir, "result_replay_report.json"), "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"Refined brain saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
