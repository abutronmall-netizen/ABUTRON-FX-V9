#!/usr/bin/env python3
"""V9 Contract Verification — reconcile risk/lot-size math vs MT5 specs.

Verifies every registered symbol's point, point_value, lot_min,
lot_step against the approved risk policy (per-trade fraction,
portfolio ceiling, daily loss cap), and reports any mismatches.

Usage:
    PYTHONPATH=src python -u scripts/verify_contracts.py \
        [--equity 10000] [--stop-points 26]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from abutron.risk.contract_verify import (verify_contract,
                                          verify_all_contracts,
                                          verify_owner_exposure)
from abutron.config import (DEFAULT_PER_TRADE_RISK_PCT,
                            DEFAULT_PORTFOLIO_RISK_CAP_PCT,
                            DEFAULT_DAILY_LOSS_CAP_PCT)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--equity", type=float, default=10000.0)
    ap.add_argument("--stop-points", type=float, default=26.0,
                    help="representative ATR stop distance in points")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results = verify_all_contracts(
        equity=args.equity,
        stop_distance_points=args.stop_points,
    )

    verified = [r for r in results if not r.issues]
    failed = [r for r in results if r.issues]

    if args.json:
        import json
        payload = {
            "equity": args.equity,
            "stop_distance_points": args.stop_points,
            "per_trade_risk_pct": DEFAULT_PER_TRADE_RISK_PCT,
            "portfolio_cap_pct": DEFAULT_PORTFOLIO_RISK_CAP_PCT,
            "daily_loss_cap_pct": DEFAULT_DAILY_LOSS_CAP_PCT,
            "total_symbols": len(results),
            "verified": len(verified),
            "failed": len(failed),
            "contracts": [r.to_dict() for r in results],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(f"=== V9 Contract Verification ===")
        print(f"Equity: ${args.equity}  Stop distance: {args.stop_points} pts")
        print(f"Per-trade risk: {DEFAULT_PER_TRADE_RISK_PCT:.2%}"
              f"  Portfolio cap: {DEFAULT_PORTFOLIO_RISK_CAP_PCT:.0%}"
              f"  Daily loss cap: {DEFAULT_DAILY_LOSS_CAP_PCT:.2%}")
        print(f"Symbols: {len(results)}  "
              f"Verified: {len(verified)}  Failed: {len(failed)}\n")
        for r in results:
            status = "OK" if not r.issues else "FAIL"
            print(f"[{status}] {r.symbol:16s} "
                  f"point={r.point:<12.8f} pv={r.point_value:<6.2f} "
                  f"lot_min={r.lot_min:<6.2f} "
                  f"min-risk=${r.min_lot_stop_risk_usd:.4f}")
            for issue in r.issues:
                print(f"        ! {issue}")
        print(f"\n{len(verified)}/{len(results)} symbols verified.")
        if failed:
            print(f"{len(failed)} symbols have issues — review above.")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
