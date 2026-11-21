#!/usr/bin/env python3
"""Read-only diagnostic: investigate a single UNKNOWN receipt end-to-end.

When a V9 order dispatch returns UNKNOWN (the broker outcome is
ambiguous — the request may or may not have reached the EA), the
receipt is recorded with the request ID embedded in the reason code and
left unresolved. ``recover_native_delayed_receipt`` already searches
for a delayed native receipt on each reconciliation pass, but when no
receipt is found it is hard to tell whether:

  * The order never reached the EA (transport/socket timeout)
  * The EA processed it but the receipt file was never written
  * The EA rejected it silently (no receipt file)
  * The order was filled but the position has since closed

This diagnostic answers those questions read-only. It:

  1. Extracts the REQUEST_ID from the receipt's reason code
  2. Searches the native receipt file directory for a matching receipt
  3. Checks current signed exposure for a matching position
  4. Searches historical signed exposure snapshots for a post-entry
     position that may have since closed
  5. Reports a classification and the evidence found

Never writes to any DB or native file. Fail-closed: the receipt stays
UNKNOWN unless authenticated broker-native evidence is found.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

from abutron.reconciliation.native_crm_6207 import (
    signed_native_exposure,
    recover_native_delayed_receipt,
)
from abutron.execution.writer_gateway import SingleWriterGateway


def extract_request_id(reason_code: str) -> str:
    """Extract ABUTRON_REQ_* request ID from a UNKNOWN reason code.

    The dispatcher formats unknown reasons as
    ``REQUEST_ID=<id>;<reason>`` when a request ID is present.
    """
    if not reason_code:
        return ""
    if reason_code.startswith("REQUEST_ID="):
        return reason_code.split(";", 1)[0][len("REQUEST_ID="):]
    return ""


def search_receipt_files(files_dir: str, request_id: str,
                         login: int) -> list:
    """Search native receipt files for a matching request_id."""
    if not request_id:
        return []
    directory = Path(files_dir)
    matches = []
    for path in sorted(directory.glob("ABUTRON_NATIVE_RECEIPTS_V2_*.txt"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:120]:
        try:
            raw = path.read_text(encoding="utf-8-sig")
            j = json.loads(raw)
        except (OSError, UnicodeError, ValueError, TypeError):
            continue
        if j.get("request_id") == request_id:
            if str(j.get("login", "")) != str(login):
                continue
            matches.append({"path": str(path), "receipt": j})
    return matches


def find_position_in_exposure(exposure: dict, *, symbol: str,
                              direction: str, volume: float) -> list:
    """Find positions in an exposure snapshot matching the candidate."""
    matches = []
    for pos in exposure.get("positions", []):
        if (pos.get("symbol") == symbol
                and pos.get("direction") == direction
                and abs(float(pos.get("volume", 0)) - volume) < 1e-4):
            matches.append(pos)
    return matches


def search_historical_position(files_dir: str, *, login: int, cid: str,
                               server: str, secret: str, receipt: dict,
                               candidate: dict) -> dict:
    """Search historical signed exposure snapshots for a post-entry position.

    Scans the 10-minute window after the receipt's received_at for any
    signed exposure snapshot containing a matching position.
    """
    try:
        entry_ts = dt.datetime.fromisoformat(
            str(receipt["received_at"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, TypeError):
        return {"found": False, "reason": "invalid_received_at"}

    root = Path(files_dir)
    prefix = "ABUTRON_NATIVE_EXPOSURE_V2_ABUTRON_NATIVE_" + str(login) + "_"
    for path in sorted(root.glob(prefix + "*.txt")):
        try:
            stamp = int(path.stem[len(prefix):])
        except ValueError:
            continue
        if not entry_ts <= stamp <= entry_ts + 600:
            continue
        try:
            exposure, _sha = signed_native_exposure(
                files_dir, cid=cid, login=login, server=server,
                secret=secret, ttl=1_000_000_000, source_file=path)
            matches = find_position_in_exposure(
                exposure, symbol=candidate["symbol"],
                direction=candidate["direction"],
                volume=float(candidate.get("volume", 0)))
            if matches:
                return {"found": True, "stamp": stamp,
                        "positions": matches, "source_file": str(path)}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return {"found": False, "reason": "no_matching_position_in_window"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-db",
                        default="/var/lib/abutron-v9-production-6207/engine.db")
    parser.add_argument("--cid", required=True, help="Connection ID (32 hex)")
    parser.add_argument("--receipt-id", required=True,
                        help="The UNKNOWN receipt_id to investigate")
    parser.add_argument("--login", type=int, required=True)
    parser.add_argument("--server", default="Deriv-Demo")
    parser.add_argument("--files-dir", required=True,
                        help="MQL5/Files dir for the signed writer")
    args = parser.parse_args(argv)

    # 1. Read the receipt from the engine DB (read-only).
    with sqlite3.connect("file:" + args.engine_db + "?mode=ro", uri=True) as db:
        cols = [c[0] for c in db.execute(
            "SELECT * FROM order_receipts LIMIT 1").description]
        row = db.execute(
            "SELECT * FROM order_receipts WHERE receipt_id=?",
            (args.receipt_id,)).fetchone()
    if row is None:
        print(json.dumps({"error": "receipt not found",
                          "receipt_id": args.receipt_id}, sort_keys=True))
        return 1
    receipt = dict(zip(cols, row))

    # 2. Read the candidate.
    candidate_id = receipt.get("candidate_id", "")
    candidate = None
    if candidate_id:
        with sqlite3.connect("file:" + args.engine_db + "?mode=ro", uri=True) as db:
            ccols = [c[0] for c in db.execute(
                "SELECT * FROM candidates LIMIT 1").description]
            crow = db.execute(
                "SELECT * FROM candidates WHERE candidate_id=?",
                (candidate_id,)).fetchone()
        if crow:
            candidate = dict(zip(ccols, crow))

    # 3. Extract request ID.
    reason_code = str(receipt.get("reason_code") or "")
    request_id = extract_request_id(reason_code)

    # 4. Search native receipt files.
    receipt_matches = search_receipt_files(
        args.files_dir, request_id, args.login)

    # 5. Check current signed exposure.
    secret = SingleWriterGateway.read_existing_secret(args.files_dir)
    current_exposure, _sha = signed_native_exposure(
        args.files_dir, cid=args.cid, login=args.login,
        server=args.server, secret=secret, ttl=1_000_000_000)
    current_matches = []
    if candidate:
        current_matches = find_position_in_exposure(
            current_exposure, symbol=candidate["symbol"],
            direction=candidate["direction"],
            volume=float(candidate.get("volume", 0)))

    # 6. Search historical exposure.
    historical = {"found": False}
    if candidate:
        historical = search_historical_position(
            args.files_dir, login=args.login, cid=args.cid,
            server=args.server, secret=secret, receipt=receipt,
            candidate=candidate)

    # 7. Classify.
    if receipt_matches:
        state = "RECEIPT_FOUND"
        classification = (
            "A native receipt file exists for this request. The EA "
            "processed the order. Check the receipt state (ACK/REJECTED) "
            "and reconcile accordingly.")
    elif current_matches:
        state = "POSITION_OPEN_IN_CURRENT_EXPOSURE"
        classification = (
            "A matching position exists in current signed exposure but "
            "no receipt file was found. The order was likely filled but "
            "the receipt was not written. Independent position proof "
            "exists; reconcile against the position.")
    elif historical.get("found"):
        state = "POSITION_IN_HISTORICAL_EXPOSURE"
        classification = (
            "A matching position exists in a historical signed exposure "
            "snapshot (post-entry window). The position may have since "
            "closed. Reconcile using historical signed protection.")
    else:
        state = "NO_EVIDENCE_FOUND"
        classification = (
            "No native receipt, no current position, and no historical "
            "position found. The order likely never reached the EA "
            "(transport/socket timeout) or was rejected without a "
            "receipt. Fail-closed: remains UNKNOWN. No retry — "
            "at-most-once dispatch.")

    report = {
        "receipt_id": receipt.get("receipt_id"),
        "candidate_id": candidate_id,
        "symbol": receipt.get("symbol"),
        "direction": receipt.get("direction"),
        "volume": receipt.get("volume"),
        "execution_state": receipt.get("execution_state"),
        "reason_code": reason_code,
        "request_id": request_id,
        "received_at": receipt.get("received_at"),
        "classification": state,
        "detail": classification,
        "native_receipt_files": len(receipt_matches),
        "current_exposure_matches": len(current_matches),
        "historical_exposure_match": historical.get("found", False),
        "diagnostic_only": True,
        "orders_submitted": 0,
    }
    if receipt_matches:
        report["receipt_state"] = receipt_matches[0]["receipt"].get("state")
        report["receipt_reason"] = receipt_matches[0]["receipt"].get("reason")
    if historical.get("found"):
        report["historical_stamp"] = historical.get("stamp")
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
