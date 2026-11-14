"""Single-writer gateway (blueprint: exactly one writer per account).

Per-account single-writer lock guarantees no duplicate native sender.
The gateway talks to the native MT5 EA (ApexNativeWriter) through the
terminal's MQL5/Files handoff protocol and verifies the compiled EA SHA256
pin before any order request. A disarmed or mismatched writer never sends.
"""
from __future__ import annotations

import json
import math
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
        """Attempt one DEMO order only with account, writer, and stop proof.

        This layer never treats an application response as a broker fill.
        """
        status = self.status(connection_id)
        reason = self._preflight(connection_id, candidate, account, broker, status)
        if reason:
            self.log.append(make_event(
                "writer.send_blocked", connection_id=connection_id,
                broker=broker, order_request_id=candidate.get("candidate_id"),
                execution_state="REJECTED", reason_code=reason))
            return {"outcome": "REJECTED", "reason": reason}

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
                    os.environ["ABUTRON_ACTIVATION_SECRET"],
                    f"{nonce}|{connection_id}"),
                "created_at": utc_now(),
            }
            outcome, receipt_ref = self._deliver_to_ea(request, request_timeout)
            if outcome == "ACK" and not self._native_ack_is_proven(
                    receipt_ref, request):
                outcome = "UNKNOWN"
                receipt_ref = None
            return {"outcome": outcome, "receipt_ref": receipt_ref,
                    "request_id": nonce}
        finally:
            lock.release()

    @staticmethod
    def _preflight(connection_id: str, candidate: Dict[str, Any],
                   account: Dict[str, Any], broker: str,
                   status: WriterStatus) -> str:
        if not (account.get("desired_on") and account.get("effective_on")):
            return "CLIENT_AUTOMATIC_SWITCH_OFF"
        if str(account.get("environment", "")).lower() != "demo":
            return "DEMO_ONLY_ACCOUNT_BOUNDARY"
        if str(account.get("broker", "")) != broker or (
                candidate.get("connection_id", connection_id) != connection_id):
            return "BROKER_OR_CONNECTION_MISMATCH"
        login = str(account.get("mt5_login", ""))
        if login.endswith("0178") or login.endswith("3492"):
            return "RETIRED_ACCOUNT_NOT_ALLOWED"
        if not status.armed or not status.revision_ok:
            return "EA_WRITER_NOT_ATTESTED"
        if not status.activation_revision:
            return "ACTIVATION_REVISION_MISSING"
        secret = os.getenv("ABUTRON_ACTIVATION_SECRET", "")
        if not secret or secret == "apex-activation":
            return "ACTIVATION_SECRET_UNSET"
        direction = candidate.get("direction")
        try:
            price = float(candidate.get("trigger_price",
                                        candidate.get("price", 0.0)))
            sl = float(candidate["stop_loss"])
            tp = float(candidate["take_profit"])
            volume = float(candidate["volume"])
        except (ValueError, TypeError, KeyError):
            return "BROKER_ORDER_GEOMETRY_MISSING"
        if not all(math.isfinite(x) and x > 0 for x in
                   (price, sl, tp, volume)):
            return "BROKER_ORDER_GEOMETRY_INVALID"
        if direction == "BUY" and not (sl < price < tp):
            return "INVALID_BUY_STOPS"
        if direction == "SELL" and not (tp < price < sl):
            return "INVALID_SELL_STOPS"
        if direction not in ("BUY", "SELL"):
            return "INVALID_DIRECTION"
        if candidate.get("entry_type", "CONFIRM") not in ("LIMIT", "CONFIRM"):
            return "UNKNOWN_ENTRY_TYPE"
        return ""

    @staticmethod
    def _native_ack_is_proven(response: Any, request: Dict[str, Any]) -> bool:
        """An app-generated ticket cannot prove a broker deal or protected SL/TP."""
        if not isinstance(response, dict) or response.get("status") != "ack":
            return False
        if (str(response.get("connection_id")) !=
                str(request["connection_id"]) or
                str(response.get("mt5_login")) !=
                str(request["mt5_login"])):
            return False
        if not response.get("broker_native_protected", False):
            return False
        if not str(response.get("deal", "")).strip():
            return False
        try:
            ticket = int(response["ticket"])
            fill = float(response["fill_price"])
            sl = float(response["stop_loss"])
            tp = float(response["take_profit"])
        except (TypeError, ValueError, KeyError):
            return False
        if ticket <= 0 or not all(math.isfinite(x) and x > 0 for x in
                                  (fill, sl, tp)):
            return False
        if request["direction"] == "BUY":
            return sl < fill < tp
        if request["direction"] == "SELL":
            return tp < fill < sl
        return False

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
