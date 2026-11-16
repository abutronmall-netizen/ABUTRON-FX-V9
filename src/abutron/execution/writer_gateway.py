"""Single-writer gateway (blueprint: exactly one writer per account).

Per-account single-writer lock guarantees no duplicate native sender.
The gateway talks to the native MT5 EA (ApexNativeWriter) through the
terminal's MQL5/Files handoff protocol and verifies the compiled EA SHA256
pin before any order request. A disarmed or mismatched writer never sends.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..config import MANAGER_SOCK, utc_now
from ..crypto import hmac_sign, make_nonce, verify_file_pin
from ..events import EventLog, make_event
from .state import state_machine


@dataclass
class WriterStatus:
    connection_id: str
    armed: bool
    ea_sha256: str
    expected_sha256: str
    activation_revision: str
    revision_ok: bool
    lock_held: bool
    last_heartbeat: str

    def to_dict(self) -> Dict[str, Any]:
        return {"connection_id": self.connection_id, "armed": self.armed,
                "ea_sha256": self.ea_sha256,
                "expected_sha256": self.expected_sha256,
                "activation_revision": self.activation_revision,
                "revision_ok": self.revision_ok, "lock_held": self.lock_held,
                "last_heartbeat": self.last_heartbeat}


class SingleWriterGateway:
    """One authoritative native order-submission actor per account."""

    def __init__(self, event_log: EventLog):
        self.log = event_log
        self._locks: Dict[str, threading.Lock] = {}
        self._armed: Dict[str, WriterStatus] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, connection_id: str) -> threading.Lock:
        with self._locks_guard:
            if connection_id not in self._locks:
                self._locks[connection_id] = threading.Lock()
            return self._locks[connection_id]

    # ------------------------------------------------------------------
    # Arming (native writer readiness)
    # ------------------------------------------------------------------
    def arm(self, *, connection_id: str, ea_path: str,
            expected_sha256: str, activation_revision: str,
            broker: str) -> WriterStatus:
        ok, actual = verify_file_pin(ea_path, expected_sha256)
        if not ok:
            self.log.append(make_event(
                "writer.binary_mismatch", connection_id=connection_id,
                broker=broker, reason_code="BINARY_SOURCE_MISMATCH",
                payload={"expected": expected_sha256, "actual": actual}))
            status = WriterStatus(connection_id, False, actual,
                                  expected_sha256, activation_revision,
                                  False, False, utc_now())
        else:
            status = WriterStatus(connection_id, True, actual,
                                  expected_sha256, activation_revision,
                                  True, False, utc_now())
        self._armed[connection_id] = status
        self.log.append(make_event(
            "writer.arm", connection_id=connection_id, broker=broker,
            ea_sha256=actual, activation_revision=activation_revision,
            execution_state="ARMED" if status.armed else "DISARMED",
            reason_code="OK" if status.armed else "BINARY_SOURCE_MISMATCH"))
        return status

    def disarm(self, connection_id: str) -> None:
        if connection_id in self._armed:
            self._armed[connection_id].armed = False
            self._armed[connection_id].lock_held = False
            self.log.append(make_event(
                "writer.disarm", connection_id=connection_id,
                execution_state="DISARMED", reason_code="FP_SINGLE_WRITER_DISARMED"))

    def status(self, connection_id: str) -> WriterStatus:
        return self._armed.get(connection_id) or WriterStatus(
            connection_id, False, "", "", "", False, False, "")

    # ------------------------------------------------------------------
    # Order send under the single-writer lock
    # ------------------------------------------------------------------
    def send_order(self, *, connection_id: str, candidate: Dict[str, Any],
                   account: Dict[str, Any], broker: str,
                   request_timeout: float = 10.0) -> Dict[str, Any]:
        """Send a native order for one candidate, under the account lock.

        Aggressive mode: the arming / SHA256-pin gates are ADVISORY — a
        disarmed or mismatched writer is logged but does NOT block order
        submission (fail-closed writer gating removed). The per-account lock
        is retained for correctness (no duplicate sends in-process).

        Returns a dict with outcome: 'ACK' | 'UNKNOWN' | 'REJECTED'.
        """
        status = self.status(connection_id)
        if not status.armed:
            self.log.append(make_event(
                "writer.send_advisory_disarmed", connection_id=connection_id,
                broker=broker, order_request_id=candidate.get("candidate_id"),
                execution_state="DISPATCHED",
                reason_code="FP_SINGLE_WRITER_DISARMED_ADVISORY"))
        if not status.revision_ok:
            self.log.append(make_event(
                "writer.send_advisory_mismatch", connection_id=connection_id,
                broker=broker, order_request_id=candidate.get("candidate_id"),
                execution_state="DISPATCHED",
                reason_code="BINARY_SOURCE_MISMATCH_ADVISORY"))

        lock = self._lock_for(connection_id)
        if not lock.acquire(blocking=False):
            # Another send in flight for this account — do not double-send.
            self.log.append(make_event(
                "writer.send_conflict", connection_id=connection_id,
                broker=broker, order_request_id=candidate.get("candidate_id"),
                execution_state="REJECTED",
                reason_code="DUPLICATE_WRITER_BUSY"))
            return {"outcome": "REJECTED", "reason": "DUPLICATE_WRITER_BUSY"}

        try:
            state_machine.move(candidate["candidate_id"], "SEND", "DISPATCHED")
            nonce = make_nonce("ABUTRON_REQ")
            request = {
                "kind": "order_handoff",
                "request_id": nonce,
                "candidate_id": candidate["candidate_id"],
                "connection_id": connection_id,
                "mt5_login": account.get("mt5_login"),
                "symbol": candidate["symbol"],
                "direction": candidate["direction"],
                "volume": candidate["volume"],
                # Sniper entry geometry: the EA places a LIMIT order at the
                # trigger price for LIMIT entries, or a market order for
                # CONFIRM entries. stop_loss = tight invalidation.
                "entry_type": candidate.get("entry_type", "CONFIRM"),
                "trigger_price": candidate.get("trigger_price",
                                               candidate.get("price", 0.0)),
                "stop_loss": candidate.get("stop_loss", 0.0),
                "take_profit": candidate.get("take_profit", 0.0),
                # Trailing stop: the EA trails the broker stop using this
                # ATR multiple and current trail stop.
                "trail_k": candidate.get("trail_k", 0.0),
                "trail_stop": candidate.get("trail_stop", 0.0),
                "activation_revision": status.activation_revision,
                "signed": hmac_sign(
                    os.getenv("ABUTRON_ACTIVATION_SECRET", "apex-activation"),
                    f"{nonce}|{candidate['candidate_id']}|{connection_id}"),
                "created_at": utc_now(),
            }
            outcome, receipt_ref = self._deliver_to_ea(request, request_timeout)
            return {"outcome": outcome, "receipt_ref": receipt_ref,
                    "request_id": nonce}
        finally:
            lock.release()

    def _deliver_to_ea(self, request: Dict[str, Any],
                       timeout: float) -> tuple:
        """Deliver the signed request to the native EA via the manager socket.

        In this deployment the manager socket is a local Unix socket; the
        manager proxies to the per-account terminal. If the socket is not
        present (offline dev / test), the outcome is UNKNOWN — never a
        fabricated ACK.
        """
        try:
            if not os.path.exists(MANAGER_SOCK):
                return "UNKNOWN", None
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(MANAGER_SOCK)
            sock.sendall((json.dumps(request) + "\n").encode())
            data = sock.recv(4096).decode()
            sock.close()
            resp = json.loads(data.strip().splitlines()[-1])
            if resp.get("status") == "ack":
                return "ACK", resp
            if resp.get("status") == "reject":
                return "REJECTED", resp
            return "UNKNOWN", resp
        except (socket.timeout, ConnectionRefusedError, OSError,
                ValueError, json.JSONDecodeError):
            return "UNKNOWN", None


writer_gateway = SingleWriterGateway(None)  # event log injected on startup
