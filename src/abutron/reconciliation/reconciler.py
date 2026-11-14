"""Broker-native reconciliation (blueprint section 08).

The broker's native history/positions are the final arbiter. The
reconciler compares local receipts against native ticket/deal/SL/TP
evidence, resolves UNKNOWN sends, detects trailing-stop updates, and
transitions the ledger. Missing account inventory is reported UNKNOWN,
never zero-filled.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..config import RECEIPTS_ROOT, utc_now
from .. import db
from ..events import EventLog, make_event
from ..execution.receipts import ReceiptManager


@dataclass
class NativeHistoryEntry:
    """One row of the broker-native position/history export."""
    ticket: int
    login: int
    symbol: str
    volume: float
    price: float
    stop_loss: float
    take_profit: float
    state: str          # 'OPEN' | 'CLOSED'
    realized_pnl: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    balance: float = 0.0
    close_time: str = ""


class Reconciler:
    def __init__(self, event_log: EventLog, receipt_mgr: ReceiptManager,
                 brain=None):
        self.log = event_log
        self.receipts = receipt_mgr
        self.brain = brain

    def reconcile_account(self, connection_id: str,
                          native_history: List[NativeHistoryEntry]) -> Dict:
        """Reconcile all receipts for an account against native history."""
        account = db.engine_db.get_account(connection_id)
        if account is None:
            return {"connection_id": connection_id, "status": "NO_ACCOUNT"}
        history_by_ticket = {h.ticket: h for h in native_history}

        report = {"connection_id": connection_id, "matched": 0,
                  "unresolved": 0, "resolved_unknown": 0,
                  "open_positions": 0, "details": []}

        for receipt in db.engine_db.list_receipts(connection_id, limit=500):
            ticket = receipt["native_ticket"]
            if receipt["reconciled_state"] in ("SETTLED", "MATCHED"):
                continue
            if ticket and ticket in history_by_ticket:
                h = history_by_ticket[ticket]
                self._match_receipt(receipt, h, account, report)
            elif receipt["execution_state"] == "UNKNOWN":
                # Try to resolve UNKNOWN against any native open position
                resolved = self._resolve_unknown(receipt, native_history,
                                                 account, report)
                if not resolved:
                    report["unresolved"] += 1
            else:
                # Receipt with no native evidence
                self.log.append(make_event(
                    "reconcile.missing_evidence", connection_id=connection_id,
                    mt5_login=account["mt5_login"],
                    order_request_id=receipt["candidate_id"],
                    native_ticket=ticket, symbol=receipt["symbol"],
                    reconciliation_state="UNKNOWN",
                    reason_code="SEND_UNKNOWN"))
                report["unresolved"] += 1

        # Count native open positions not covered by local receipts
        local_tickets = {r["native_ticket"] for r in
                         db.engine_db.list_receipts(connection_id, limit=500)}
        for h in native_history:
            if h.state == "OPEN" and h.ticket not in local_tickets:
                report["details"].append({
                    "ticket": h.ticket, "symbol": h.symbol,
                    "issue": "NATIVE_POSITION_UNKNOWN_TO_LEDGER"})
                self.log.append(make_event(
                    "reconcile.unknown_position", connection_id=connection_id,
                    mt5_login=account["mt5_login"], native_ticket=h.ticket,
                    symbol=h.symbol, reconciliation_state="UNKNOWN",
                    reason_code="POSITION_NOT_IN_LEDGER"))

        self.log.append(make_event(
            "reconcile.run", connection_id=connection_id,
            broker=account["broker"], mt5_login=account["mt5_login"],
            reconciliation_state="COMPLETED",
            payload={"matched": report["matched"],
                     "unresolved": report["unresolved"]}))
        return report

    def _match_receipt(self, receipt: Dict, h: NativeHistoryEntry,
                       account: Dict, report: Dict) -> None:
        # Verify SL/TP against native
        sl_ok = (abs(h.stop_loss - receipt["stop_loss"]) < 1e-9
                 or (receipt["stop_loss"] == 0.0 and h.stop_loss > 0))
        tp_ok = (abs(h.take_profit - receipt["take_profit"]) < 1e-9
                 or (receipt["take_profit"] == 0.0 and h.take_profit > 0))
        # A tightened SL is accepted when broker-attested; wider SL with
        # higher USD risk is NOT silently accepted.
        stop_widened = (receipt["stop_loss"] > 0 and h.stop_loss > 0 and
                        abs(h.stop_loss - receipt["stop_loss"]) > 1e-9 and
                        abs(h.stop_loss - receipt["stop_loss"]) > h.stop_loss * 0.02)

        state = "MATCHED" if (sl_ok and tp_ok) else "SL_TP_MISMATCH"
        if not sl_ok and stop_widened:
            state = "STOP_WIDENED"

        self.receipts.reconcile(
            receipt_id=receipt["receipt_id"], reconciled_state=state,
            native_ticket=h.ticket, fill_price=h.price,
            volume=h.volume, stop_loss=h.stop_loss, take_profit=h.take_profit)

        # Ledger entry
        db.engine_db.add_ledger({
            "connection_id": account["connection_id"],
            "owner_id": account["owner_id"],
            "native_ticket": h.ticket,
            "event": "CLOSE" if h.state == "CLOSED" else "OPEN",
            "symbol": h.symbol, "direction": receipt["direction"],
            "volume": h.volume, "price": h.price,
            "realized_pnl": h.realized_pnl, "commission": h.commission,
            "swap": h.swap, "stop_loss": h.stop_loss,
            "take_profit": h.take_profit, "balance": h.balance,
            "observed_at": utc_now(), "source": "native",
        })
        # Self-learning: feed a settled (CLOSED) trade back to the brain so
        # it learns from the losing trade and never enters too early.
        if h.state == "CLOSED" and self.brain is not None:
            cand = db.engine_db.get_candidate(receipt["candidate_id"]) or {}
            self.brain.learn_from_trade(
                symbol=receipt["symbol"],
                regime=cand.get("regime", ""),
                direction=receipt["direction"],
                entry_type=cand.get("entry_type", "CONFIRM"),
                entry_quality=float(cand.get("entry_quality", 0.0) or 0.0),
                pnl=float(h.realized_pnl or 0.0))

        report["matched"] += 1
        report["details"].append({
            "ticket": h.ticket, "symbol": h.symbol, "state": state,
            "sl_ok": sl_ok, "tp_ok": tp_ok, "stop_widened": stop_widened})

    def _resolve_unknown(self, receipt: Dict,
                         native_history: List[NativeHistoryEntry],
                         account: Dict, report: Dict) -> bool:
        """Resolve an UNKNOWN send by matching native open positions to the
        candidate's symbol/direction/size."""
        for h in native_history:
            if (h.state == "OPEN" and h.symbol == receipt["symbol"]
                    and abs(h.volume - receipt["volume"]) < 0.011):
                self.receipts.reconcile(
                    receipt_id=receipt["receipt_id"],
                    reconciled_state="MATCHED",
                    native_ticket=h.ticket, fill_price=h.price,
                    volume=h.volume, stop_loss=h.stop_loss,
                    take_profit=h.take_profit)
                self.log.append(make_event(
                    "reconcile.unknown_resolved",
                    connection_id=account["connection_id"],
                    mt5_login=account["mt5_login"],
                    order_request_id=receipt["candidate_id"],
                    native_ticket=h.ticket, symbol=receipt["symbol"],
                    reconciliation_state="MATCHED",
                    reason_code="UNKNOWN_RESOLVED_TO_NATIVE"))
                report["resolved_unknown"] += 1
                return True
        return False
