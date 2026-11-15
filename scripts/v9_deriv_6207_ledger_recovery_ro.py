#!/usr/bin/env python3
"""Deriv DEMO 6207 native ACK / missing-ledger audit. READ-ONLY, NO ORDERS."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from abutron.execution.deriv_6207_recovery_audit import (
    RecoveryUnverified, inspect_6207,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--crm-db", type=Path,
                        default=Path("/var/lib/apex-signup/accounts.sqlite3"))
    parser.add_argument("--ledger-db", type=Path,
                        default=Path("/var/lib/apex-client-feed/execution-ledger.sqlite3"))
    parser.add_argument("--native-feed-root", type=Path,
                        default=Path("/var/lib/apex-client-feed"))
    parser.add_argument("--mt5-worker-root", type=Path, default=None)
    args = parser.parse_args()
    worker = args.mt5_worker_root or (
        Path("/var/lib/private") /
        ("abutron-client-mt5-" + args.connection_id))
    try:
        result = inspect_6207(
            cid=args.connection_id, crm_db=args.crm_db,
            ledger_db=args.ledger_db,
            feed_root=args.native_feed_root, worker_root=worker)
    except (RecoveryUnverified, OSError, ValueError, KeyError,
            json.JSONDecodeError) as exc:
        reason = str(exc) if isinstance(exc, RecoveryUnverified) else (
            "NATIVE_RECOVERY_SOURCE_UNREADABLE")
        result = {
            "schema": "abutron.v9.deriv_demo_6207_ledger_recovery_audit.readonly.v1",
            "state": "UNVERIFIED",
            "account_suffix": "6207",
            "execution_authorized": False,
            "ledger_modified": False, "orders_submitted": 0,
            "blockers": [reason],
        }
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["state"] == "RECONCILED_READONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
