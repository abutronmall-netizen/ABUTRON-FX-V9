#!/usr/bin/env python3
"""Train V50 broker-net entry reward candidate; NEVER touch active owner models.

Usage on an authorized Ubuntu copy of the existing broker-native SQLite ledger:
  PYTHONPATH=src python scripts/train_broker_net_reward49.py \
    --ledger /path/to/observed_native_economics.sqlite3 \
    --output-dir /path/to/private/research

The report is retrospective only. Prospective performance is unproven.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from abutron.brain.net_reward49 import (
    ClosedTrade, NetReward49, chronological_split, group_results
)
from abutron.brain.unified import SYMBOL_UNIVERSE
from abutron.brain.features import N_FEATURES as MARKET_FEATURES

SYMBOL = "Volatility 50 Index.0"
APPROVED_DEMO = frozenset(("6207", "7593", "9244"))


def _utc(value: str) -> datetime:
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("an explicit UTC offset is required")
    return t.astimezone(timezone.utc)


def _safe_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError("research artifact exists; use a distinct output directory")
    fd, temp = tempfile.mkstemp(prefix=".candidate-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def run(ledger: Path, output_dir: Path, val_start: datetime, test_start: datetime,
        accounts: set[str]) -> dict:
    if not accounts or not accounts.issubset(APPROVED_DEMO):
        raise ValueError("only explicitly approved Deriv DEMO accounts are supported")
    if output_dir.resolve().is_relative_to(Path("/var/lib/abutron-v9-production")):
        raise ValueError("cannot write into production broker data")
    if "active-owner-models" in output_dir.resolve().parts:
        raise ValueError("cannot write into active owner models")
    if not ledger.is_file():
        raise FileNotFoundError("authenticated native economics ledger missing")
    ledger_sha = hashlib.sha256(ledger.read_bytes()).hexdigest()
    query = """SELECT account, trade_key, symbol, direction, entry_time, exit_time,
           features_json, lots, gross_usd, commission_usd, fee_usd,
           swap_usd, net_usd, stop_loss_exit, take_profit_exit,
           native_deal_verified, cost_breakdown_verified
           FROM observations
           WHERE symbol=? AND account IN (?,?,?)"""
    with sqlite3.connect(f"file:{ledger}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        raw = db.execute(query, (SYMBOL, "6207", "7593", "9244")).fetchall()
    trades = [
        ClosedTrade.from_native_row(dict(row)) for row in raw
        if row["account"] in accounts
    ]
    if len(trades) < 80:
        raise ValueError("insufficient verified closed V50 broker trades")
    train, validation, holdout, purged = chronological_split(
        trades, val_start, test_start
    )
    if min(len(train), len(validation), len(holdout)) < 15:
        raise ValueError("chronological split lacks enough real outcomes")
    identity_slot = MARKET_FEATURES + SYMBOL_UNIVERSE.index(SYMBOL)
    if MARKET_FEATURES != 23 or len(SYMBOL_UNIVERSE) != 26 or identity_slot >= 49:
        raise ValueError("unexpected canonical brain registry")
    model = NetReward49(identity_slot=identity_slot, min_side_trades=20)
    train_report = model.fit(train, validation)

    predictions = []
    for trade in holdout:
        result = model.predict(trade.features, trade.side)
        if result["status"] == "RESEARCH_PREDICTION":
            predictions.append((trade, result["broker_net_usd_per_lot"]))
    eligible = [row for row, _ in predictions]
    scores = [value for _, value in predictions]
    report = {
        "schema": "abutron.v9.v50.broker_net_reward49.backtest.v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": "native authenticated closed DEAL economics, not inferred candle fills",
        "source_ledger_sha256": ledger_sha,
        "accounts_approved_demo": sorted(accounts),
        "symbol": SYMBOL,
        "input_market_feature_width": 49,
        "validation_start_utc": val_start.isoformat(),
        "holdout_start_utc": test_start.isoformat(),
        "train_trades": len(train),
        "validation_trades": len(validation),
        "holdout_trades": len(holdout),
        "purged_boundary_crossing_trades": purged,
        "unique_holdout_m5_entry_candles": len({r.closed_m5_bucket for r in holdout}),
        "observed_side_counts": {
            segment: {
                side: sum(r.side == side for r in data)
                for side in ("BUY", "SELL")
            }
            for segment, data in (("train", train), ("validation", validation), ("holdout", holdout))
        },
        "side_training": train_report,
        "holdout_evaluated_executed_side_trades": len(eligible),
        "holdout_realized_outcomes": group_results(eligible, scores),
        "unseen_opposite_side_returns_claimed": False,
        "protected_exit_labels_used": True,
        "gross_commission_fee_swap_labels_used": True,
        "broker_net_profitability_proven": False,
        "model_automatically_promoted": False,
        "production_execution_or_risk_changed": False,
        "broker_orders_submitted": 0,
    }
    _safe_write(output_dir / "net_reward49_candidate.json", model.artifact())
    _safe_write(output_dir / "chronological_evaluation.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--accounts", default="6207,7593,9244")
    parser.add_argument("--validation-start", default="2026-11-21T00:00:00+00:00")
    parser.add_argument("--holdout-start", default="2026-11-22T00:00:00+00:00")
    args = parser.parse_args()
    report = run(
        args.ledger, args.output_dir, _utc(args.validation_start),
        _utc(args.holdout_start), set(args.accounts.split(","))
    )
    print(json.dumps({
        key: report[key] for key in
        ("train_trades", "validation_trades", "holdout_trades",
         "observed_side_counts", "holdout_realized_outcomes",
         "broker_net_profitability_proven", "model_automatically_promoted")
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
