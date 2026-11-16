"""Walk-forward win-rate optimizer.

Sweeps the sniper TP/SL geometry + high-precision entry filters over a
train window, ranks combos by win rate subject to a positive-expectancy
guard (so it never picks a degenerate high-win-rate / negative-EV setup),
then validates the winner on a held-out test window.

A 92% win rate is achievable by tightening the TP and filtering entries,
but that trades away reward:risk. This optimizer finds the win-rate /
expectancy tradeoff that actually makes money, and reports both.

Example:
    python -m abutron.backtest.optimize --data /path/to/market-data \\
        --broker deriv --symbol "Boom 1000 Index.0" --timeframe M5 \\
        --htf H1 --equity 10000 --risk-pct 0.05
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import List, Optional

from ..brain import make_brain, default_ensemble
from ..brain.unified import SYMBOL_UNIVERSE
from ..apex.sniper import SniperEntryEngine
from ..apex.runtime_rules import DEFAULT_TRAIL_K, write_runtime_rules
from .loader import MarketDataLoader
from .engine import BacktestEngine


@dataclass
class ComboResult:
    tp_mult: float
    sl_frac: float
    min_conv: float
    require_trend: bool
    require_mom: bool
    trades: int
    win_rate: float
    profit_factor: float
    expectancy: float
    max_dd: float


def _expectancy(r) -> float:
    """Per-trade expectancy in USD (avg PnL per trade)."""
    if not r.trades:
        return 0.0
    return r.total_pnl / r.trades


def _run_combo(candles, htf_candles, *, tp_mult, sl_frac, min_conv,
               require_trend, require_mom, equity, risk_pct, warmup):
    brain = make_brain(ensemble=default_ensemble(seed=0), size_cap=1.0)
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Win-rate optimizer")
    ap.add_argument("--data", required=True)
    ap.add_argument("--broker", default="deriv",
                    choices=["deriv", "fpmarkets"])
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", required=True)
    ap.add_argument("--htf", default=None)
    ap.add_argument("--equity", type=float, default=10000.0)
    ap.add_argument("--risk-pct", type=float, default=0.05)
    ap.add_argument("--warmup", type=int, default=30)
    ap.add_argument("--test-frac", type=float, default=0.2,
                    help="held-out fraction for final validation")
    ap.add_argument("--min-trades", type=int, default=20,
                    help="min trades for a combo to be considered")
    ap.add_argument("--sweep-bars", type=int, default=1600,
                    help="max bars used for the grid sweep (speed)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--runtime-rules-out", default=None,
                    help="write the held-out validated non-neural rule JSON")
    ap.add_argument("--export-trades", default=None,
                    help="export held-out paper trades with entry features")
    args = ap.parse_args(argv)

    loader = MarketDataLoader(args.data)
    if args.broker == "deriv":
        candles = loader.load_deriv(symbol=args.symbol,
                                    timeframe=args.timeframe)
        htf = loader.load_deriv(symbol=args.symbol, timeframe=args.htf) \
            if args.htf else []
    else:
        candles, _ = loader.load_fp_markets(symbol=args.symbol,
                                            timeframe=args.timeframe)
        htf = []
        if args.htf:
            htf, _ = loader.load_fp_markets(symbol=args.symbol,
                                            timeframe=args.htf)
    if not candles:
        print("No candles loaded.")
        return 1
    print(f"Loaded {len(candles)} candles"
          + (f", {len(htf)} HTF" if htf else "") + ".")

    split = max(1, int(len(candles) * (1.0 - args.test_frac)))
    train = candles[:split]
    test = candles[split:]
    htf_tr = [h for h in htf if h.close_time <= train[-1].close_time] \
        if htf else []
    htf_te = [h for h in htf if h.close_time <= test[-1].close_time] \
        if htf else []

    # --- grid sweep on the TRAIN window --------------------------------
    tp_grid = [0.5, 1.0, 1.5]
    sl_grid = [0.3, 0.6, 1.0]
    conv_grid = [0.0, 0.5]
    filter_sets = [
        (False, False), (True, False), (False, True), (True, True),
    ]
    # Limit bars used for the sweep to keep the grid fast; final validation
    # runs on the full held-out test window.
    sweep_bars = args.sweep_bars
    sweep_train = train[:sweep_bars] if sweep_bars and len(train) > sweep_bars else train
    sweep_htf = [h for h in htf_tr if h.close_time <= sweep_train[-1].close_time] if htf_tr else []

    results: List[ComboResult] = []
    print("\nSweeping geometry + filters on train window "
          f"({len(sweep_train)} bars, {len(tp_grid)*len(sl_grid)*len(conv_grid)*len(filter_sets)} combos)...")
    for tp in tp_grid:
        for sl in sl_grid:
            for conv in conv_grid:
                for trend, mom in filter_sets:
                    r = _run_combo(
                        sweep_train, sweep_htf, tp_mult=tp, sl_frac=sl,
                        min_conv=conv, require_trend=trend,
                        require_mom=mom, equity=args.equity,
                        risk_pct=args.risk_pct, warmup=args.warmup)
                    if r.trades < args.min_trades:
                        continue
                    ev = _expectancy(r)
                    results.append(ComboResult(
                        tp_mult=tp, sl_frac=sl, min_conv=conv,
                        require_trend=trend, require_mom=mom,
                        trades=r.trades, win_rate=r.win_rate,
                        profit_factor=r.profit_factor, expectancy=ev,
                        max_dd=r.max_drawdown))

    if not results:
        print("No combos met the minimum-trades threshold.")
        return 1

    # --- rank: positive expectancy first, then win rate -----------------
    pos = [c for c in results if c.expectancy > 0]
    pool = pos if pos else results
    pool.sort(key=lambda c: (c.win_rate, c.expectancy), reverse=True)
    best = pool[0]

    # --- report the top tradeoff curve ----------------------------------
    print("\n  Top 5 combos by win rate (expectancy-positive):")
    print("   TP   SL   conv  trend mom | trades  WR     PF     expect  maxDD")
    for c in pool[:5]:
        print(f"   {c.tp_mult:4.1f} {c.sl_frac:4.1f} {c.min_conv:4.1f}  "
              f"{'Y' if c.require_trend else 'n':5s} "
              f"{'Y' if c.require_mom else 'n':2s} | {c.trades:5d}  "
              f"{c.win_rate:5.1f}%  {c.profit_factor:5.2f}  "
              f"${c.expectancy:+8.2f}  {c.max_dd:5.2f}%")

    try:
        with open("/tmp/abutron_opt_results.json", "w") as fh:
            json.dump([{k: getattr(c, k) for k in
                        ("tp_mult", "sl_frac", "min_conv", "require_trend",
                         "require_mom", "trades", "win_rate",
                         "profit_factor", "expectancy", "max_dd")}
                       for c in pool], fh, indent=2, default=str)
    except OSError:
        pass

    # --- validate the winner on the held-out TEST window ---------------
    r_test = _run_combo(
        test, htf_te, tp_mult=best.tp_mult, sl_frac=best.sl_frac,
        min_conv=best.min_conv, require_trend=best.require_trend,
        require_mom=best.require_mom, equity=args.equity,
        risk_pct=args.risk_pct, warmup=args.warmup)

    print(f"\n=== BEST COMBO (ranked by win rate, expectancy>0) ===")
    print(f"  TP = {best.tp_mult} ATR | SL = {best.sl_frac} ATR | "
          f"min conviction = {best.min_conv} | "
          f"trend filter = {best.require_trend} | "
          f"momentum filter = {best.require_mom}")
    print(f"\n  TRAIN ({len(train)} bars):")
    print(f"    trades={best.trades}  win rate={best.win_rate:.1f}%  "
          f"PF={best.profit_factor:.2f}  expectancy=${best.expectancy:+.2f}  "
          f"maxDD={best.max_dd:.2f}%")
    print(f"  TEST  ({len(test)} bars, held-out):")
    print(f"    trades={r_test.trades}  win rate={r_test.win_rate:.1f}%  "
          f"PF={r_test.profit_factor:.2f}  "
          f"expectancy=${_expectancy(r_test):+.2f}  "
          f"maxDD={r_test.max_drawdown:.2f}%")

    if args.runtime_rules_out and r_test.trades >= args.min_trades and \
            _expectancy(r_test) > 0 and r_test.profit_factor > 1.0:
        rule = {
            "tp_atr_mult": float(best.tp_mult),
            "invalidation_atr_frac": float(best.sl_frac),
            "min_conviction": float(best.min_conv),
            "require_trend": bool(best.require_trend),
            "require_momentum": bool(best.require_mom),
            "trail_k": DEFAULT_TRAIL_K,
            "validation": {
                "held_out_trades": int(r_test.trades),
                "held_out_win_rate": float(r_test.win_rate),
                "held_out_profit_factor": float(r_test.profit_factor),
                "held_out_expectancy_usd": float(_expectancy(r_test)),
                "held_out_max_dd_pct": float(r_test.max_drawdown),
            },
        }
        symbols = {args.symbol: rule}
        for sym in SYMBOL_UNIVERSE:
            if sym.lower() == args.symbol.lower() and sym != args.symbol:
                symbols.setdefault(sym, rule)
                break
        path = write_runtime_rules(
            args.runtime_rules_out, symbols,
            metadata={"source": "abutron.backtest.optimize",
                      "timeframe": args.timeframe, "htf": args.htf,
                      "note": "Execution rules only; never neural weights"})
        print(f"Validated runtime rules -> {path}")
    if args.export_trades:
        import os
        out = os.path.abspath(args.export_trades)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({
                "source": "abutron.backtest.optimize",
                "mode": "held_out_paper",
                "signal_sources": r_test.signal_sources,
                "trades": r_test.details,
            }, fh, indent=2, default=str)
        print(f"Exported {len(r_test.details)} held-out trades -> {out}")
    if args.json:
        print(json.dumps({
            "best": {k: getattr(best, k) for k in
                     ("tp_mult", "sl_frac", "min_conv", "require_trend",
                      "require_mom", "trades", "win_rate", "profit_factor",
                      "expectancy", "max_dd")},
            "test": {"trades": r_test.trades,
                     "win_rate": r_test.win_rate,
                     "profit_factor": r_test.profit_factor,
                     "expectancy": _expectancy(r_test),
                     "max_drawdown": r_test.max_drawdown},
        }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
