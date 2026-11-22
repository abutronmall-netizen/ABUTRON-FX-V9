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
    destination = output_dir.resolve()
    if destination == Path("/var/lib/abutron-v9-production") or (
        destination.is_relative_to(Path("/var/lib/abutron-v9-production-6207"))
        or destination.is_relative_to(Path("/var/lib/abutron-v9-production-7593"))
        or destination.is_relative_to(Path("/var/lib/abutron-v9-production-9244"))
    ):
        raise ValueError("cannot write into production broker data")
    if "active-owner-models" in destination.parts:
        raise ValueError("cannot write into active owner models")
    if not ledger.is_file():
        raise FileNotFoundError("authenticated native economics ledger missing")
    query = """SELECT account, trade_key, symbol, direction, entry_time, exit_time,
           features_json, lots, gross_usd, commission_usd, fee_usd,
           swap_usd, net_usd, stop_loss_exit, take_profit_exit,
           native_deal_verified, cost_breakdown_verified
           FROM observations
           WHERE symbol=? AND account IN (?,?,?)"""
    with sqlite3.connect(f"file:{ledger}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        raw = [dict(row) for row in db.execute(
            query, (SYMBOL, "6207", "7593", "9244")).fetchall()]
        db.rollback()
    # This hashes exactly the broker evidence used by this training run.
    # A live SQLite WAL file hash alone does not identify a consistent snapshot.
    native_rows_sha = hashlib.sha256(json.dumps(
        sorted(raw, key=lambda row: (row["account"], row["trade_key"])),
        sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    trades = [
        ClosedTrade.from_native_row(row) for row in raw if row["account"] in accounts
    ]
    if not trades:
        raise ValueError("no verified closed V50 broker trades in requested accounts")
    identity_slot = MARKET_FEATURES + SYMBOL_UNIVERSE.index(SYMBOL)
    if MARKET_FEATURES != 23 or len(SYMBOL_UNIVERSE) != 26 or identity_slot >= 49:
        raise ValueError("unexpected canonical brain registry")
    report = {
        "schema": "abutron.v9.v50.per_account_broker_net_reward49.research.v2",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": "native authenticated closed DEAL economics, not inferred candle fills",
        "source_native_rows_sha256": native_rows_sha,
        "accounts_approved_demo": sorted(accounts), "symbol": SYMBOL,
        "market_feature_width": 49, "account_isolation": True,
        "validation_start_utc": val_start.isoformat(),
        "holdout_start_utc": test_start.isoformat(),
        "accounts": {},
        "broker_net_profitability_proven": False,
        "model_automatically_promoted": False,
        "production_execution_or_risk_changed": False,
        "broker_orders_submitted": 0,
    }
    for account in sorted(accounts):
        own = [row for row in trades if row.account == account]
        tr, va, te, purged = chronological_split(own, val_start, test_start)
        entry = {
            "closed_positions": len(own), "train_trades": len(tr),
            "validation_trades": len(va), "holdout_trades": len(te),
            "purged_boundary_crossing_trades": purged,
            "observed_side_counts": {
                segment: {side: sum(row.side == side for row in group)
                          for side in ("BUY", "SELL")}
                for segment, group in (("train", tr), ("validation", va), ("holdout", te))
            },
        }
        if min(len(tr), len(va), len(te)) < 10:
            entry["status"] = "INSUFFICIENT_OWNER_CHRONOLOGICAL_EVIDENCE"
            report["accounts"][account] = entry
            continue
        model = NetReward49(identity_slot=identity_slot, min_side_trades=20)
        entry["side_training"] = model.fit(tr, va)
        observed = []
        for row in te:
            result = model.predict(row.features, row.side)
            if result["status"] == "RESEARCH_PREDICTION":
                observed.append((row, result["broker_net_usd_per_lot"]))
        entry["holdout_evaluated_executed_side_trades"] = len(observed)
        entry["holdout_realized_outcomes"] = group_results(
            [row for row, _ in observed], [pred for _, pred in observed]
        )
        entry["unseen_opposite_side_returns_claimed"] = False
        if not model.heads:
            entry["status"] = "NO_SUPPORTED_EXECUTED_DIRECTION"
        else:
            entry["status"] = "ISOLATED_RESEARCH_CANDIDATE"
            _safe_write(output_dir / account / "net_reward49_candidate.json", model.artifact())
        report["accounts"][account] = entry
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
        "accounts": report["accounts"],
        "source_native_rows_sha256": report["source_native_rows_sha256"],
        "broker_net_profitability_proven": report["broker_net_profitability_proven"],
        "model_automatically_promoted": report["model_automatically_promoted"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
