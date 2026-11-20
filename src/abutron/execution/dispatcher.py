"""Per-account native order dispatcher.

Reads reserved candidates for an account, checks writer readiness, sends
the order through the single-writer gateway, and records native receipts
(ACK / UNKNOWN / REJECTED). UNKNOWN receipts block resubmission until
reconciliation resolves them — this is the blueprint's "do not resubmit
under uncertain ACK" rule.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ..config import RECEIPTS_ROOT, utc_now
from ..crypto import make_activation_revision
from .. import db
from ..events import EventLog, make_event
from .receipts import ReceiptManager
from .writer_gateway import SingleWriterGateway


class NativeDispatcher:
    def __init__(self, event_log: EventLog,
                 gateway: SingleWriterGateway,
                 receipt_mgr: ReceiptManager):
        self.log = event_log
        self.gateway = gateway
        self.receipts = receipt_mgr

    def dispatch_account(self, connection_id: str) -> Dict[str, Any]:
        """Dispatch all RESERVED candidates for one account.

        Before sending, each candidate is gated by the owner-wide
        exposure guard (BLOCKED_INCOMPLETE_EXPOSURE). If the
        aggregate stop risk cannot be verified against real contract
        specs — or exceeds the approved portfolio ceiling — the
        candidate is blocked before any native order is sent.
        """
        account = db.engine_db.get_account(connection_id)
        if account is None:
            return {"connection_id": connection_id, "sent": 0,
                    "error": "ACCOUNT_NOT_FOUND"}

        # Pre-flight: verify owner-wide exposure with real contract specs.
        owner_id = account.get("owner_id", "")
        if not owner_id:
            return {"connection_id": connection_id, "sent": 0,
                    "error": "OWNER_ID_UNVERIFIED"}
        if owner_id:
            # Never authorize an order from registry-estimated stop risk.
            # Only independently reconciled, account-bound MT5 position
            # inventories and OrderCalcProfit stop losses may open headroom.
            from ..risk.risk_engine import RiskEngine
            exposure = RiskEngine(self.log).aggregate_owner_risk(owner_id)
            if (exposure.get("owner_risk_certified") is not True
                    or exposure.get("cap_exceeded") is not False):
                # Block all RESERVED candidates for this owner — do NOT
                # send any order until exposure is reconciled.
                blocked = self._block_for_incomplete_exposure(
                    connection_id, owner_id, exposure)
                return {"connection_id": connection_id, "sent": 0,
                        "blocked": blocked,
                        "error": "BLOCKED_INCOMPLETE_EXPOSURE",
                        "exposure": exposure}

        # Collect reserved (RESERVED state) candidates
        candidates = [self._load_candidate(cid)
                      for cid in self._reserved_candidate_ids(connection_id)]
        results = {"connection_id": connection_id, "sent": 0,
                   "ack": 0, "unknown": 0, "rejected": 0,
                   "details": []}

        for cand in candidates:
            if cand is None:
                continue
            # Idempotency: skip candidates already beyond RESERVED
            if cand.get("execution_state") not in ("RESERVED", "SEND"):
                continue
            outcome = self._dispatch_one(account, cand)
            results["details"].append(outcome)
            if outcome["outcome"] == "ACK":
                results["sent"] += 1
                results["ack"] += 1
            elif outcome["outcome"] == "UNKNOWN":
                results["unknown"] += 1
            elif outcome["outcome"] == "REJECTED":
                results["rejected"] += 1
        return results

    def _block_for_incomplete_exposure(self, connection_id: str,
                                       owner_id: str,
                                       exposure: Dict) -> List[str]:
        """Block all RESERVED candidates when exposure is incomplete/unsafe.

        Sets candidate state to BLOCKED_INCOMPLETE_EXPOSURE and logs
        the event. Returns the list of blocked candidate IDs.
        """
        blocked_ids = []
        from .state import state_machine
        from ..events import make_event
        for cid in self._reserved_candidate_ids(connection_id):
            cand = self._load_candidate(cid)
            if cand is None:
                continue
            if cand.get("execution_state") not in ("RESERVED", "SEND"):
                continue
            state_machine.move(cid, "RESERVED", "RESERVED")
            state_machine.move(cid, "BLOCKED_INCOMPLETE_EXPOSURE",
                              "OWNER_EXPOSURE_UNVERIFIED")
            db.engine_db.set_candidate_state(
                cid, "BLOCKED_INCOMPLETE_EXPOSURE",
                "OWNER_EXPOSURE_UNVERIFIED")
            blocked_ids.append(cid)
        self.log.append(make_event(
            "dispatch.blocked_incomplete_exposure",
            connection_id=connection_id, owner_id=owner_id,
            execution_state="BLOCKED_INCOMPLETE_EXPOSURE",
            reason_code="OWNER_EXPOSURE_UNVERIFIED",
            payload={"aggregate_stop_risk_usd":
                     exposure.get("aggregate_stop_risk_usd", 0.0),
                     "cap_exceeded": exposure.get("cap_exceeded", False),
                     "issues": exposure.get("issues", [])}))
        return blocked_ids

    def _dispatch_one(self, account: Dict[str, Any],
                      candidate: Dict[str, Any]) -> Dict[str, Any]:
        cid = account["connection_id"]
        broker = account["broker"]
        sent = self.gateway.send_order(
            connection_id=cid, candidate=candidate, account=account,
            broker=broker)
        outcome = sent.get("outcome")

        if outcome == "ACK":
            # Use the ACTUAL native execution price and volume from the
            # broker's OrderSend result (res.price, res.volume), not the
            # model's proposed price/volume. The EA writes these into the
            # receipt file and returns them in the socket response.
            receipt_ref = sent.get("receipt_ref", {})
            actual_price = float(receipt_ref.get("price", 0.0) or 0.0)
            actual_volume = float(receipt_ref.get("volume", 0.0) or 0.0)
            if actual_price <= 0:
                # Fall back to candidate price only if the EA did not
                # report the actual fill (older EA versions).
                actual_price = float(candidate.get("trigger_price",
                                                   candidate.get("price", 0.0)))
            if actual_volume <= 0:
                actual_volume = float(candidate.get("volume", 0.0))
            self.receipts.record_native_receipt(
                candidate_id=candidate["candidate_id"],
                connection_id=cid, mt5_login=account["mt5_login"],
                symbol=candidate["symbol"], direction=candidate["direction"],
                native_ticket=receipt_ref.get("ticket", 0),
                broker_deal_id=receipt_ref.get("deal", ""),
                volume=actual_volume,
                fill_price=actual_price,
                stop_loss=candidate["stop_loss"],
                take_profit=candidate["take_profit"],
                commission=0.0, swap=0.0,
                execution_state="ACK", reason_code="NATIVE_ACK")
            db.engine_db.set_candidate_state(candidate["candidate_id"], "ACK")
            return {"candidate_id": candidate["candidate_id"],
                    "outcome": "ACK", "receipt": sent["receipt_ref"]}

        if outcome == "UNKNOWN":
            rid = self.receipts.record_unknown(
                candidate_id=candidate["candidate_id"], connection_id=cid,
                mt5_login=account["mt5_login"],
                symbol=candidate["symbol"],
                direction=candidate["direction"],
                volume=candidate["volume"],
                reason_code=sent.get("reason", "SEND_UNKNOWN"))
            db.engine_db.set_candidate_state(candidate["candidate_id"], "UNKNOWN",
                                          sent.get("reason", "SEND_UNKNOWN"))
            return {"candidate_id": candidate["candidate_id"],
                    "outcome": "UNKNOWN", "receipt_id": rid}

        # REJECTED — expose the actual MT5 return code from the EA's
        # native receipt so operators can diagnose OrderCheck failures.
        receipt_ref = sent.get("receipt_ref", {}) or {}
        reject_reason = sent.get("reason", "REJECTED")
        mt5_retcode = receipt_ref.get("reason", "") or reject_reason
        db.engine_db.set_candidate_state(candidate["candidate_id"], "REJECTED",
                                      mt5_retcode)
        self.log.append(make_event(
            "dispatch.rejected", connection_id=cid, broker=broker,
            order_request_id=candidate["candidate_id"],
            symbol=candidate["symbol"], execution_state="REJECTED",
            reason_code=mt5_retcode,
            payload={"mt5_return_code": mt5_retcode,
                     "native_reason": receipt_ref.get("reason", ""),
                     "broker_deal_id": receipt_ref.get("deal", "")}))
        return {"candidate_id": candidate["candidate_id"],
                "outcome": "REJECTED",
                "reason": mt5_retcode,
                "mt5_return_code": mt5_retcode}

    def _reserved_candidate_ids(self, connection_id: str) -> List[str]:
        from sqlalchemy import select
        from ..db import CandidateRow
        stmt = select(CandidateRow.candidate_id).where(
            CandidateRow.connection_id == connection_id,
            CandidateRow.execution_state.in_(("RESERVED", "SEND")))
        return list(db.engine_db.session.scalars(stmt))

    def _load_candidate(self, candidate_id: str) -> Optional[Dict]:
        return db.engine_db.get_candidate(candidate_id)
