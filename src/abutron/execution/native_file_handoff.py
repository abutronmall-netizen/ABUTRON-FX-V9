"""Native MQL5/Files order handoff transport.

Delivers signed order requests to the MT5 terminal's MQL5/Files
sandbox by writing ``ABUTRON_REQ_PENDING.json`` and reading the
receipt file the EA writes back. This is the fallback transport for
deployments where the terminal runs in an isolated Wine environment
without a local Unix socket bridge.

The protocol matches ``ProcessOrderRequests()`` in
``mql5/ApexNativeWriter.mq5``:
1. Write the signed JSON request to ``ABUTRON_REQ_PENDING.json``
2. Wait for the EA to process it and write a receipt file
3. Read the receipt (ACK / REJECTED / UNKNOWN)
4. The EA deletes the pending file after processing

Fail-closed: if the receipt cannot be read, the outcome is UNKNOWN —
never a fabricated ACK.
"""
from __future__ import annotations

import glob
import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


PENDING_FILE = "ABUTRON_REQ_PENDING.json"
RECEIPT_GLOB = "ABUTRON_REQ_RECEIPT_*.json"


class NativeFileHandoffError(ValueError):
    pass


class NativeFileHandoff:
    """File-based order transport to the MT5 terminal's MQL5/Files."""

    def __init__(self, files_dir: str, *, connection_id: str,
                 mt5_login: int):
        self.files_dir = Path(files_dir)
        self.connection_id = connection_id
        self.mt5_login = mt5_login

    def _validate_dir(self) -> None:
        st = self.files_dir.lstat()
        if (self.files_dir.name != "Files"
                or self.files_dir.parent.name != "MQL5"
                or not stat.S_ISDIR(st.st_mode)
                or st.st_mode & 0o022):
            raise NativeFileHandoffError("NATIVE_FILES_DIR_UNTRUSTED")

    def _write_pending(self, request: Dict[str, Any]) -> str:
        """Atomically write the pending request file."""
        self._validate_dir()
        path = self.files_dir / PENDING_FILE
        if path.is_symlink():
            raise NativeFileHandoffError("PENDING_FILE_SYMLINK")
        fd, tmp = os.mkstemp(prefix=".abutron-req-", dir=str(self.files_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(request, f, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(path))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return str(path)

    def _wait_for_receipt(self, request_id: str,
                          timeout: float) -> Optional[Dict]:
        """Wait for the EA to write a receipt file for this request."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            # The EA writes ABUTRON_REQ_RECEIPT_<request_id>.json
            pattern = str(self.files_dir /
                          f"ABUTRON_REQ_RECEIPT_{request_id}*.json")
            matches = sorted(glob.glob(pattern),
                             key=os.path.getmtime, reverse=True)
            if matches:
                path = matches[0]
                if os.path.islink(path):
                    return None
                try:
                    st = os.stat(path)
                    if st.st_size < 10 or st.st_size > 4096:
                        return None
                    with open(path, encoding="utf-8") as f:
                        return json.load(f)
                except (ValueError, OSError, UnicodeDecodeError):
                    return None
            time.sleep(0.1)
        return None

    def send(self, request: Dict[str, Any],
             timeout: float = 10.0) -> Tuple[str, Optional[Dict]]:
        """Write the request and wait for the native receipt.

        Returns (outcome, receipt_ref) where outcome is
        'ACK' | 'REJECTED' | 'UNKNOWN'.
        """
        request_id = request.get("request_id", "")
        if not request_id:
            raise NativeFileHandoffError("NO_REQUEST_ID")

        # Clean up any stale receipt files from previous attempts.
        for old in glob.glob(str(self.files_dir /
                                 f"ABUTRON_REQ_RECEIPT_{request_id}*.json")):
            try:
                os.unlink(old)
            except OSError:
                pass

        # Write the pending request.
        self._write_pending(request)

        # Wait for the receipt.
        receipt = self._wait_for_receipt(request_id, timeout)

        # Clean up the pending file if the EA did not (stale request).
        pending_path = self.files_dir / PENDING_FILE
        if pending_path.exists() and not pending_path.is_symlink():
            try:
                os.unlink(str(pending_path))
            except OSError:
                pass

        if receipt is None:
            return "UNKNOWN", None

        state = str(receipt.get("state", "")).upper()
        if state == "ACK":
            return "ACK", receipt
        if state == "REJECTED":
            return "REJECTED", receipt
        return "UNKNOWN", receipt
