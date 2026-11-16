"""Operations health + fault-code registry (blueprint cheat-sheet 05).

Distinct states for CONNECTED / PUBLISHING / EFFECTIVE ON / BROKER
CONFIRMED. Dashboard badges derive from reconciled native facts, not
"service active" alone. Fault codes map to likely domains + first
read-only check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..config import HEARTBEAT_STALE_SECONDS, utc_now
from ..db import engine_db
from ..events import EventLog, make_event


FAULT_CODES: Dict[str, Dict[str, str]] = {
    "CRM_EFFECTIVE_ON_OWNER_AND_ACK_UNVERIFIED": {
        "domain": "owner-wide risk + activation revision/ACK",
        "check": "supervisor; CRM desired vs effective; owner evidence join"},
    "FP2582_NATIVE_ORDER_DISPATCHER_MISSING": {
        "domain": "missing account-specific native send path",
        "check": "dispatcher unit, EA handshake, allowed binary pin"},
    "FP_SINGLE_WRITER_DISARMED": {
        "domain": "order authority absent or stale",
        "check": "EA readiness marker, active chart, connection ID, writer lock"},
    "BINARY_SOURCE_MISMATCH": {
        "domain": "compiled EA vs expected source pin drift",
        "check": "compare SHA256 and compiled provenance"},
    "BROKER_CLOCK_OFFSET_UNVERIFIED": {
        "domain": "FP feed timestamp trust failure",
        "check": "broker tick/terminal server time + closed-candle conversion"},
    "CANDLE_ALREADY_CONSUMED": {
        "domain": "anti-duplicate state (not a failure)",
        "check": "last consumed bar key, symbol lock, candidate age"},
    "SEND_UNKNOWN": {
        "domain": "broker submit outcome ambiguous",
        "check": "native terminal history, tickets/deals, receipts"},
    "OPEN_UNPROTECTED": {
        "domain": "stops absent or USD risk > budget",
        "check": "broker SL/TP + equity/point value/lot/stop-risk"},
    "PROCESS_EXITED": {
        "domain": "MT5 worker exited / connection lost",
        "check": "worker service, account-bound health, terminal logs"},
}


@dataclass
class HealthSnapshot:
    connection_id: str
    worker_status: str
    publisher_status: str
    desired_on: bool
    effective_on: bool
    broker_confirmed: bool
    clock_verified: bool
    disk_ok: bool
    last_heartbeat: str
    faults: List[str] = field(default_factory=list)

    @property
    def overall(self) -> str:
        if self.faults:
            return "DEGRADED"
        if self.broker_confirmed:
            return "BROKER_CONFIRMED"
        if self.effective_on:
            return "EFFECTIVE_ON"
        if self.publisher_status == "PUBLISHING":
            return "PUBLISHING"
        if self.worker_status == "CONNECTED":
            return "CONNECTED"
        return "UNKNOWN"

    def to_dict(self) -> Dict[str, Any]:
        return {"connection_id": self.connection_id,
                "worker_status": self.worker_status,
                "publisher_status": self.publisher_status,
                "desired_on": self.desired_on, "effective_on": self.effective_on,
                "broker_confirmed": self.broker_confirmed,
                "clock_verified": self.clock_verified,
                "disk_ok": self.disk_ok, "last_heartbeat": self.last_heartbeat,
                "faults": self.faults, "overall": self.overall}


class HealthMonitor:
    def __init__(self, event_log: EventLog):
        self.log = event_log

    def snapshot_account(self, connection_id: str, *,
                         worker_status: str = "CONNECTED",
                         publisher_status: str = "PUBLISHING",
                         clock_verified: bool = True,
                         disk_ok: bool = True) -> HealthSnapshot:
        acct = engine_db.get_account(connection_id)
        if acct is None:
            snap = HealthSnapshot(connection_id, "UNKNOWN", "UNKNOWN",
                                  False, False, False, False, False, "")
            snap.faults = ["ACCOUNT_NOT_FOUND"]
            return snap

        receipts = engine_db.list_receipts(connection_id, limit=20)
        broker_confirmed = any(r["execution_state"] in ("ACK", "OPEN", "PROTECTED")
                               and r["reconciled_state"] in ("MATCHED", "SETTLED")
                               for r in receipts)
        desired = bool(acct["desired_on"])
        effective = bool(acct["effective_on"])

        faults: List[str] = []
        if not clock_verified:
            faults.append("BROKER_CLOCK_OFFSET_UNVERIFIED")
        if not disk_ok:
            faults.append("DISK_CAPACITY_WARNING")
        # Effective ON but no recent native receipt -> not broker confirmed
        if effective and not broker_confirmed:
            faults.append("EFFECTIVE_ON_NO_BROKER_CONFIRMED")

        snap = HealthSnapshot(connection_id, worker_status, publisher_status,
                              desired, effective, broker_confirmed,
                              clock_verified, disk_ok, utc_now(), faults)
        self.log.append(make_event(
            "health.snapshot", connection_id=connection_id,
            broker=acct["broker"], execution_state=snap.overall,
            reason_code=";".join(faults) or "OK",
            payload=snap.to_dict()))
        return snap

    def decode_fault(self, code: str) -> Dict[str, str]:
        entry = FAULT_CODES.get(code)
        if entry:
            return {"code": code, **entry}
        return {"code": code, "domain": "UNKNOWN", "check": "inspect journal"}

    def all_health(self, **kw) -> List[Dict]:
        out = []
        for acct in engine_db.list_accounts():
            out.append(self.snapshot_account(acct["connection_id"], **kw)
                       .to_dict())
        return out
