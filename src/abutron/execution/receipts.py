"""Native receipt handling.

Receipts are broker-originating MT5 ticket/deal evidence. A receipt is
only authoritative when it comes from the native terminal export and is
bound to a candidate + connection. UNKNOWN submit outcomes are recorded
as UNKNOWN and never silently upgraded to success.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List, Optional

from ..config import RECEIPTS_ROOT, utc_now
from .. import db
from ..events import EventLog, make_event
from .state import state_machine


class ReceiptManager:
    def __init__(self, event_log: EventLog):
        self.log = event_log
        os.makedirs(RECEIPTS_ROOT, exist_ok=True)

    def record_native_receipt(self, *, candidate_id: str, connection_id: str,
                              mt5_login: int, symbol: str, direction: str,
                              native_ticket: int, broker_deal_id: str,
                              volume: float, fill_price: float,
                              stop_loss: float, take_profit: float,
                              commission: float, swap: float,
                              execution_state: str,
                              reason_code: str = "") -> Dict[str, Any]:
        receipt_id = f"rcpt_{uuid.uuid4().hex[:12]}"
        state_machine.move(candidate_id, execution_state, reason_code)
        row = {
            "receipt_id": receipt_id, "candidate_id": candidate_id,
            "connection_id": connection_id, "mt5_login": mt5_login,
            "symbol": symbol, "direction": direction,
            "native_ticket": native_ticket,
            "broker_deal_id": broker_deal_id,
            "volume": volume, "fill_price": fill_price,
            "stop_loss": stop_loss, "take_profit": take_profit,
            "commission": commission, "swap": swap,
            "execution_state": execution_state,
            "reconciled_state": "PENDING",
            "received_at": utc_now(), "reason_code": reason_code,
        }
        db.engine_db.add_receipt(row)
        self._persist_file(receipt_id, row)
        self.log.append(make_event(
            "receipt.native", connection_id=connection_id,
            mt5_login=mt5_login, symbol=symbol,
            order_request_id=candidate_id, native_ticket=native_ticket,
            broker_deal_id=broker_deal_id, volume=volume,
            fill_price=fill_price, stop_loss=stop_loss, take_profit=take_profit,
            commission=commission, swap=swap,
            execution_state=execution_state, reason_code=reason_code))
        return row

    def record_unknown(self, *, candidate_id: str, connection_id: str,
                       mt5_login: int, symbol: str, direction: str,
                       volume: float, reason_code: str = "SEND_UNKNOWN") -> str:
        """Broker submit outcome ambiguous — record UNKNOWN, block resubmit."""
        state_machine.move(candidate_id, "UNKNOWN", reason_code)
        receipt_id = f"rcpt_{uuid.uuid4().hex[:12]}"
        row = {
            "receipt_id": receipt_id, "candidate_id": candidate_id,
            "connection_id": connection_id, "mt5_login": mt5_login,
            "symbol": symbol, "direction": direction,
            "native_ticket": 0, "broker_deal_id": "",
            "volume": volume, "fill_price": 0.0,
            "stop_loss": 0.0, "take_profit": 0.0,
            "commission": 0.0, "swap": 0.0,
            "execution_state": "UNKNOWN", "reconciled_state": "UNKNOWN",
            "received_at": utc_now(), "reason_code": reason_code,
        }
        db.engine_db.add_receipt(row)
        self._persist_file(receipt_id, row)
        self.log.append(make_event(
            "receipt.unknown", connection_id=connection_id,
            mt5_login=mt5_login, symbol=symbol,
            order_request_id=candidate_id, volume=volume,
            execution_state="UNKNOWN", reason_code=reason_code))
        return receipt_id

    def reconcile(self, *, receipt_id: str, reconciled_state: str,
                  **evidence) -> None:
        db.engine_db.update_receipt(receipt_id, reconciled_state=reconciled_state,
                                 **evidence)
        self.log.append(make_event(
            "receipt.reconciled", execution_state=reconciled_state,
            reason_code="RECONCILED", payload=evidence))

    def _persist_file(self, receipt_id: str, row: Dict[str, Any]) -> None:
        with open(os.path.join(RECEIPTS_ROOT, f"{receipt_id}.json"), "w") as fh:
            json.dump(row, fh, indent=2)

    def list_receipts(self, connection_id: Optional[str] = None,
                      limit: int = 100) -> List[Dict]:
        return db.engine_db.list_receipts(connection_id, limit)

    def verify_protective_stops(self, *, receipt_id: str,
                                broker_sl: float, broker_tp: float,
                                expected_sl: float, expected_tp: float,
                                tolerance_points: float = 0.0,
                                point: float = 0.0001) -> Dict[str, Any]:
        """Verify broker-side protective SL/TP against the requested geometry.

        Compares the broker's actual stop_loss and take_profit on the
        filled position against the values the brain requested. If the
        broker's SL differs beyond tolerance, the receipt is flagged —
        the native EA's OrderSend must have sent the correct values.

        Returns a dict with verification result and any issues.
        """
        tol = tolerance_points * point
        sl_ok = abs(broker_sl - expected_sl) <= tol if expected_sl else True
        tp_ok = abs(broker_tp - expected_tp) <= tol if expected_tp else True
        issues = []
        if not sl_ok:
            issues.append(
                f"SL_MISMATCH: broker SL {broker_sl} vs requested "
                f"{expected_sl} (tolerance {tol})")
        if not tp_ok:
            issues.append(
                f"TP_MISMATCH: broker TP {broker_tp} vs requested "
                f"{expected_tp} (tolerance {tol})")
        self.reconcile(
            receipt_id=receipt_id,
            reconciled_state="PROTECTED" if (sl_ok and tp_ok) else "UNKNOWN",
            protective_stops_verified=sl_ok and tp_ok,
            broker_sl=broker_sl, broker_tp=broker_tp,
            expected_sl=expected_sl, expected_tp=expected_tp,
            tolerance_points=tolerance_points)
        self.log.append(make_event(
            "receipt.protective_stops_verified",
            order_request_id=receipt_id,
            execution_state="PROTECTED" if (sl_ok and tp_ok) else "UNKNOWN",
            reason_code="SL_TP_VERIFIED" if (sl_ok and tp_ok)
            else "SL_TP_MISMATCH",
            payload={"broker_sl": broker_sl, "broker_tp": broker_tp,
                     "expected_sl": expected_sl, "expected_tp": expected_tp}))
        return {"receipt_id": receipt_id, "sl_ok": sl_ok, "tp_ok": tp_ok,
                "verified": sl_ok and tp_ok, "issues": issues}

    def verify_chandelier_trail(self, *, receipt_id: str,
                                broker_sl: float, broker_trail_stop: float,
                                point: float = 0.0001,
                                tolerance_points: float = 0.0) -> Dict[str, Any]:
        """Verify the broker-side Chandelier trailing stop was applied.

        Confirms the broker's current SL equals the trail stop the brain
        computed. If the broker SL has not been moved to the trail stop,
        the trailing modification was not applied natively.

        Returns a dict with verification result and any issues.
        """
        tol = tolerance_points * point
        trail_ok = abs(broker_sl - broker_trail_stop) <= tol
        issues = []
        if not trail_ok:
            issues.append(
                f"TRAIL_NOT_APPLIED: broker SL {broker_sl} != trail stop "
                f"{broker_trail_stop} (tolerance {tol})")
        self.reconcile(
            receipt_id=receipt_id,
            reconciled_state="PROTECTED" if trail_ok else "UNKNOWN",
            chandelier_trail_verified=trail_ok,
            broker_sl=broker_sl, broker_trail_stop=broker_trail_stop)
        self.log.append(make_event(
            "receipt.chandelier_trail_verified",
            order_request_id=receipt_id,
            execution_state="PROTECTED" if trail_ok else "UNKNOWN",
            reason_code="CHANDELIER_TRAIL_VERIFIED" if trail_ok
            else "CHANDELIER_TRAIL_NOT_APPLIED",
            payload={"broker_sl": broker_sl,
                     "trail_stop": broker_trail_stop}))
        return {"receipt_id": receipt_id, "trail_ok": trail_ok,
                "verified": trail_ok, "issues": issues}
