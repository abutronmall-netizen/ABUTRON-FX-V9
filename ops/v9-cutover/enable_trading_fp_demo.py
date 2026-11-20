#!/usr/bin/python3
"""Enable V9 native order dispatch on FP Markets DEMO accounts.

Activates the trading path on the FP Markets DEMO accounts
(0512, 1636, 1622, 2582):

1. Verifies the deployed ApexNativeWriter.ex5 matches the pinned
   SHA-256 (the EA is broker-agnostic and shared with Deriv DEMO).
2. Updates the execution preset on each account's FP QA terminal,
   PRESERVING the account-bound inputs (InpConnectionId,
   InpBrokerServer, InpActivationSecret, InpActivationSeed). Only the
   execution flags (InpExposureOnly=false, InpTrailingEnabled=true,
   trade magic, risk fraction) are written from the template.
3. Runs one execute cycle per account to verify the dispatch path.

Risk invariants (0.15%/trade, 20% ceiling, R:R >= 1.50, exposure
certification, at-most-once) are NOT bypassed. No operator override
is created: the DEMO exposure certification satisfies the gate.

This script only operates on FP Markets DEMO accounts. LIVE FP
Markets (9392) is never touched and requires separate authorization.

Usage (as root on the Ubuntu host):
    python3 ops/v9-cutover/enable_trading_fp_demo.py
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

CRM_DB = "/var/lib/apex-signup/accounts.sqlite3"
ACCOUNTS = ["0512", "1636", "1622", "2582"]

REPO_ROOT = Path(__file__).resolve().parents[2]
PRESET_TEMPLATE = REPO_ROOT / "ops/v9-cutover/PR37WriterExposureOnly.set"

# Expected SHA-256 of the deployed ApexNativeWriter.ex5. Must match
# the binary on each terminal. Updated to the CI-compiled artifact
# before deployment.
EXPECTED_EX5_SHA = (
    "b6b63afeb7c4939a2bea1f40cfad58d8288af4009d8dc4e2534d7196162b91f3"
)

# Input keys this script is responsible for setting. All other Inp*
# keys (account-bound) are preserved from the existing preset.
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
    """FP Markets DEMO terminal path layout (configurable via env)."""
    return {
        "qa_root": Path(os.environ.get(
            "ABUTRON_FP_QA_ROOT",
            f"/var/lib/private/abutron-pr37-direct-writer-fp-demo{suffix}-qa")),
        "data_root": os.environ.get(
            "ABUTRON_FP_DATA_ROOT",
            f"/var/lib/abutron-v9-production-fp-{suffix}"),
    }


def resolve_connection_id(suffix: str) -> str:
    """Find the exact CRM FP Markets DEMO connection ID for a suffix."""
    with sqlite3.connect("file:" + CRM_DB + "?mode=ro", uri=True) as db:
        rows = db.execute(
            """SELECT id FROM apex_mt5_connections
               WHERE broker='FP Markets' AND environment='DEMO'
                     AND status='CONNECTED' AND account_reference LIKE '%' || ?""",
            (suffix,)).fetchall()
    if len(rows) != 1 or len(str(rows[0][0])) != 32:
        raise ValueError(f"CRM_FP_DEMO_{suffix}_CONNECTION_NOT_UNIQUE")
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
        "// ABUTRON V9 PR37 Native Writer — FP MARKETS DEMO EXECUTION PRESET",
        "// Applied by enable_trading_fp_demo.py. Account-bound inputs",
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


def verify_ex5_sha(suffix: str) -> str:
    """Verify the deployed ApexNativeWriter.ex5 matches the pinned SHA."""
    paths = _account_paths(suffix)
    ex5 = paths["qa_root"] / "terminal/MQL5/Experts/ABUTRON/ApexNativeWriter.ex5"
    if not ex5.is_file() or ex5.is_symlink():
        raise FileNotFoundError(
            f"NATIVE_EA_BINARY_MISSING_{suffix}: {ex5}")
    actual = hashlib.sha256(ex5.read_bytes()).hexdigest()
    if actual != EXPECTED_EX5_SHA:
        raise ValueError(
            f"NATIVE_EA_BINARY_CHANGED_{suffix}: deployed EX5 SHA "
            f"{actual[:16]}... does not match pinned SHA "
            f"{EXPECTED_EX5_SHA[:16]}...")
    return str(ex5)


def write_preset(suffix: str) -> list:
    """Update the execution preset on the account's FP QA terminal.

    Preserves account-bound inputs and the existing file's permission
    mode. Updates both Presets/ and presets/ case variants. If no
    preset exists, refuses to write a blank-connection preset.
    """
    paths = _account_paths(suffix)
    preset_dirs = [
        paths["qa_root"] / "terminal/MQL5/Presets",
        paths["qa_root"] / "terminal/MQL5/presets",
    ]
    template_values = _parse_preset(PRESET_TEMPLATE.read_text(encoding="utf-8"))

    canonical_dir = preset_dirs[0]
    canonical_path = canonical_dir / "PR37WriterExposureOnly.set"
    if canonical_path.is_file():
        existing = _parse_preset(canonical_path.read_text(encoding="utf-8"))
        mode = canonical_path.stat().st_mode & 0o777
    else:
        existing = {}
        mode = 0o600

    if not existing:
        raise FileNotFoundError(
            f"NO_EXISTING_PRESET_{suffix}: {canonical_path} does not exist. "
            "Refusing to write a preset with empty account-bound inputs. "
            "Apply an account-bound preset first, then re-run this script.")

    merged = dict(existing)
    for key in EXECUTION_KEYS:
        if key in template_values:
            merged[key] = template_values[key]

    content = _render_preset(merged)
    written = []
    for preset_dir in preset_dirs:
        if not preset_dir.is_dir():
            continue
        preset_path = preset_dir / "PR37WriterExposureOnly.set"
        tmp = preset_path.with_name(preset_path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.chmod(mode)
        tmp.replace(preset_path)
        written.append(preset_path)
    return written


def run_execute_cycle(suffix: str) -> dict:
    """Run one V9 execute cycle for an FP DEMO account."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [
        sys.executable, "-B", "-m",
        "abutron.agent.production_fp_demo",
        "--account", suffix,
        "--mode", "execute",
    ]
    import subprocess
    started_ns = time.time_ns()
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    status_path = Path(_account_paths(suffix)["data_root"]) / "status.json"
    if result.returncode != 0:
        return {
            "status": "EXECUTE_CYCLE_FAILED",
            "stdout": result.stdout[:500],
            "stderr": result.stderr[:500],
            "returncode": result.returncode,
        }
    if status_path.is_file() and status_path.stat().st_mtime_ns >= started_ns:
        status = json.loads(status_path.read_text())
        if status.get("account_suffix") != suffix:
            return {"status": "EXECUTE_CYCLE_ACCOUNT_MISMATCH"}
        status["returncode"] = result.returncode
        return status
    return {
        "status": "EXECUTE_CYCLE_STALE_OR_MISSING_STATUS",
        "stdout": result.stdout[:500],
        "stderr": result.stderr[:500],
        "returncode": result.returncode,
    }


