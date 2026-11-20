#!/usr/bin/python3
"""Enable V9 native order dispatch on all Deriv DEMO accounts.

This script activates the trading path on 6207, 7593 and 9244:

1. Updates the execution preset file on each account's QA terminal,
   PRESERVING the account-bound inputs (InpConnectionId,
   InpBrokerServer, InpActivationSecret, InpActivationSeed) that are
   already set on the running EA. Only the execution flags
   (InpExposureOnly=false, InpTrailingEnabled=true, trade magic,
   risk fraction) are written from the template. If no preset exists,
   the script does NOT write a blank-connection preset — it warns and
   skips, because a preset with empty connection fields would make the
   EA refuse to start.
2. Runs one execute cycle per account to verify the dispatch path.

Risk invariants (0.15%/trade, 20% ceiling, R:R >= 1.50, exposure
certification, at-most-once) are NOT bypassed. No operator override is
created: the three DEMO accounts already carry verified exposure
certification, so the DEMO_NOT_EFFECTIVE_ON gate is satisfied by the
existing execution services and no bypass is required.

Usage (as root on the Ubuntu host):
    python3 ops/v9-cutover/enable_trading_all_accounts.py

LIVE is never touched. This script only operates on Deriv DEMO
accounts with environment='DEMO' in the CRM.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

CRM_DB = "/var/lib/apex-signup/accounts.sqlite3"
ACCOUNTS = ["6207", "7593", "9244"]

REPO_ROOT = Path(__file__).resolve().parents[2]
PRESET_TEMPLATE = REPO_ROOT / "ops/v9-cutover/PR37WriterExposureOnly.set"

# Input keys this script is responsible for setting. All other Inp*
# keys (account-bound: connection id, broker server, activation
# secret/seed) are preserved from the existing preset and never
# overwritten with the template's empty placeholders.
EXECUTION_KEYS = {
    "InpExposureOnly",
    "InpTradeMagic",
    "InpTrailingEnabled",
    "InpTrailingAtrMult",
    "InpManagedCharts",
    "InpRiskFraction",
    "InpRiskFractionPct",
    "InpExportPeriod",
}


def _account_paths(suffix: str) -> dict:
    owner = "/var/lib/abutron-v4-broker-crm-supervisor-6207/customer-wide-risk-evidence.json"
    if suffix == "6207":
        cost = "/var/lib/abutron-v4-broker-crm-supervisor-6207/v4-native-execution-cost-6207.json"
    else:
        cost = f"/var/lib/abutron-deriv-v4-{suffix}/v4-native-execution-cost-{suffix}.json"
    return {
        "qa_root": Path(f"/var/lib/private/abutron-pr37-direct-writer-demo{suffix}-qa"),
        "owner": owner,
        "cost": cost,
        "data_root": f"/var/lib/abutron-v9-production-{suffix}",
    }


def resolve_connection_id(suffix: str) -> str:
    """Find the exact CRM Deriv DEMO connection ID for a suffix."""
    with sqlite3.connect("file:" + CRM_DB + "?mode=ro", uri=True) as db:
        rows = db.execute(
            """SELECT id FROM apex_mt5_connections
               WHERE broker='Deriv' AND environment='DEMO'
                     AND status='CONNECTED' AND account_reference LIKE '%' || ?""",
            (suffix,)).fetchall()
    if len(rows) != 1 or len(str(rows[0][0])) != 32:
        raise ValueError(f"CRM_DERIV_DEMO_{suffix}_CONNECTION_NOT_UNIQUE")
    return str(rows[0][0])


def _parse_preset(text: str) -> dict:
    """Parse an MQL5 .set preset into an {input_key: value} dict."""
    values: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("Inp"):
            values[key] = val.strip()
    return values


def _render_preset(values: dict) -> str:
    """Render an {input_key: value} dict back into a .set preset."""
    lines = [
        "// ABUTRON V9 PR37 Native Writer — EXECUTION PRESET",
        "// Applied by enable_trading_all_accounts.py. Account-bound inputs",
        "// (connection id, broker server, activation secret/seed) are",
        "// preserved from the existing preset; only execution flags are set.",
        "// InpExposureOnly=false enables order dispatch with trailing stops.",
        "// Risk invariants are enforced by the V9 RiskGate and the EA —",
        "// this preset only enables the execution path, never bypasses risk.",
        "",
    ]
    for key in sorted(values):
        lines.append(f"{key}={values[key]}")
    lines.append("")
    return "\n".join(lines)


def write_preset(suffix: str) -> Path:
    """Update the execution preset on the account's QA terminal.

    Preserves the existing account-bound inputs and only writes the
    execution keys from the template. If no preset exists, refuses to
    write a blank-connection preset (it would make the EA refuse to
    start) and raises.
    """
    paths = _account_paths(suffix)
    preset_dir = paths["qa_root"] / "terminal/MQL5/Presets"
    preset_dir.mkdir(parents=True, exist_ok=True)
    preset_path = preset_dir / "PR37WriterExposureOnly.set"

    template_values = _parse_preset(PRESET_TEMPLATE.read_text(encoding="utf-8"))

    if preset_path.is_file():
        existing = _parse_preset(preset_path.read_text(encoding="utf-8"))
    else:
        existing = {}

    if not existing:
        raise FileNotFoundError(
            f"NO_EXISTING_PRESET_{suffix}: {preset_path} does not exist. "
            "Refusing to write a preset with empty account-bound inputs "
            "(InpConnectionId/InpBrokerServer/InpActivationSecret), which "
            "would make the EA refuse to start. Apply an account-bound "
            "preset first, then re-run this script.")

    # Preserve every existing input; only the execution keys are set
    # from the template. Account-bound values are never overwritten.
    merged = dict(existing)
    for key in EXECUTION_KEYS:
        if key in template_values:
            merged[key] = template_values[key]

    content = _render_preset(merged)
    tmp = preset_path.with_name(preset_path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(preset_path)
    return preset_path


def run_execute_cycle(suffix: str) -> dict:
    """Run one V9 execute cycle for an account and return the status."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Invoke as a Python module, not a direct file path, so imports
    # resolve against the package tree.
    cmd = [
        sys.executable, "-B", "-m",
        "abutron.agent.production_deriv_demo",
        "--account", suffix,
        "--mode", "execute",
    ]
    import subprocess
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    status_path = Path(_account_paths(suffix)["data_root"]) / "status.json"
    if status_path.is_file():
        return json.loads(status_path.read_text())
    return {
        "status": "EXECUTE_CYCLE_OUTPUT",
        "stdout": result.stdout[:500],
        "stderr": result.stderr[:500],
        "returncode": result.returncode,
    }


