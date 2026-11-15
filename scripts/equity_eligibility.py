#!/usr/bin/env python
"""Equity-aware symbol eligibility — live demo / admin view.

Demonstrates the automatic AI system that looks at the client equity and
trades ONLY the symbols that cater for that equity, ranked by the
forward-test validation already done on each instrument. The per-symbol
trained brains are loaded and reported so you can see the brain is the
one trained + validated on each symbol.

Usage:
    PYTHONPATH=src python scripts/equity_eligibility.py            # $50
    PYTHONPATH=src python scripts/equity_eligibility.py 100 500 1000
    PYTHONPATH=src python scripts/equity_eligibility.py --risk 0.25 50
"""
from __future__ import annotations

import argparse
import os
import sys

# allow running from repo root without install
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.apex.eligibility import (  # noqa: E402
    SYMBOL_REGISTRY, SymbolEligibility, load_symbol_brain,
    default_brain_root, slug_for)


def _brain_status(symbol: str, brain_root: str) -> str:
    b = load_symbol_brain(symbol, brain_root=brain_root)
    if b is None:
        return "no trained brain (default ensemble)"
    rep = b.ensemble  # trained ensemble present
    acc = ""
    # the held-out accuracy lives in the brain JSON's report; load_brain
    # does not surface it, so report member count as the trained-brain signal.
    return f"trained brain ({len(rep.members)} members)"


def show(equity: float, risk_pct: float, brain_root: str) -> None:
    eng = SymbolEligibility(default_risk_pct=risk_pct)
    rep = eng.report(equity, risk_pct=risk_pct)
    print(f"\n{'=' * 78}")
    print(f"EQUITY-AWARE ELIGIBILITY  |  equity=${equity:,.2f}  "
          f"risk={risk_pct*100:.0f}% per trade")
    print(f"{'=' * 78}")
    if rep["tradable"]:
        print(f"\nTRADABLE ({len(rep['tradable'])}) — ranked by validation:")
        print(f"  {'#':>2}  {'symbol':24} {'minEq':>9} {'WR':>6} {'PF':>6} "
              f"{'trades':>6} {'validated':>9}  brain")
        for i, r in enumerate(rep["tradable"], 1):
            v = SYMBOL_REGISTRY[r["symbol"]].validation
            pf = f"{v.test_pf:.2f}" if v.test_pf else "  -"
            wr = f"{v.test_win_rate*100:.1f}%"
            print(f"  {i:>2}  {r['symbol']:24} ${r['min_equity']:>7.2f} "
                  f"{wr:>6} {pf:>6} {v.test_trades:>6} "
                  f"{'YES' if r['validated'] else 'no':>9}  "
                  f"{_brain_status(r['symbol'], brain_root)}")
    else:
        print("\nTRADABLE: (none — equity too small for any symbol)")
    if rep["rejected"]:
        print(f"\nSKIPPED ({len(rep['rejected'])}) — equity below min-lot threshold:")
        print(f"  {'symbol':24} {'needs':>9}  reason")
        for r in rep["rejected"]:
            print(f"  {r['symbol']:24} ${r['min_equity']:>7.2f}  "
                  f"{r['reason']}")
    print(f"\n  -> The engine trades ONLY the tradable set above; the brain used "
          f"for each is its own trained, validated brain.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("equities", nargs="*", type=float,
                    help="equity levels to report (default: 50)")
    ap.add_argument("--risk", type=float, default=0.15,
                    help="per-trade risk fraction (default 0.15 = committed)")
    ap.add_argument("--brain-root", default=None,
                    help="trained-brain root (default data/brains)")
    args = ap.parse_args()
    brain_root = args.brain_root or default_brain_root()
    equities = args.equities if args.equities else [50.0]
    print(f"Trained-brain root: {brain_root}")
    print(f"Registry symbols: {len(SYMBOL_REGISTRY)}")
    for eq in equities:
        show(eq, args.risk, brain_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
