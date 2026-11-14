"""Reconciliation worker — reads native terminal exports and reconciles.

Runs as `abutron-reconcile.service` (timer-driven every 15 min). Reads the
native history/receipt exports from each account's MT5 terminal and
reconciles local receipts against broker-native ticket/deal/SL/TP
evidence. Missing inventory is reported UNKNOWN, never zero-filled.
"""
from __future__ import annotations

import json
import os
import glob
from typing import List

from ..config import DATA_ROOT, NATIVE_ROOT
from ..events import EventLog, make_event
from ..db import engine_db
from ..execution.receipts import ReceiptManager
from ..reconciliation.reconciler import Reconciler, NativeHistoryEntry


def _read_native_history(connection_id: str) -> List[NativeHistoryEntry]:
    """Read the latest native history export for an account."""
    pattern = os.path.join(NATIVE_ROOT, connection_id,
                           "ABUTRON_NATIVE_HISTORY_V2_*.json")
    matches = sorted(glob.glob(pattern))
    if not matches:
        return []
    try:
        data = json.loads(open(matches[-1]).read())
    except (ValueError, OSError):
        return []
    out: List[NativeHistoryEntry] = []
    for h in data.get("history", []):
        out.append(NativeHistoryEntry(
            ticket=int(h.get("ticket", 0)), login=int(h.get("login", 0)),
            symbol=h.get("symbol", ""), volume=float(h.get("volume", 0)),
            price=float(h.get("price", 0)),
            stop_loss=float(h.get("stop_loss", 0)),
            take_profit=float(h.get("take_profit", 0)),
            state=h.get("state", "OPEN"),
            realized_pnl=float(h.get("realized_pnl", 0)),
            commission=float(h.get("commission", 0)),
            swap=float(h.get("swap", 0)),
            balance=float(h.get("balance", 0)),
            close_time=h.get("close_time", "")))
    return out


def main() -> None:
    log = EventLog(os.path.join(DATA_ROOT, "events.jsonl"))
    receipt_mgr = ReceiptManager(log)
    reconciler = Reconciler(log, receipt_mgr)

    for acct in engine_db.list_accounts():
        cid = acct["connection_id"]
        history = _read_native_history(cid)
        if not history:
            log.append(make_event(
                "reconcile.no_native_history", connection_id=cid,
                broker=acct["broker"], mt5_login=acct["mt5_login"],
                reconciliation_state="UNKNOWN",
                reason_code="NATIVE_HISTORY_MISSING"))
            continue
        report = reconciler.reconcile_account(cid, history)
        log.append(make_event(
            "reconcile.timer_run", connection_id=cid, broker=acct["broker"],
            mt5_login=acct["mt5_login"],
            reconciliation_state="COMPLETED", payload=report))


if __name__ == "__main__":
    main()
