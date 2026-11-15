"""Batch win-rate optimizer across every symbol in market-data.

Runs the TP/SL + filter sweep for all Deriv and/or FP-Markets symbols on
a timeframe (with HTF confluence where data exists), ranks each by win
rate subject to a positive-expectancy guard, validates the winner on
held-out bars, and prints a ranked table + writes JSON.

This is the all-symbol companion to ``abutron.backtest.optimize`` (which
optimizes a single symbol). Use it to find which instruments reach high
win rate and what geometry/filters get them there.

Example:
    python -m abutron.backtest.optimize_all --data /path/to/market-data \\
        --broker all --timeframe M5 --htf H1 --equity 10000 --risk-pct 0.05
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import List, Optional, Tuple

from ..brain import make_brain, default_ensemble
from ..apex.sniper import SniperEntryEngine
from .loader import MarketDataLoader
from .engine import BacktestEngine


def _slug(symbol: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", symbol.lower()).strip("_")


def _load_ensemble(brain_root: Optional[str], symbol: str, tf: str):
    """Load a trained brain for a symbol, or None to use untrained."""
    if not brain_root:
        return None
    from ..brain.trainer import load_brain
    path = os.path.join(brain_root, f"{_slug(symbol)}_{tf.lower()}")
    if os.path.isdir(path):
        try:
            return load_brain(path, mc_samples=16)
        except Exception:
            return None
    return None

# --- grids --------------------------------------------------------------
GRIDS = {
    # fast: tight/normal TP x tight/loose SL x 4 filter sets
    "fast": dict(tp=[0.5, 1.0], sl=[0.3, 0.6], conv=[0.0],
                 filters=[(False, False), (True, False),
                          (False, True), (True, True)]),
    # full: wider TP sweep incl. tight TPs + conviction
    "full": dict(tp=[0.3, 0.4, 0.5, 0.7, 1.0, 1.2],
                 sl=[0.3, 0.5, 0.8, 1.0],
                 conv=[0.0, 0.3, 0.5],
                 filters=[(False, False), (True, False),
                          (False, True), (True, True)]),
}


def _load(broker, symbol, tf, htf, data):
    l = MarketDataLoader(data)
    if broker == "deriv":
        c = l.load_deriv(symbol=symbol, timeframe=tf)
        h = l.load_deriv(symbol=symbol, timeframe=htf) if htf else []
    else:
        c, _ = l.load_fp_markets(symbol=symbol, timeframe=tf)
        h = []
        if htf:
            h, _ = l.load_fp_markets(symbol=symbol, timeframe=htf)
    return c, h


def _run_combo(candles, htf_candles, *, tp_mult, sl_frac, min_conv,
               require_trend, require_mom, equity, risk_pct, warmup,
               ensemble=None):
    if ensemble is None:
        ensemble = default_ensemble(seed=0)
    brain = make_brain(ensemble=ensemble, size_cap=1.0)
    sniper = SniperEntryEngine(tp_atr_mult=tp_mult,
                               invalidation_atr_frac=sl_frac)
    eng = BacktestEngine(
        brain, sniper=sniper, equity=equity, risk_pct=risk_pct,
        point=0.0, point_value=1.0, warmup=warmup,
        min_conviction=min_conv, require_trend_regime=require_trend,
        require_momentum=require_mom)
    return eng.run(candles, [], symbol=candles[0].symbol if candles else "",
                   timeframe=candles[0].timeframe if candles else "",
                   htf_candles=htf_candles)


def _expectancy(r) -> float:
    return (r.total_pnl / r.trades) if r.trades else 0.0


def _optimize_symbol(job) -> dict:
    broker, symbol, tf, htf, data, equity, risk, grid, sweep_bars, brain_root = job
    candles, htf_candles = _load(broker, symbol, tf, htf, data)
    if len(candles) < 400:
        return {"broker": broker, "symbol": symbol, "bars": len(candles),
                "status": "too_few_bars"}
    ensemble = _load_ensemble(brain_root, symbol, tf)
    split = max(1, int(len(candles) * 0.8))
    train, test = candles[:split], candles[split:]
    htf_tr = [x for x in htf_candles
              if x.close_time <= train[-1].close_time] if htf_candles else []
    htf_te = [x for x in htf_candles
              if x.close_time <= test[-1].close_time] if htf_candles else []
    st = train[:sweep_bars] if len(train) > sweep_bars else train
    sh = [x for x in htf_tr if x.close_time <= st[-1].close_time] \
        if htf_tr else []

    results = []
    for tp in grid["tp"]:
        for sl in grid["sl"]:
            for conv in grid["conv"]:
                for trend, mom in grid["filters"]:
                    r = _run_combo(
                        st, sh, tp_mult=tp, sl_frac=sl, min_conv=conv,
                        require_trend=trend, require_mom=mom, equity=equity,
                        risk_pct=risk, warmup=30, ensemble=ensemble)
                    if r.trades < 15:
                        continue
                    results.append((tp, sl, conv, trend, mom, r))
    if not results:
        return {"broker": broker, "symbol": symbol, "bars": len(candles),
                "status": "no_combos"}

    pos = [c for c in results if _expectancy(c[5]) > 0]
    pool = pos if pos else results
    pool.sort(key=lambda c: (c[5].win_rate, _expectancy(c[5])), reverse=True)
    tp, sl, conv, trend, mom, best = pool[0]
    rt = _run_combo(test, htf_te, tp_mult=tp, sl_frac=sl, min_conv=conv,
                    require_trend=trend, require_mom=mom, equity=equity,
                    risk_pct=risk, warmup=30, ensemble=ensemble)
    return {
        "broker": broker, "symbol": symbol, "bars": len(candles),
        "status": "ok",
        "tp": tp, "sl": sl, "conv": conv,
        "trend": trend, "mom": mom,
        "train_trades": best.trades, "train_wr": round(best.win_rate, 4),
        "train_pf": round(best.profit_factor, 3),
        "train_exp": round(_expectancy(best), 2),
        "train_dd": round(best.max_drawdown, 3),
        "test_trades": rt.trades, "test_wr": round(rt.win_rate, 4),
        "test_pf": round(rt.profit_factor, 3),
        "test_exp": round(_expectancy(rt), 2),
        "test_dd": round(rt.max_drawdown, 3),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="All-symbol win-rate optimizer")
    ap.add_argument("--data", required=True, help="market-data directory")
    ap.add_argument("--broker", default="all",
                    choices=["all", "deriv", "fpmarkets"])
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--htf", default=None,
                    help="higher timeframe for confluence (e.g. H1)")
    ap.add_argument("--equity", type=float, default=10000.0)
    ap.add_argument("--risk-pct", type=float, default=0.05)
    ap.add_argument("--grid", default="fast", choices=["fast", "full"],
                    help="sweep grid size (fast ~16, full ~96 combos)")
    ap.add_argument("--sweep-bars", type=int, default=1000,
                    help="max bars used per sweep (speed)")
    ap.add_argument("--min-trades", type=int, default=15)
    ap.add_argument("--max-workers", type=int, default=2)
    ap.add_argument("--brain-root", default=None,
                    help="directory of trained brains (data/brains); loads "
                         "the per-symbol trained brain instead of the "
                         "untrained ensemble")
    ap.add_argument("--out", default=None,
                    help="write JSON results to this path")
    ap.add_argument("--json", action="store_true",
                    help="also print full JSON after the table")
    args = ap.parse_args(argv)

    loader = MarketDataLoader(args.data)
    disc = loader.discover()
    grid = GRIDS[args.grid]
    jobs = []
    brokers = ["deriv", "fpmarkets"] if args.broker == "all" else [args.broker]
    for broker in brokers:
        if broker not in disc:
            continue
        for sym in sorted({p["symbol"] for p in disc[broker]}):
            jobs.append((broker, sym, args.timeframe, args.htf, args.data,
                         args.equity, args.risk_pct, grid, args.sweep_bars,
                         args.brain_root))
    n = len(grid["tp"]) * len(grid["sl"]) * len(grid["conv"]) * len(grid["filters"])
    brain_tag = " +trained brains" if args.brain_root else ""
    print(f"Optimizing {len(jobs)} symbols on {args.timeframe}"
          + (f" +{args.htf}" if args.htf else "")
          + f" ({n}-combo {args.grid} grid){brain_tag}...\n")

    out: List[dict] = []
    with ProcessPoolExecutor(max_workers=args.max_workers) as ex:
        futs = {ex.submit(_optimize_symbol, j): j for j in jobs}
        for f in as_completed(futs):
            r = f.result()
            out.append(r)
            if r.get("status") == "ok":
                print(f"  {r['broker']:8} {r['symbol']:18} "
                      f"train {r['train_wr']:5.1f}%/PF{r['train_pf']:.2f}  "
                      f"test {r['test_wr']:5.1f}%/PF{r['test_pf']:.2f}  "
                      f"TP{r['tp']}/SL{r['sl']} "
                      f"{'T' if r['trend'] else '-'}{'M' if r['mom'] else '-'}")
            else:
                print(f"  {r['broker']:8} {r['symbol']:18} {r['status']}")

    ok = [r for r in out if r.get("status") == "ok"]
    ok.sort(key=lambda r: r["test_wr"], reverse=True)
    print("\n===== RANKED BY HELD-OUT TEST WIN RATE =====")
    print(f"{'broker':8} {'symbol':20} {'bars':>5} {'TP':>4} {'SL':>4} "
          f"{'filt':4} {'trd':>4} {'testWR':>7} {'testPF':>6} "
          f"{'testExp':>8} {'testDD':>7}")
    for r in ok:
        filt = ("T" if r["trend"] else "-") + ("M" if r["mom"] else "-")
        print(f"{r['broker']:8} {r['symbol']:20} {r['bars']:5d} "
              f"{r['tp']:4.1f} {r['sl']:4.1f} {filt:4} {r['test_trades']:4d} "
              f"{r['test_wr']:6.1f}% {r['test_pf']:6.2f} {r['test_exp']:8.2f} "
              f"{r['test_dd']:6.2f}%")
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(out, fh, indent=2, default=str)
        print(f"\nSaved {len(out)} results -> {args.out}")
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
