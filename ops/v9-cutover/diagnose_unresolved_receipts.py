#!/usr/bin/env python3
"""Read-only diagnostic: classify why each ACK receipt is unresolved.

``reconcile_engine_receipts`` returns only aggregate counts
(``protected``/``unresolved``/``unknown``), which tells an operator that
*something* is stuck but not *why*. This script mirrors the same
classification logic read-only — it never writes to the engine DB, the
CRM DB, or any native file — and reports the exact failure reason for
each receipt that would land in the ``unresolved`` bucket.

This is a diagnostic. It does not fix, certify, or upgrade any receipt.
Fail-closed: every unresolved receipt stays unresolved until matched to
authenticated broker-native protective evidence.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

from abutron.reconciliation.native_crm_6207 import (
    signed_native_exposure,
    match_protective_order,
    broker_position_from_native_deal,
    historical_signed_protection,
    recover_native_delayed_receipt,
)
from abutron.execution.writer_gateway import SingleWriterGateway


def classify_receipt(receipt, candidate, exposure, tick_sizes, *,
                     secret, cid, login, server, files_dir):
    """Return (would_be_unresolved: bool, reason: str, detail: dict).

    Mirrors ``reconcile_engine_receipts`` exactly but read-only.
    """
    # Already protected — skip (not unresolved).
    if (receipt.get("reconciled_state") == "PROTECTED" and
            receipt.get("broker_native_ordercalcprofit_verified") and
            float(receipt.get("broker_native_stop_risk_usd") or 0) > 0 and
            int(receipt.get("native_position_ticket") or 0) > 0):
        return False, "ALREADY_PROTECTED", {}

    exec_state = receipt.get("execution_state")
    if exec_state == "UNKNOWN":
        reason = str(receipt.get("reason_code") or "")
        native_id = (reason.split(";", 1)[0][len("REQUEST_ID="):]
                     if reason.startswith("REQUEST_ID=") else "")
        recovered = recover_native_delayed_receipt(
            files_dir, request_id=native_id, login=login)
        if recovered is None:
            return True, "UNKNOWN_NO_NATIVE_RECEIPT", {"reason_code": reason}
        if recovered.get("state") == "REJECTED":
            return True, "UNKNOWN_RECOVERED_REJECTED", {
                "reason_code": str(recovered.get("reason", ""))[:128]}
        recovered_ticket = int(recovered.get("ticket", 0) or 0)
        if recovered_ticket <= 0:
            return True, "UNKNOWN_RECOVERED_NO_TICKET", {}
        # Recovered ACK — fall through to the ACK protection path below.
        receipt = dict(receipt, native_ticket=recovered_ticket)
    elif exec_state != "ACK":
        return False, "SKIPPED_NON_ACK", {"execution_state": exec_state}

    # --- ACK path (or recovered ACK) ---
    if candidate is None:
        return True, "CANDIDATE_MISSING", {
            "candidate_id": receipt.get("candidate_id")}

    symbol = candidate["symbol"]
    tick = float(tick_sizes.get(symbol, 0.0))
    if not 0 < tick < 100000:
        return True, "TICK_SIZE_INVALID", {"symbol": symbol, "tick": tick}

    position = broker_position_from_native_deal(receipt, candidate)
    native_position = position or receipt.get("native_ticket", 0)
    status = match_protective_order(
        {"ticket": native_position, "state": "ACK"},
        candidate, exposure, tick)

    if status.get("status") != "BROKER_PROTECTED_SLTP" and position:
        historical = historical_signed_protection(
            files_dir, cid=cid, login=login, server=server, secret=secret,
            receipt=receipt, candidate=candidate, tick_size=tick,
            position=position)
        if historical:
            status = historical[0]

    if status.get("status") == "BROKER_PROTECTED_SLTP":
        return False, "WOULD_BE_PROTECTED", {"ticket": status.get("ticket")}

    # Map the non-protected status to a human-readable reason.
    reason_map = {
        "BROKER_POSITION_PROOF_PENDING":
            "POSITION_PROOF_PENDING",
        "BROKER_PROTECTION_NOT_CONFIRMED":
            "PROTECTION_NOT_CONFIRMED",
        "BROKER_STOP_WIDENED":
            "STOP_WIDENED",
        "BROKER_VOLUME_MISMATCH":
            "VOLUME_MISMATCH",
    }
    reason = reason_map.get(status.get("status"), "UNPROTECTED")
    detail = {"status": status.get("status"),
              "native_position": native_position,
              "position_from_deal": position}
    if position is None:
        detail["deal_id"] = str(receipt.get("broker_deal_id") or "")
        detail["note"] = "no native deal match; position not resolved"
    return True, reason, detail


def load_tick_sizes(crm_db: str, cid: str) -> dict:
    """Best-effort tick-size map from the CRM cost report if present."""
    # The cost report is not in the CRM DB; tick sizes are passed via the
    # cost report JSON. For diagnostics we read them from the engine DB's
    # instrument metadata if available, otherwise leave empty (the
    # TICK_SIZE_INVALID branch will report it).
    try:
        with sqlite3.connect("file:" + crm_db + "?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT symbol, tick_size FROM instruments "
                "WHERE connection_id=?", (cid,)).fetchall()
        return {r[0]: float(r[1]) for r in rows if r[1] is not None}
    except (sqlite3.Error, OSError):
        return {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crm-db", default="/var/lib/apex-signup/accounts.sqlite3")
    parser.add_argument("--engine-db", default="/var/lib/abutron-v9-production-6207/engine.sqlite3")
    parser.add_argument("--cid", required=True, help="Connection ID (32 hex)")
    parser.add_argument("--login", type=int, required=True)
    parser.add_argument("--server", default="Deriv-Demo")
    parser.add_argument("--files-dir", required=True,
                        help="MQL5/Files dir for the signed writer")
    parser.add_argument("--tick-sizes", default="",
                        help="Optional JSON file mapping symbol->tick_size")
    args = parser.parse_args(argv)

    tick_sizes: dict = {}
    if args.tick_sizes:
        tick_sizes = json.loads(Path(args.tick_sizes).read_text())
    else:
        tick_sizes = load_tick_sizes(args.crm_db, args.cid)

    secret = SingleWriterGateway.read_existing_secret(args.files_dir)
    exposure, _sha = signed_native_exposure(
        args.files_dir, cid=args.cid, login=args.login,
        server=args.server, secret=secret, ttl=1_000_000_000)

    # Read receipts from the engine DB (read-only).
    receipts = []
    with sqlite3.connect("file:" + args.engine_db + "?mode=ro", uri=True) as db:
        receipts = [dict(zip(
            [c[0] for c in db.execute("SELECT * FROM order_receipts LIMIT 1").description],
            row)) for row in db.execute(
            "SELECT * FROM order_receipts WHERE connection_id=? LIMIT 500",
            (args.cid,))]

    results = []
    for receipt in receipts:
        candidate = _get_candidate(args.engine_db, receipt.get("candidate_id", ""))
        unresolved, reason, detail = classify_receipt(
            receipt, candidate, exposure, tick_sizes,
            secret=secret, cid=args.cid, login=args.login,
            server=args.server, files_dir=args.files_dir)
        entry = {
            "receipt_id": receipt.get("receipt_id"),
            "candidate_id": receipt.get("candidate_id"),
            "symbol": receipt.get("symbol"),
            "execution_state": receipt.get("execution_state"),
            "reconciled_state": receipt.get("reconciled_state"),
            "native_ticket": receipt.get("native_ticket"),
            "unresolved": unresolved,
            "reason": reason,
        }
        if detail:
            entry["detail"] = {k: v for k, v in detail.items()
                               if k not in ("reason_code",)}
            if "reason_code" in detail:
                entry["reason_code"] = detail["reason_code"][:128]
        results.append(entry)

    unresolved_list = [r for r in results if r["unresolved"]]
    report = {
        "connection_id": args.cid,
        "login": args.login,
        "scanned": len(results),
        "unresolved_count": len(unresolved_list),
        "unresolved": unresolved_list,
        "diagnostic_only": True,
        "orders_submitted": 0,
    }
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


def _get_candidate(engine_db: str, candidate_id: str):
    if not candidate_id:
        return None
    try:
        with sqlite3.connect("file:" + engine_db + "?mode=ro", uri=True) as db:
            cols = [c[0] for c in db.execute(
                "SELECT * FROM candidates LIMIT 1").description]
            row = db.execute(
                "SELECT * FROM candidates WHERE candidate_id=?",
                (candidate_id,)).fetchone()
        if row is None:
            return None
        return dict(zip(cols, row))
    except (sqlite3.Error, OSError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
