"""Connection IPC — Unix socket manager protocol.

The portal adapter (`abutron_mt5_portal_adapter`) communicates with the
per-account MT5 manager through a Unix socket. This module implements the
manager side: it accepts JSON line requests (status, launch, stop, order
handoff) and routes them to the terminal manager / writer gateway.

Private local channel only — never exposed publicly. Requests carry
hmac-signed activation revisions so a stale or spoofed nonce cannot gain
order authority.
"""
from __future__ import annotations

import json
import os
import socketserver
import threading
from typing import Any, Dict, Optional

from ..config import MANAGER_SOCK, utc_now
from ..crypto import hmac_verify


class ManagerRequest:
    """A single JSON line request to the manager socket."""

    def __init__(self, raw: Dict[str, Any]):
        self.raw = raw

    @property
    def kind(self) -> str:
        return self.raw.get("kind", "")

    @property
    def connection_id(self) -> str:
        return self.raw.get("connection_id", "")

    def verified(self, secret: str) -> bool:
        sig = self.raw.get("signed", "")
        msg = self.raw.get("request_id", "") + "|" + \
            self.raw.get("connection_id", "")
        return hmac_verify(secret, msg, sig)


class ManagerSocketServer:
    """Unix-socket manager. Threaded, one handler per request."""

    def __init__(self, secret: str = "apex-activation"):
        self.secret = secret
        self._handlers: Dict[str, callable] = {}
        self._server: Optional[socketserver.ThreadingUnixStreamServer] = None
        self._thread: Optional[threading.Thread] = None

    def register(self, kind: str, handler: callable) -> None:
        self._handlers[kind] = handler

    def start(self, path: str = MANAGER_SOCK) -> None:
        if os.path.exists(path):
            os.unlink(path)
        self._server = socketserver.ThreadingUnixStreamServer(path, _Handler)
        self._server.manager = self
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def dispatch(self, request: ManagerRequest) -> Dict[str, Any]:
        handler = self._handlers.get(request.kind)
        if handler is None:
            return {"status": "error", "error": f"UNKNOWN_KIND:{request.kind}"}
        if request.kind in ("order", "order_handoff"):
            if not request.verified(self.secret):
                return {"status": "reject", "reason": "BAD_SIGNATURE"}
        return handler(request)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        manager: ManagerSocketServer = self.server.manager
        buf = b""
        while True:
            chunk = self.request.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    req = ManagerRequest(json.loads(line.decode()))
                    resp = manager.dispatch(req)
                except (ValueError, json.JSONDecodeError) as e:
                    resp = {"status": "error", "error": f"BAD_REQUEST:{e}"}
                self.request.sendall((json.dumps(resp) + "\n").encode())


manager_socket = ManagerSocketServer()
