"""Single-writer gateway (blueprint: exactly one writer per account).

Per-account single-writer lock guarantees no duplicate native sender.
The gateway talks to the native MT5 EA (ApexNativeWriter) through the
terminal's MQL5/Files handoff protocol and verifies the compiled EA SHA256
pin before any order request. A disarmed or mismatched writer never sends.
"""
from __future__ import annotations

import fcntl
import json
import re
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..config import utc_now
from ..crypto import hmac_sign, make_nonce, verify_file_pin
from ..events import EventLog, make_event
from .state import state_machine


def _canonical_order(nonce: str, candidate: Dict[str, Any],
                     connection_id: str, expires_at: str = "") -> str:
    """Build the canonical order message that is HMAC-signed.

    Covers ALL order parameters so any tampering with any field is
    detected by the MQL5 verifier. The format must match exactly on
    both sides (Python and MQL5):
        nonce|candidate_id|connection_id|symbol|direction|volume|
        stop_loss|take_profit|entry_type|trigger_price|trail_k|trail_stop
    """
    trigger = float(candidate.get('trigger_price',
                                   candidate.get('price', 0.0)))
    return (
        f"{nonce}|{candidate['candidate_id']}|{connection_id}|"
        f"{candidate['symbol']}|{candidate['direction']}|"
        f"{float(candidate['volume']):.6f}|"
        f"{float(candidate.get('stop_loss', 0.0)):.6f}|"
        f"{float(candidate.get('take_profit', 0.0)):.6f}|"
        f"{candidate.get('entry_type', 'CONFIRM')}|"
        f"{trigger:.6f}|"
        f"{float(candidate.get('trail_k', 0.0)):.6f}|"
        f"{float(candidate.get('trail_stop', 0.0)):.6f}|{expires_at}"
    )


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
        self._delivery_paths: Dict[str, str] = {}
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
            broker: str, mql5_files_dir: str = "") -> WriterStatus:
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
        # Provision the HMAC secret to the EA via MQL5/Files so the
        # native writer can verify request signatures. Provisioning
        # failure is logged and downgrades the arm — the EA will reject
        # all orders without a valid secret, so arming is incomplete.
        secret_ok = status.armed and self.provision_secret(
            connection_id, mql5_files_dir, activation_revision)
        if status.armed and secret_ok:
            self._delivery_paths[connection_id] = os.path.realpath(mql5_files_dir)
        else:
            self._delivery_paths.pop(connection_id, None)
        if status.armed and not secret_ok:
            status.armed = False
            status.revision_ok = False
            self.log.append(make_event(
                "writer.arm", connection_id=connection_id, broker=broker,
                ea_sha256=actual, activation_revision=activation_revision,
                execution_state="DISARMED",
                reason_code="SECRET_PROVISION_FAILED"))
        else:
            self.log.append(make_event(
                "writer.arm", connection_id=connection_id, broker=broker,
                ea_sha256=actual, activation_revision=activation_revision,
                execution_state="ARMED" if status.armed else "DISARMED",
                reason_code="OK" if status.armed else "BINARY_SOURCE_MISMATCH"))
        return status

    def provision_secret(self, connection_id: str,
                         mql5_files_dir: str,
                         activation_revision: str = "") -> bool:
        """Provision the exact shared secret and activation revision for MT5.

        Never use a known fallback secret. Files are replaced atomically with
        owner-only permissions; the caller must provide the *trusted* account
        terminal's MQL5/Files path. Failed provisioning must not arm trading.
        """
        secret = os.getenv("ABUTRON_ACTIVATION_SECRET", "")
        if not mql5_files_dir or not secret:
            self.log.append(make_event(
                "writer.secret_provision_failed", connection_id=connection_id,
                reason_code="SECRET_NOT_CONFIGURED" if not secret else
                            "MQL5_FILES_DIRECTORY_MISSING"))
            return False
        try:
            if not os.path.isdir(mql5_files_dir) or os.path.islink(mql5_files_dir):
                raise OSError("terminal MQL5/Files directory is not trusted")
            def install_secret_file(name: str, value: str) -> None:
                fd, tmp = tempfile.mkstemp(prefix=".abutron-", dir=mql5_files_dir)
                try:
                    os.fchmod(fd, 0o600)
                    # Preserve the private terminal directory and place the
                    # secret under the broker worker UID, not the portal UID.
                    owner = os.stat(mql5_files_dir, follow_symlinks=False)
                    os.fchown(fd, owner.st_uid, owner.st_gid)
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(value + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp, os.path.join(mql5_files_dir, name))
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
            install_secret_file("ABUTRON_ACTIVATION_SECRET.txt", secret)
            if activation_revision:
                install_secret_file("ABUTRON_ACTIVATION_REVISION.txt",
                                    activation_revision)
            self.log.append(make_event(
                "writer.secret_provisioned", connection_id=connection_id,
                execution_state="ARMED", reason_code="SECRET_PROVISIONED"))
            return True
        except OSError as exc:
            self.log.append(make_event(
                "writer.secret_provision_failed", connection_id=connection_id,
                reason_code="SECRET_PROVISION_FAILED",
                payload={"error_type": type(exc).__name__}))
            return False

    def disarm(self, connection_id: str) -> None:
        if connection_id in self._armed:
            self._armed[connection_id].armed = False
            self._armed[connection_id].lock_held = False
            self._delivery_paths.pop(connection_id, None)
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
        if not status.armed or not status.revision_ok:
            self.log.append(make_event(
                "writer.send_disarmed", connection_id=connection_id,
                broker=broker, order_request_id=candidate.get("candidate_id"),
                execution_state="REJECTED",
                reason_code="SINGLE_WRITER_NOT_ARMED"))
            return {"outcome": "REJECTED", "reason": "SINGLE_WRITER_NOT_ARMED"}
        secret = os.getenv("ABUTRON_ACTIVATION_SECRET", "")
        if not secret:
            return {"outcome": "REJECTED", "reason": "ACTIVATION_SECRET_MISSING"}

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
            expires_at = str(int(time.time()) + 30)
            request = {
                "request_id": nonce,
                "expires_at": expires_at,
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
                # Sign the COMPLETE canonical order payload so any tampering
                # with symbol, direction, volume, SL, or TP is detected.
                # Canonical: nonce|candidate_id|connection_id|symbol|
                #           direction|volume|stop_loss|take_profit
                "signed": hmac_sign(
                    secret,
                    _canonical_order(nonce, candidate, connection_id,
                                     expires_at=expires_at)),
                "created_at": utc_now(),
            }
            outcome, receipt_ref = self._deliver_to_ea(request, request_timeout)
            return {"outcome": outcome, "receipt_ref": receipt_ref,
                    "request_id": nonce}
        finally:
            lock.release()

    def _deliver_to_ea(self, request: Dict[str, Any],
                       timeout: float) -> tuple:
        """Account-scoped atomic MQL5/Files handoff, never a socket ACK.

        The native EA reads ABUTRON_REQ_PENDING.json and writes an immutable
        ABUTRON_NATIVE_RECEIPT_<request_id>.json. An absent or contradictory
        receipt is UNKNOWN; a broker ACK requires a positive broker ticket,
        deal, price, volume and exact account/request identity.
        """
        cid = str(request.get("connection_id", ""))
        reqid = str(request.get("request_id", ""))
        root = self._delivery_paths.get(cid, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,110}", reqid):
            return "REJECTED", {"reason": "BAD_REQUEST_ID"}
        if not root or not os.path.isdir(root) or os.path.islink(root):
            return "REJECTED", {"reason": "ACCOUNT_NATIVE_FILES_NOT_ARMED"}
        pending = os.path.join(root, "ABUTRON_REQ_PENDING.json")
        receipt_path = os.path.join(
            root, "ABUTRON_NATIVE_RECEIPT_" + reqid + ".json")
        lockfile = os.path.join(root, ".abutron_v9_native_writer.lock")
        try:
            lockfd = os.open(lockfile, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                             0o600)
            with os.fdopen(lockfd, "a+") as exclusive:
                try:
                    fcntl.flock(exclusive.fileno(),
                                fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return "REJECTED", {"reason": "ANOTHER_NATIVE_WRITER_BUSY"}
                if os.path.lexists(pending) or os.path.lexists(receipt_path):
                    return "REJECTED", {"reason": "UNRECONCILED_NATIVE_HANDOFF"}
                data = json.dumps(request, allow_nan=False,
                                  sort_keys=True, separators=(",", ":")) + "\n"
                fd, temp = tempfile.mkstemp(
                    prefix=".abutron-pending-", dir=root)
                try:
                    os.fchmod(fd, 0o600)
                    owner = os.stat(root, follow_symlinks=False)
                    os.fchown(fd, owner.st_uid, owner.st_gid)
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    # Hard-link is exclusive: unlike os.replace it cannot
                    # silently overwrite a pending request from another writer.
                    try:
                        os.link(temp, pending, follow_symlinks=False)
                    except FileExistsError:
                        return "REJECTED", {"reason": "NATIVE_PENDING_COLLISION"}
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
                deadline = time.monotonic() + min(max(float(timeout), 0.1),
                                                 30.0)
                while time.monotonic() < deadline:
                    if os.path.lexists(receipt_path):
                        if os.path.islink(receipt_path):
                            return "UNKNOWN", {"reason": "RECEIPT_SYMLINK"}
                        try:
                            if os.path.getsize(receipt_path) > 65536:
                                raise ValueError("OVERSIZED_NATIVE_RECEIPT")
                            with open(receipt_path, encoding="utf-8-sig") as f:
                                receipt = json.load(f)
                        except (OSError, ValueError, UnicodeError):
                            return "UNKNOWN", {"reason": "RECEIPT_UNREADABLE"}
                        if (receipt.get("request_id") != reqid or
                                str(receipt.get("login")) !=
                                str(request.get("mt5_login"))):
                            return "UNKNOWN", {"reason": "RECEIPT_ACCOUNT_MISMATCH"}
                        if receipt.get("state") == "REJECTED":
                            return "REJECTED", receipt
                        if receipt.get("state") == "ACK":
                            try:
                                ticket = int(receipt.get("ticket", 0))
                                deal = int(receipt.get("deal", 0))
                                price = float(receipt.get("price", 0))
                                volume = float(receipt.get("volume", 0))
                                limit = float(request["volume"])
                            except (TypeError, ValueError, KeyError):
                                return "UNKNOWN", {"reason": "ACK_FIELDS_INVALID"}
                            if (ticket > 0 and deal > 0 and price > 0 and
                                    volume > 0 and volume <= limit + 1e-8):
                                return "ACK", receipt
                        return "UNKNOWN", receipt
                    time.sleep(0.1)
                return "UNKNOWN", {
                    "reason": "BROKER_RECEIPT_TIMEOUT",
                }
        except (OSError, TypeError, ValueError):
            return "UNKNOWN", {"reason": "NATIVE_FILES_HANDOFF_ERROR"}


writer_gateway = SingleWriterGateway(None)  # event log injected on startup
