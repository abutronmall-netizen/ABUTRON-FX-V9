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