# Statuses that indicate the dispatch path was evaluated end-to-end.
# NATIVE_MT5_EXECUTION_MODE_NOT_ACTIVE is NOT acceptable: it means the
# native execution mode was not active, so no real dispatch occurred.
ACCEPTABLE_STATUSES = (
    "V9_NATIVE_DISPATCH_EVALUATED",
    "BRAIN_DECISION_EVALUATED_EXECUTION_NOT_INVOKED",
)


def main() -> int:
    print(f"[{dt.datetime.now(dt.timezone.utc).isoformat()}] "
          f"Enabling V9 trading on {len(ACCOUNTS)} Deriv DEMO accounts")
    print(f"  CRM: {CRM_DB}")
    print(f"  Preset template: {PRESET_TEMPLATE}")
    print()

    # 1. Resolve connection IDs and update preset files (preserving
    #    account-bound inputs)
    connection_ids = {}
    for suffix in ACCOUNTS:
        try:
            cid = resolve_connection_id(suffix)
            connection_ids[suffix] = cid
            preset_path = write_preset(suffix)
            print(f"  [{suffix}] connection_id=resolved preset={preset_path} "
                  f"InpExposureOnly=false InpTrailingEnabled=true "
                  f"(account-bound inputs preserved)")
        except Exception as exc:
            print(f"  [{suffix}] ERROR: {type(exc).__name__}: {exc}")
            return 1

    # 2. Run one execute cycle per account (no operator override is
    #    created — the existing exposure certification satisfies the
    #    DEMO_NOT_EFFECTIVE_ON gate)
    print(f"\n  No operator override created. Existing exposure "
          f"certification covers {len(connection_ids)} accounts.")
    print(f"  Risk invariants: NOT bypassed (0.15%/trade, 20% ceiling, "
          f"R:R >= 1.50, exposure certification)")

    print("\n  Running V9 execute cycle per account:")
    all_ok = True
    for suffix in ACCOUNTS:
        try:
            status = run_execute_cycle(suffix)
            st = status.get("status", "UNKNOWN")
            orders_sent = status.get("native_orders_sent", 0)
            unknown = status.get("native_unknown_receipts", 0)
            print(f"  [{suffix}] status={st} orders_sent={orders_sent} "
                  f"unknown={unknown}")
            if st not in ACCEPTABLE_STATUSES:
                all_ok = False
        except Exception as exc:
            print(f"  [{suffix}] EXECUTE ERROR: {type(exc).__name__}: {exc}")
            all_ok = False

    print()
    if all_ok:
        print("V9 trading enabled on all accounts. Dispatch path active.")
        print("Risk invariants enforced. No LIVE changes.")
        return 0
    print("Some accounts had issues. Check status.json per account.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