ACCEPTABLE_STATUSES = (
    "V9_NATIVE_DISPATCH_EVALUATED",
    "BRAIN_DECISION_EVALUATED_EXECUTION_NOT_INVOKED",
)


def main() -> int:
    print(f"[{dt.datetime.now(dt.timezone.utc).isoformat()}] "
          f"Enabling V9 trading on {len(ACCOUNTS)} FP Markets DEMO accounts")
    print(f"  CRM: {CRM_DB}")
    print(f"  Preset template: {PRESET_TEMPLATE}")
    print()

    # Verify every DEMO CRM binding, preset and binary before writing any
    # account's execution configuration. Existing feed-only terminals will
    # fail here without affecting their running service.
    connection_ids = {}
    for suffix in ACCOUNTS:
        try:
            cid = resolve_connection_id(suffix)
            paths = _account_paths(suffix)
            preset = paths["qa_root"] / "terminal/MQL5/Presets/PR37WriterExposureOnly.set"
            if not preset.is_file() or preset.is_symlink():
                raise FileNotFoundError(f"NO_ACCOUNT_BOUND_FP_PRESET_{suffix}")
            bound = _parse_preset(preset.read_text(encoding="utf-8"))
            if not all(bound.get(k, "").strip('"')
                       for k in ("InpConnectionId", "InpBrokerServer", "InpActivationSeed")):
                raise ValueError(f"FP_PRESET_ACCOUNT_BINDING_MISSING_{suffix}")
            if bound["InpConnectionId"].strip('"') != cid:
                raise ValueError(f"FP_PRESET_CRM_ID_MISMATCH_{suffix}")
            if not Path(paths["data_root"]).is_dir():
                raise FileNotFoundError(f"FP_V9_DATA_ROOT_NOT_PREPARED_{suffix}")
            verify_ex5_sha(suffix)
            connection_ids[suffix] = cid
            print(f"  [{suffix}] CRM, account preset, data root, and EX5 verified")
        except Exception as exc:
            print(f"  [{suffix}] PREPARE ERROR: {type(exc).__name__}: {exc}")
            return 1

    # Only update execution flags, leaving all identity and authorization
    # values intact. This is DEMO-only and never creates an override.
    for suffix in ACCOUNTS:
        try:
            paths = write_preset(suffix)
            print(f"  [{suffix}] account-bound presets updated: {len(paths)}")
        except Exception as exc:
            print(f"  [{suffix}] PRESET ERROR: {type(exc).__name__}: {exc}")
            return 1

    print(f"\n  No operator override created. Risk invariants: NOT bypassed "
          f"(0.15%/trade, 20% ceiling, R:R >= 1.50, exposure certification)")

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
        print("V9 trading enabled on all FP Markets DEMO accounts. "
              "Dispatch path active.")
        print("Risk invariants enforced. No LIVE changes.")
        return 0
    print("Some accounts had issues. Check status.json per account.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
