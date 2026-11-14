#!/usr/bin/env python3
"""ABUTRON V9 native-order readiness, READ-ONLY. Does not connect or place orders."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def get_count(path: Path, table: str) -> int | None:
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as db:
            return int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    except (sqlite3.Error, OSError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="/var/lib/abutron-fx-v9-hermes/data")
    parser.add_argument("--risk-evidence", default="/var/lib/abutron-v4-broker-crm-supervisor-6207/customer-wide-risk-evidence.json")
    parser.add_argument("--source-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--env-file", default="/etc/abutron-fx-v9-hermes.env")
    args = parser.parse_args()
    root = Path(args.data_root)
    source = Path(args.source_root)
    blockers: list[str] = []
    notes: list[str] = []

    count = get_count(root / "engine.db", "accounts")
    if count is None:
        blockers.append("V9_ACCOUNT_INVENTORY_UNREADABLE")
    elif count < 1:
        blockers.append("V9_ZERO_LINKED_ACCOUNTS")

    model = root / "brain" / "nexus_brain.json"
    if not model.is_file() or model.is_symlink():
        blockers.append("V9_ACTIVE_NEXUS_MODEL_MISSING")

    compiled = list((source / "mql5").glob("ApexNativeWriter.ex5"))
    if not compiled:
        blockers.append("V9_NATIVE_WRITER_BINARY_UNVERIFIED")

    runtime = source / "src" / "abutron" / "main.py"
    if runtime.is_file() and "BROKER_NATIVE_ORDER_HANDLER_NOT_CONNECTED" in runtime.read_text():
        blockers.append("V9_NATIVE_MT5_HANDOFF_NOT_CONNECTED")

    risk_path = Path(args.risk_evidence)
    risk = {}
    if risk_path.is_file():
        try:
            risk = json.loads(risk_path.read_text())
        except (OSError, ValueError):
            pass
    risk_status = risk.get("status", "UNAVAILABLE")
    if risk_status != "PASS":
        blockers.append("CUSTOMER_WIDE_NATIVE_RISK_NOT_CERTIFIED")
    details = risk.get("blockers", [])

    env = {}
    f = Path(args.env_file)
    if f.is_file():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                if key in ("ABUTRON_ENV", "ABUTRON_LIVE_TRADING_ENABLED",
                           "ABUTRON_PER_TRADE_PCT", "ABUTRON_RISK_CAP_PCT"):
                    env[key] = value.strip()
    if env.get("ABUTRON_LIVE_TRADING_ENABLED", "unknown").lower() != "false":
        blockers.append("V9_LIVE_MODE_MUST_REMAIN_DISABLED_FOR_DEMO_PROOF")
    if env.get("ABUTRON_ENV", "unknown").lower() != "demo":
        blockers.append("V9_EXECUTION_ENVIRONMENT_NOT_DEMO")
    if env.get("ABUTRON_PER_TRADE_PCT") != "0.10":
        # The current vendored candidate contract is exactly 0.10%;
        # the actual owner policy is different and must be reconciled.
        notes.append("PER_TRADE_POLICY_V9_ENV_DIFFERS_FROM_APEX_CONTRACT_0_10")
    if risk.get("risk_caps_policy", {}).get("aggregate_open_pct") == 2.0:
        notes.append("AGGREGATE_OPEN_RISK_2_PERCENT_IS_DISTINCT_FROM_20_PERCENT_MAX_DRAWDOWN")
    output = {
        "checked_utc": datetime.now(timezone.utc).isoformat(),
        "inspection_mode": "READ_ONLY_NO_ORDERS",
        "verdict": "NOT_READY_FOR_V9_AUTOMATIC_DEMO_EXECUTION" if blockers else "PREFLIGHT_CLEARED_NOT_BROKER_CERTIFIED",
        "v9_linked_accounts": count,
        "v9_active_nexus_model_present": model.is_file(),
        "v9_compiled_writer_found": bool(compiled),
        "customer_risk_status": risk_status,
        "customer_risk_checked_utc": risk.get("checked_utc"),
        "customer_risk_blockers": details,
        "v9_env_nonsecret_subset": env,
        "blockers": blockers,
        "notes": notes,
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
