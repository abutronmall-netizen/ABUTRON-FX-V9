"""CLI: run a backtest or forward test over the market-data datasets.

Examples
--------
List available symbols/timeframes:
    python -m abutron.backtest.run --data /path/to/market-data --list

Backtest Deriv Boom 1000 on M5:
    python -m abutron.backtest.run --data /path/to/market-data \\
        --broker deriv --symbol "Boom 1000 Index.0" --timeframe M5

Forward test FP Markets AUDUSD (last 20% held out):
    python -m abutron.backtest.run --data /path/to/market-data \\
        --broker fpmarkets --symbol AUDUSD.r --timeframe D1 \\
        --forward --forward-frac 0.2
"""
from __future__ import annotations

import argparse
import json
import sys

from ..brain import make_brain, default_ensemble
from ..brain.trainer import load_brain
from ..brain.unified import load_unified
from ..config import BRAIN_ENSEMBLE_SEED, BRAIN_SIZE_CAP
from ..apex.sniper import SniperEntryEngine
from .loader import MarketDataLoader
from .engine import BacktestEngine


def _print_report(r) -> None:
    print(f"\n=== {r.mode.upper()} — {r.symbol} {r.timeframe} ===")
    print(f"  equity start : {r.equity_start:,.2f}")
    print(f"  equity end   : {r.equity_end:,.2f}")
    print(f"  total PnL    : {r.total_pnl:,.2f}  ({r.return_pct:+.2f}%)")
    print(f"  trades       : {r.trades} (reported window: {r.trades_reported})")
    print(f"  win rate     : {r.win_rate:.1f}%  ({r.wins}W / {r.losses}L)")
    print(f"  profit factor: {r.profit_factor:.2f}")
    print(f"  avg win / loss: {r.avg_win:,.2f} / {r.avg_loss:,.2f}")
    print(f"  max drawdown : {r.max_drawdown:.2f}%")
    print(f"  max consec losses: {r.max_consecutive_losses}")
    if r.htf:
        print(f"  MTF confluence: HTF {r.htf} — "
              f"{r.confluence_filtered} entries filtered (waited for TFs)")
    if r.entry_filtered:
        print(f"  entry filters : {r.entry_filtered} entries filtered "
              f"(conviction/regime/momentum/volatility)")
    if r.details:
        print(f"\n  last 10 closed trades:")
        for d in r.details[-10:]:
            print(f"    {d['direction']:4} {d['symbol']:14} "
                  f"entry {d['entry']:.5f} exit {d['exit']:.5f} "
                  f"{d['pnl']:+.2f} [{d['reason']}]")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ABUTRON FX backtest / forward test")
    ap.add_argument("--data", required=True, help="market-data directory")
    ap.add_argument("--broker", default="deriv",
                    choices=["deriv", "fpmarkets"])
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--timeframe", default=None)
    ap.add_argument("--htf", default=None,
                    help="higher timeframe for multi-timeframe confluence "
                         "(e.g. H1 when trading M5); only enter when the HTF "
                         "direction agrees with the trigger")
    ap.add_argument("--equity", type=float, default=10000.0)
    ap.add_argument("--risk-pct", type=float, default=0.05)
    ap.add_argument("--point", type=float, default=0.0,
                    help="fallback point size; auto-detected from data if 0")
    ap.add_argument("--point-value", type=float, default=1.0,
                    help="USD per point per lot (1.0 = standard for indices/FX)")
    ap.add_argument("--commission-per-lot", type=float, default=0.0)
    ap.add_argument("--tp-atr", type=float, default=None,
                    help="sniper take-profit in ATR multiples "
                         "(optimizer picks tighter TP for higher win rate)")
    ap.add_argument("--sl-atr", type=float, default=None,
                    help="sniper stop-loss in ATR fraction")
    ap.add_argument("--min-conviction", type=float, default=0.0,
                    help="skip entries with brain conviction below this "
                         "(0 = no filter)")
    ap.add_argument("--require-trend", action="store_true",
                    help="only enter in a trending regime")
    ap.add_argument("--require-momentum", action="store_true",
                    help="skip entries opposed by an extreme RSI / momentum")
    ap.add_argument("--min-atr-pct", type=float, default=0.0,
                    help="skip entries when ATR% is below this (volatility)")
    ap.add_argument("--max-atr-pct", type=float, default=0.0,
                    help="skip entries when ATR% is above this (volatility)")
    ap.add_argument("--brain", default=None,
                    help="path to a trained brain dir (from train.py) to load "
                         "instead of an untrained ensemble")
    ap.add_argument("--forward", action="store_true",
                    help="forward test: report only the last --forward-frac")
    ap.add_argument("--forward-frac", type=float, default=0.2)
    ap.add_argument("--list", action="store_true",
                    help="list available symbols/timeframes and exit")
    ap.add_argument("--json", action="store_true", help="emit JSON report")
    args = ap.parse_args(argv)

    loader = MarketDataLoader(args.data)
    if args.list:
        summary = loader.discover()
        print(json.dumps(summary, indent=2))
        return 0

    if args.broker == "deriv":
        candles = loader.load_deriv(symbol=args.symbol,
                                    timeframe=args.timeframe)
        quotes = []
    else:
        candles, quotes = loader.load_fp_markets(symbol=args.symbol,
                                                 timeframe=args.timeframe)

    htf_candles = []
    if args.htf:
        if args.broker == "deriv":
            htf_candles = loader.load_deriv(symbol=args.symbol,
                                            timeframe=args.htf)
        else:
            htf_candles, _ = loader.load_fp_markets(symbol=args.symbol,
                                                    timeframe=args.htf)

    if not candles:
        print("No candles loaded. Check --broker/--symbol/--timeframe or run "
              "--list.")
        return 1

    print(f"Loaded {len(candles)} candles"
          + (f", {len(quotes)} quotes" if quotes else "") + ".")

    if args.brain:
        # Prefer the unified (49-dim market+symbol) brain; fall back to a
        # plain per-symbol ensemble brain if no unified checkpoint exists.
        brain = load_unified(args.brain)
        if brain is not None:
            print(f"Loaded UNIFIED brain from {args.brain} "
                  f"({len(brain.ensemble.members)} members, "
                  f"{len(brain.symbol_ids)} symbols, "
                  f"online_updates={brain.online_updates}).")
        else:
            ensemble = load_brain(args.brain)
            brain = make_brain(ensemble=ensemble, size_cap=BRAIN_SIZE_CAP)
            print(f"Loaded trained brain from {args.brain} "
                  f"({len(ensemble.members)} members, "
                  f"calibrated={ensemble._fitted}).")
    else:
        brain = make_brain(ensemble=default_ensemble(seed=BRAIN_ENSEMBLE_SEED),
                           size_cap=BRAIN_SIZE_CAP)
        print("Using untrained brain ensemble (random fallback signals). "
              "Train first with: python -m abutron.backtest.train")
    sniper_kwargs = {}
    if args.tp_atr is not None:
        sniper_kwargs["tp_atr_mult"] = args.tp_atr
    if args.sl_atr is not None:
        sniper_kwargs["invalidation_atr_frac"] = args.sl_atr
    sniper = SniperEntryEngine(**sniper_kwargs)
    engine = BacktestEngine(
        brain, sniper=sniper, equity=args.equity, risk_pct=args.risk_pct,
        point=args.point, point_value=args.point_value,
        commission_per_lot=args.commission_per_lot,
        min_conviction=args.min_conviction,
        require_trend_regime=args.require_trend,
        require_momentum=args.require_momentum,
        min_atr_pct=args.min_atr_pct,
        max_atr_pct=args.max_atr_pct)
    result = engine.run(
        candles, quotes, symbol=args.symbol or "",
        timeframe=args.timeframe or "",
        htf_candles=htf_candles,
        forward_test=args.forward, forward_frac=args.forward_frac)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        _print_report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
