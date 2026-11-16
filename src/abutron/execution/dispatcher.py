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
from .native_evidence import (assess_native_evidence,
                              REASON_BLOCKED_INCOMPLETE_EXPOSURE)


class NativeDispatcher:
    def __init__(self, event_log: EventLog,
                 gateway: SingleWriterGateway,
                 receipt_mgr: ReceiptManager):
        self.log = event_log
        self.gateway = gateway
        self.receipts = receipt_mgr

    def dispatch_account(self, connection_id: str) -> Dict[str, Any]:
        """Dispatch all RESERVED candidates for one account.

        Native-evidence gate: when any of the five evidence categories
        (broker session, EA attestation, terminal liveness, positions
        inventory, owner exposure join) is missing, nothing is sent —
        candidates stay RESERVED for a later tick and each is reported
        BLOCKED with reason BLOCKED_INCOMPLETE_EXPOSURE.
        """
        account = db.engine_db.get_account(connection_id)
        if account is None:
            return {"connection_id": connection_id, "sent": 0,
                    "error": "ACCOUNT_NOT_FOUND"}

        try:
            evidence = assess_native_evidence(
                connection_id, gateway=self.gateway, event_log=self.log)
        except Exception:
            evidence = None

        # Collect reserved (RESERVED state) candidates
        candidates = [self._load_candidate(cid)
                      for cid in self._reserved_candidate_ids(connection_id)]
        results = {"connection_id": connection_id, "sent": 0,
                   "ack": 0, "unknown": 0, "rejected": 0, "blocked": 0,
                   "evidence": (evidence.to_dict() if evidence is not None
                                else {"complete": False,
                                      "reason": "EVIDENCE_ASSESSMENT_FAILED"}),
                   "details": []}

        if evidence is None or not evidence.complete:
            missing = (evidence.missing if evidence is not None
                       else ["assessment_failed"])
            for cand in candidates:
                if cand is None:
                    continue
                if cand.get("execution_state") not in ("RESERVED", "SEND"):
                    continue
                results["blocked"] += 1
                results["details"].append({
                    "candidate_id": cand["candidate_id"],
                    "outcome": "BLOCKED",
                    "reason": REASON_BLOCKED_INCOMPLETE_EXPOSURE,
                    "missing": missing})
            self.log.append(make_event(
                "dispatch.blocked", connection_id=connection_id,
                broker=account["broker"], execution_state="BLOCKED",
                reason_code=REASON_BLOCKED_INCOMPLETE_EXPOSURE,
                payload={"missing": missing,
                         "blocked": results["blocked"]}))
            return results

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

    def _dispatch_one(self, account: Dict[str, Any],
                      candidate: Dict[str, Any]) -> Dict[str, Any]:
        cid = account["connection_id"]
        broker = account["broker"]
        sent = self.gateway.send_order(
            connection_id=cid, candidate=candidate, account=account,
            broker=broker)
        outcome = sent.get("outcome")

        if outcome == "ACK":
            self.receipts.record_native_receipt(
                candidate_id=candidate["candidate_id"],
                connection_id=cid, mt5_login=account["mt5_login"],
                symbol=candidate["symbol"], direction=candidate["direction"],
                native_ticket=sent["receipt_ref"].get("ticket", 0),
                broker_deal_id=sent["receipt_ref"].get("deal", ""),
                volume=candidate["volume"],
                fill_price=candidate.get("trigger_price",
                                         candidate.get("price", 0.0)),
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

        # REJECTED
        db.engine_db.set_candidate_state(candidate["candidate_id"], "REJECTED",
                                      sent.get("reason", "REJECTED"))
        self.log.append(make_event(
            "dispatch.rejected", connection_id=cid, broker=broker,
            order_request_id=candidate["candidate_id"],
            symbol=candidate["symbol"], execution_state="REJECTED",
            reason_code=sent.get("reason", "REJECTED")))
        return {"candidate_id": candidate["candidate_id"],
                "outcome": "REJECTED", "reason": sent.get("reason")}

    def _reserved_candidate_ids(self, connection_id: str) -> List[str]:
        from sqlalchemy import select
        from ..db import CandidateRow
        stmt = select(CandidateRow.candidate_id).where(
            CandidateRow.connection_id == connection_id,
            CandidateRow.execution_state.in_(("RESERVED", "SEND")))
        return list(db.engine_db.session.scalars(stmt))

    def _load_candidate(self, candidate_id: str) -> Optional[Dict]:
        return db.engine_db.get_candidate(candidate_id)
