#!/usr/bin/env python3
"""Read-only Deriv DEMO trailing SL/TP audit against authenticated native exposure.

Reads only CRM SQLite (mode=ro), per-account QA files, and per-account native costs.
Does not create ACKs, change positions, submit orders, mutate receipts, or store secrets.
Run with PYTHONDONTWRITEBYTECODE=1 and the installed ABUTRON V9 PYTHONPATH.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

ACCOUNT_SUFFIXES = ("6207", "7593", "9244")
EXPECTED_RETCODE = 10009


def _position(exposure: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any] | None:
    matches = [
        p for p in exposure.get("positions", [])
        if str(p.get("ticket")) == str(receipt["ticket"])
        and str(p.get("symbol")) == str(receipt["symbol"])
    ]
    return matches[0] if len(matches) == 1 else None


def _near(x: Any, y: Any, tick_size: float) -> bool:
    return abs(float(x) - float(y)) <= tick_size * 1.1


def evaluate_proof(
    receipt: dict[str, Any],
    signed_pre: list[tuple[float, dict[str, Any]]],
    signed_post: list[tuple[float, dict[str, Any]]],
    tick_size: float,
) -> dict[str, Any]:
    """Never infer a broker fill or trailing modification solely from an unsigned receipt."""
    if not tick_size > 0:
        return {"classification": "INVALID_TICK_SIZE"}
    if receipt.get("state") != "BROKER_READBACK_VERIFIED" or int(receipt.get("retcode", 0)) != EXPECTED_RETCODE:
        return {"classification": "RECEIPT_NOT_BROKER_VERIFIED"}
    old, target, broker, tp = (
        float(receipt[key]) for key in ("previous_sl", "requested_sl", "broker_sl", "tp")
    )
    if min(old, target, broker, tp) <= 0 or not _near(target, broker, tick_size):
        return {"classification": "INVALID_REPORTED_SL_TP"}
    before = [(ts, p) for ts, e in signed_pre if (p := _position(e, receipt)) is not None]
    after = [(ts, p) for ts, e in signed_post if (p := _position(e, receipt)) is not None]
    if not before:
        return {"classification": "SIGNED_PRE_POSITION_MISSING"}
    if not after:
        return {"classification": "SIGNED_POST_POSITION_MISSING"}

    for pre_time, prev in reversed(before):
        if not _near(prev.get("sl", 0), old, tick_size) or not _near(prev.get("tp", 0), tp, tick_size):
            continue
        direction = prev.get("direction")
        if direction not in ("BUY", "SELL"):
            continue
        if not (target > old + tick_size * 0.9 if direction == "BUY" else target < old - tick_size * 0.9):
            continue
        for post_time, current in after:
            if post_time < pre_time or current.get("direction") != direction:
                continue
            if not _near(current.get("sl", 0), broker, tick_size) or not _near(current.get("tp", 0), tp, tick_size):
                continue
            if not _near(current.get("volume", 0), prev.get("volume", 0), 0.00001):
                continue
            return {
                "classification": "SIGNED_PRE_POST_PROOF",
                "direction": direction,
                "original_sl": old,
                "new_sl": broker,
                "tp_unchanged": True,
                "pre_utc": datetime.fromtimestamp(pre_time, timezone.utc).isoformat(),
                "post_utc": datetime.fromtimestamp(post_time, timezone.utc).isoformat(),
            }
    return {"classification": "SIGNED_POSITION_DETAILS_MISMATCH"}


def inspect_account(
    suffix: str,
    crm_db: Path,
    qa_base: Path,
    cost_dir: Path,
    pre_seconds: float = 65,
    post_seconds: float = 95,
) -> dict[str, Any]:
    if suffix not in ACCOUNT_SUFFIXES:
        raise ValueError("Only configured Deriv DEMO accounts are supported")
    from abutron.execution.writer_gateway import SingleWriterGateway
    from abutron.reconciliation.native_crm_6207 import signed_native_exposure

    with sqlite3.connect(f"file:{crm_db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT id,account_reference,broker_server FROM apex_mt5_connections "
            "WHERE broker=? AND environment=? AND status=? AND account_reference LIKE ?",
            ("Deriv", "DEMO", "CONNECTED", "%" + suffix),
        ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"Authenticated CRM account lookup not unique for {suffix}")
    cid, login, server = rows[0]
    qa = qa_base / f"abutron-pr37-direct-writer-demo{suffix}-qa" / "terminal/MQL5/Files"
    if not qa.is_dir() or qa.is_symlink():
        raise RuntimeError(f"Native QA file directory unavailable for {suffix}")
    secret = SingleWriterGateway.read_existing_secret(str(qa))
    costs_path = cost_dir / ("abutron-v4-broker-crm-supervisor-6207" if suffix == "6207" else f"abutron-deriv-v4-{suffix}") / f"v4-native-execution-cost-{suffix}.json"
    cost = json.loads(costs_path.read_text())
    specs = cost.get("instruments") or {}
    snapshots = sorted((p.stat().st_mtime, p) for p in qa.glob("ABUTRON_NATIVE_EXPOSURE_V2_*.txt"))
    times = [t for t, _ in snapshots]
    receipts = sorted(qa.glob("ABUTRON_NATIVE_TRAILING_V1_*.json"), key=lambda p: p.stat().st_mtime)
    outcomes: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for file in receipts:
        rec = json.loads(file.read_text(encoding="utf-8-sig"))
        timestamp = file.stat().st_mtime
        summary = {
            "receipt_utc": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "ticket_suffix": str(rec.get("ticket", ""))[-6:],
            "symbol": rec.get("symbol"),
        }
        if str(rec.get("login")) != str(login) or rec.get("connection_id") != cid:
            result = {"classification": "RECEIPT_ACCOUNT_IDENTITY_MISMATCH"}
        else:
            tick_size = float(specs.get(rec.get("symbol"), {}).get("tick_size") or 0)
            j = bisect_left(times, timestamp)
            pre: list[tuple[float, dict[str, Any]]] = []
            post: list[tuple[float, dict[str, Any]]] = []
            errors = 0
            for label, collection in (
                ("pre", snapshots[max(0, j - 20):j]),
                ("post", snapshots[j:j + 24]),
            ):
                for stamp, snapshot in collection:
                    delta = abs(stamp - timestamp)
                    if delta > (pre_seconds if label == "pre" else post_seconds):
                        continue
                    try:
                        verified, _ = signed_native_exposure(
                            str(qa), cid=cid, login=int(login), server=server,
                            secret=secret, ttl=1000000000, source_file=snapshot,
                        )
                        (pre if label == "pre" else post).append((stamp, verified))
                    except Exception:
                        errors += 1
            result = evaluate_proof(rec, pre, post, tick_size)
            if errors:
                result["hmac_or_source_errors"] = errors
                if result["classification"] == "SIGNED_PRE_POST_PROOF":
                    result["classification"] = "SIGNED_INPUT_INTEGRITY_ERROR"
        counts[result["classification"]] += 1
        outcomes.append({**summary, **result})
    return {
        "suffix": suffix,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "receipt_count": len(receipts),
        "verified": counts["SIGNED_PRE_POST_PROOF"],
        "classifications": dict(counts),
        "conclusion": "NO_TRAILING_EVENTS" if not receipts else (
            "ALL_VERIFIED" if counts["SIGNED_PRE_POST_PROOF"] == len(receipts) else "INCOMPLETE"),
        "events": outcomes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, choices=ACCOUNT_SUFFIXES + ("all",))
    parser.add_argument("--crm-db", type=Path, default=Path("/var/lib/apex-signup/accounts.sqlite3"))
    parser.add_argument("--qa-base", type=Path, default=Path("/var/lib/private"))
    parser.add_argument("--cost-dir", type=Path, default=Path("/var/lib"))
    args = parser.parse_args()
    accounts = ACCOUNT_SUFFIXES if args.account == "all" else (args.account,)
    result = [
        inspect_account(a, args.crm_db, args.qa_base, args.cost_dir)
        for a in accounts
    ]
    print(json.dumps(result if args.account == "all" else result[0], sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
