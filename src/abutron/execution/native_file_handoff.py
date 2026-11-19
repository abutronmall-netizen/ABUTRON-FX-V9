"""Account-bound MT5 MQL5/Files order handoff.

This is a real native transport, not a mock broker ACK. Every request is
published exactly once to the native EA's pending file; unknown outcomes
must be reconciled rather than retried.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Tuple


class NativeFileHandoff:
    def __init__(self, files_dir: str, *, connection_id: str, mt5_login: int):
        self.directory = Path(files_dir)
        self.connection_id = connection_id
        self.login = int(mt5_login)

    def _check_directory(self) -> None:
        p = self.directory
        if p.name != "Files" or p.parent.name != "MQL5" or p.is_symlink():
            raise ValueError("NATIVE_MQL5_FILES_DIRECTORY_INVALID")
        st = p.stat()
        if not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o022:
            raise ValueError("NATIVE_MQL5_FILES_DIRECTORY_UNTRUSTED")

    def _validate_request(self, req: Dict[str, Any]) -> None:
        if (req.get("connection_id") != self.connection_id
                or str(req.get("mt5_login", "")) != str(self.login)
                or not req.get("candidate_id") or not req.get("request_id")
                or not req.get("activation_revision")
                or not req.get("signed")):
            raise ValueError("NATIVE_REQUEST_IDENTITY_OR_SIGNATURE_MISSING")
        if req.get("direction") not in ("BUY", "SELL"):
            raise ValueError("NATIVE_REQUEST_DIRECTION_INVALID")
        for key in ("volume", "stop_loss", "take_profit", "trigger_price"):
            try:
                import math
                value = float(req[key])
                if not math.isfinite(value) or value <= 0:
                    raise ValueError()
            except (KeyError, ValueError, TypeError, OverflowError):
                raise ValueError("NATIVE_REQUEST_GEOMETRY_INVALID") from None

    def _receipt(self, request_id: str, start_ns: int) -> Tuple[str, Any]:
        # Receipts are written by native MQL5 and always name the request_id.
        # A file's creation time is not authoritative; check file content.
        for path in sorted(self.directory.glob("ABUTRON_NATIVE_RECEIPTS_V2_*.txt"),
                           key=lambda q: q.name, reverse=True):
            try:
                st = path.lstat()
                if (not stat.S_ISREG(st.st_mode) or st.st_size > 8192
                        or st.st_size < 8 or st.st_mtime_ns < start_ns):
                    continue
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    raw = os.read(fd, 8193)
                finally:
                    os.close(fd)
                if len(raw) > 8192:
                    continue
                receipt = json.loads(raw.decode("utf-8-sig"))
            except (OSError, UnicodeError, ValueError, TypeError):
                continue
            if receipt.get("request_id") != request_id:
                continue
            if str(receipt.get("login", "")) != str(self.login):
                return "UNKNOWN", {"reason": "NATIVE_RECEIPT_ACCOUNT_MISMATCH"}
            state = receipt.get("state")
            if state == "ACK":
                # A native EA ACK is not independent broker deal certification.
                if int(receipt.get("ticket", 0) or 0) <= 0 and not receipt.get("deal"):
                    return "UNKNOWN", {"reason": "NATIVE_ACK_HAS_NO_BROKER_TICKET"}
                return "ACK", receipt
            if state == "REJECTED":
                return "REJECTED", receipt
            return "UNKNOWN", receipt
        return "", None

    def send(self, request: Dict[str, Any], *, timeout: float = 10.0) -> Tuple[str, Any]:
        self._check_directory()
        self._validate_request(request)
        pending = self.directory / "ABUTRON_REQ_PENDING.json"
        lockfile = self.directory / ".abutron-native-writer.lock"
        fd = os.open(lockfile, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        tmp = None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if pending.exists() or pending.is_symlink():
                return "REJECTED", {"reason": "NATIVE_PENDING_REQUEST_BUSY"}
            start_ns = time.time_ns()
            temp_fd, tmp = tempfile.mkstemp(prefix=".abutron-request-", dir=self.directory)
            with os.fdopen(temp_fd, "wb") as f:
                os.fchmod(f.fileno(), 0o600)
                dst = self.directory.stat()
                os.fchown(f.fileno(), dst.st_uid, dst.st_gid)
                f.write((json.dumps(request, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, pending)
            tmp = None
            dirfd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
            # The native EA is the only party that consumes/deletes pending.
            # Never requeue a request whose ACK is unknown.
            until = time.monotonic() + max(0.0, timeout)
            while True:
                state, receipt = self._receipt(str(request["request_id"]), start_ns)
                if state:
                    return state, receipt
                if time.monotonic() >= until:
                    return "UNKNOWN", {"reason": "NATIVE_RECEIPT_TIMEOUT_NO_RETRY"}
                time.sleep(min(0.1, max(0.0, until - time.monotonic())))
        except BlockingIOError:
            return "REJECTED", {"reason": "NATIVE_SINGLE_WRITER_BUSY"}
        finally:
            if tmp is not None and os.path.exists(tmp):
                os.unlink(tmp)
            os.close(fd)
