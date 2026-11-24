#!/usr/bin/env python3
"""Stage a pinned native writer for an existing MT5 DEMO data folder.

This never runs or attaches the EA, writes a secret, or sends broker orders.
Usage requires a real pre-registered DEMO account in the V9 engine DB.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from abutron.db import engine_db
from abutron.feeds.mt5_terminal import Mt5Error, Mt5TerminalManager


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--terminal-data-dir", required=True)
    parser.add_argument("--ex5", required=True)
    parser.add_argument("--sha256", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--stage", action="store_true")
    args = parser.parse_args(argv)

    # The connection's persisted DEMO identity is authoritative. A command
    # line label does not certify broker account login, server or owner.
    account = engine_db.get_account(args.connection_id)
    if not account:
        raise SystemExit("registered CRM account not found: refusing stage")
    if str(account.get("environment", "")).lower() != "demo":
        raise SystemExit("LIVE or unclassified account cannot use DEMO staging")
    if not account.get("server") or not account.get("mt5_login"):
        raise SystemExit("registered DEMO identity lacks login/server")
    if os.getenv("ABUTRON_LIVE_TRADING_ENABLED", "true").lower() not in (
            "false", "0", "no"):
        raise SystemExit("set ABUTRON_LIVE_TRADING_ENABLED=false for QA")

    mgr = Mt5TerminalManager()
    files_dir, experts_dir = mgr._validated_terminal_data_dir(
        args.terminal_data_dir)
    source = Path(args.ex5)
    if not source.is_file() or source.is_symlink() or source.suffix.lower() != ".ex5":
        raise SystemExit("expected a regular native .ex5 source")
    actual = hashlib.sha256(source.read_bytes()).hexdigest()
    if actual.lower() != args.sha256.lower():
        raise SystemExit("native EX5 SHA-256 pin does not match source")
    result = {
        "qa_mode": "STAGE_ONLY_NO_CHART_ATTACHMENT",
        "connection_id": args.connection_id,
        "environment": "demo",
        "native_qa_sha256": actual,
        "terminal_data_dir": os.path.realpath(args.terminal_data_dir),
        "files_path_validated": True,
        "attached_to_chart": False,
        "orders_submitted": 0,
        "activation_secret_modified": False,
    }
    if args.stage:
        dest = mgr.stage_demo_writer_qa(
            connection_id=args.connection_id,
            broker=account["broker"],
            login=int(account["mt5_login"]),
            server=account["server"],
            environment=account["environment"],
            data_dir=args.terminal_data_dir,
            compiled_ex5=str(source),
            expected_sha256=actual,
        )
        result["qa_mode"] = "QA_STAGED_NOT_ATTACHED"
        result["staged_ex5"] = dest
    else:
        result["qa_mode"] = "QA_DRY_RUN_VALIDATED"
        result["proposed_experts_dir"] = str(
            Path(experts_dir) / "ABUTRON-PR37-QA")
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
