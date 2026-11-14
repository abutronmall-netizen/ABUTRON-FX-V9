"""Broker-native reconciliation (blueprint section 08).

The broker's native history/positions are the final arbiter. The
reconciler compares local receipts against native ticket/deal/SL/TP
evidence, resolves UNKNOWN sends, detects trailing-stop updates, and
transitions the ledger. Missing account inventory is reported UNKNOWN,
never zero-filled.
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter
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
    # Only broker-native identity evidence may populate these optional fields.
    # A V9 application-created candidate id or broker deal guess is not proof.
    deal_id: str = ""
    direction: str = ""
    candidate_id: str = ""


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
        # Partial fills or duplicate native exports can reuse an order ticket.
        # Do not arbitrarily pick the last row and certify a different fill.
        counts = Counter(h.ticket for h in native_history)
        ambiguous_tickets = {ticket for ticket, count in counts.items()
                             if count > 1}
        history_by_ticket = {h.ticket: h for h in native_history
                             if h.ticket > 0 and h.ticket not in ambiguous_tickets}

        report = {"connection_id": connection_id, "matched": 0,
                  "unresolved": 0, "resolved_unknown": 0,
                  "open_positions": 0, "details": []}

        for receipt in db.engine_db.list_receipts(connection_id, limit=500):
            ticket = receipt["native_ticket"]
            if receipt["reconciled_state"] in ("SETTLED", "MATCHED"):
                continue
            if ticket in ambiguous_tickets:
                report["unresolved"] += 1
                report["details"].append({
                    "ticket": ticket, "issue": "NATIVE_TICKET_AMBIGUOUS"})
            elif ticket and ticket in history_by_ticket:
                h = history_by_ticket[ticket]
                if not self._match_receipt(receipt, h, account, report):
                    report["unresolved"] += 1
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

    @staticmethod
    def _protected_native_geometry(h: NativeHistoryEntry,
                                   direction: str) -> bool:
        values = (h.volume, h.price, h.stop_loss, h.take_profit)
        try:
            if not all(math.isfinite(float(x)) and float(x) > 0 for x in values):
                return False
        except (ValueError, TypeError, OverflowError):
            return False
        if direction == "BUY":
            return h.stop_loss < h.price < h.take_profit
        if direction == "SELL":
            return h.take_profit < h.price < h.stop_loss
        return False

    def _match_receipt(self, receipt: Dict, h: NativeHistoryEntry,
                       account: Dict, report: Dict) -> bool:
        """Require exact broker identity + protected stops before ledger entry.

        A native open position is not proof of this order unless its broker
        ticket and deal match the acknowledged order on the same MT5 login.
        """
        direction = receipt.get("direction", "")
        reason = ""
        if (h.login != account["mt5_login"] or
                h.login != receipt["mt5_login"] or
                h.symbol != receipt["symbol"] or
                h.ticket <= 0 or
                h.ticket != receipt["native_ticket"] or
                h.state not in ("OPEN", "CLOSED")):
            reason = "NATIVE_ACCOUNT_TICKET_OR_SYMBOL_MISMATCH"
        elif (not h.deal_id or
              str(h.deal_id) != str(receipt.get("broker_deal_id", ""))):
            reason = "NATIVE_DEAL_ID_NOT_ATTESTED"
        elif h.direction != direction:
            reason = "NATIVE_DIRECTION_MISMATCH"
        elif (not self._protected_native_geometry(h, direction) or
              not math.isfinite(float(receipt.get("volume", 0))) or
              abs(h.volume - float(receipt["volume"])) > 1e-8):
            reason = "NATIVE_PROTECTED_POSITION_OR_VOLUME_INVALID"
        else:
            prior_sl = float(receipt.get("stop_loss", 0.0))
            prior_tp = float(receipt.get("take_profit", 0.0))
            if (prior_sl <= 0 or prior_tp <= 0 or
                    not math.isfinite(prior_sl) or not math.isfinite(prior_tp)):
                reason = "ORIGINAL_PROTECTIVE_STOPS_MISSING"
            elif abs(prior_tp - h.take_profit) > 1e-9:
                reason = "NATIVE_TAKE_PROFIT_MISMATCH"
            elif ((direction == "BUY" and h.stop_loss + 1e-9 < prior_sl) or
                  (direction == "SELL" and h.stop_loss - 1e-9 > prior_sl)):
                reason = "NATIVE_STOP_WIDENED"
        if reason:
            report["details"].append({
                "ticket": h.ticket, "symbol": h.symbol, "issue": reason})
            self.log.append(make_event(
                "reconcile.evidence_rejected",
                connection_id=account["connection_id"],
                mt5_login=account["mt5_login"],
                native_ticket=h.ticket, symbol=h.symbol,
                reconciliation_state="UNKNOWN", reason_code=reason))
            return False

        self.receipts.reconcile(
            receipt_id=receipt["receipt_id"], reconciled_state="MATCHED",
            native_ticket=h.ticket, fill_price=h.price,
            volume=h.volume, stop_loss=h.stop_loss, take_profit=h.take_profit)

        db.engine_db.add_ledger({
            "connection_id": account["connection_id"],
            "owner_id": account["owner_id"],
            "native_ticket": h.ticket,
            "event": "CLOSE" if h.state == "CLOSED" else "OPEN",
            "symbol": h.symbol, "direction": direction,
            "volume": h.volume, "price": h.price,
            "realized_pnl": h.realized_pnl, "commission": h.commission,
            "swap": h.swap, "stop_loss": h.stop_loss,
            "take_profit": h.take_profit, "balance": h.balance,
            "observed_at": utc_now(), "source": "native",
        })
        if h.state == "CLOSED" and self.brain is not None:
            cand = db.engine_db.get_candidate(receipt["candidate_id"]) or {}
            self.brain.learn_from_trade(
                symbol=receipt["symbol"],
                regime=cand.get("regime", ""),
                direction=direction,
                entry_type=cand.get("entry_type", "CONFIRM"),
                entry_quality=float(cand.get("entry_quality", 0.0) or 0.0),
                pnl=float(h.realized_pnl or 0.0))

        report["matched"] += 1
        report["details"].append({
            "ticket": h.ticket, "symbol": h.symbol, "state": "MATCHED",
            "sl_ok": abs(h.stop_loss - receipt["stop_loss"]) <= 1e-9,
            "tp_ok": True, "stop_tightened": h.stop_loss != receipt["stop_loss"]})
        return True

    def _resolve_unknown(self, receipt: Dict,
                         native_history: List[NativeHistoryEntry],
                         account: Dict, report: Dict) -> bool:
        """No fuzzy symbol/lot match can turn an UNKNOWN into a filled order.

        Native evidence must include the exact original candidate identity,
        broker deal identity, protected stop geometry, and MT5 account.
        """
        matches = [
            h for h in native_history
            if h.candidate_id == receipt["candidate_id"]
            and h.candidate_id
            and h.deal_id
            and h.direction == receipt["direction"]
            and h.login == receipt["mt5_login"] == account["mt5_login"]
            and h.symbol == receipt["symbol"]
            and h.state == "OPEN"
            and h.ticket > 0
            and math.isfinite(float(h.volume))
            and abs(h.volume - receipt["volume"]) < 1e-8
            and self._protected_native_geometry(h, receipt["direction"])
        ]
        if len(matches) != 1:
            report["details"].append({
                "candidate_id": receipt["candidate_id"],
                "issue": "UNKNOWN_NATIVE_IDENTITY_UNVERIFIED"})
            return False
        h = matches[0]
        # UNKNOWN had no authoritative post-send stops. Candidate requested
        # geometry is independently checked before broker evidence is accepted.
        cand = db.engine_db.get_candidate(receipt["candidate_id"]) or {}
        if (cand.get("connection_id") != account["connection_id"] or
                cand.get("direction") != receipt["direction"] or
                cand.get("symbol") != h.symbol):
            return False
        orig_sl, orig_tp = float(cand.get("stop_loss", 0)), float(
            cand.get("take_profit", 0))
        if not (orig_sl > 0 and orig_tp > 0 and
                math.isfinite(orig_sl) and math.isfinite(orig_tp)):
            return False
        if abs(h.take_profit - orig_tp) > 1e-9:
            return False
        if ((receipt["direction"] == "BUY" and h.stop_loss + 1e-9 < orig_sl)
                or (receipt["direction"] == "SELL" and h.stop_loss - 1e-9 > orig_sl)):
            return False
        # This marks the receipt reconciled based on independently sourced
        # broker evidence, never asserts that the original app ACK was valid.
        self.receipts.reconcile(
            receipt_id=receipt["receipt_id"], reconciled_state="MATCHED",
            native_ticket=h.ticket, fill_price=h.price,
            volume=h.volume, stop_loss=h.stop_loss,
            take_profit=h.take_profit,
            broker_deal_id=h.deal_id)
        self.log.append(make_event(
            "reconcile.unknown_resolved",
            connection_id=account["connection_id"],
            mt5_login=account["mt5_login"],
            order_request_id=receipt["candidate_id"], native_ticket=h.ticket,
            symbol=h.symbol, reconciliation_state="MATCHED",
            reason_code="EXACT_NATIVE_CANDIDATE_AND_DEAL_PROOF"))
        report["resolved_unknown"] += 1
        return True
