"""Run the MT5 gateway inside the ABUTRON platform process.

Default for `python -m abutron.main`: the portal (19743) and the API
gateway (19744) are two listeners in one process, sharing one data root
and one deployment. The gateway still has its own database and its own
tenant keys, so the public API surface stays isolated from the operator
portal.

`ABUTRON_GATEWAY_ENABLED=false` disables it (or run the gateway as a
separate systemd unit with `abutron-gateway.service`).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

import uvicorn

from .app import create_app
from .settings import GatewaySettings


class EmbeddedGateway:
    """A uvicorn server for the gateway running on a background thread."""

    def __init__(self, settings: Optional[GatewaySettings] = None):
        self.settings = settings or GatewaySettings.from_env()
        self.app = create_app(self.settings)
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[BaseException] = None

    # ------------------------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://{self.settings.host}:{self.settings.port}"

    @property
    def running(self) -> bool:
        return bool(self._server and self._server.started and
                    self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------
    def start(self, timeout: float = 12.0) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return self.running
        config = uvicorn.Config(
            self.app,
            host=self.settings.host,
            port=self.settings.port,
            log_level=os.getenv("ABUTRON_GATEWAY_LOG_LEVEL", "warning"),
            log_config=None,          # keep the platform's logging setup
            access_log=False,
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._run, name="abutron-gateway", daemon=True)
        self._thread.start()
        return self.wait_ready(timeout)

    def _run(self) -> None:
        try:
            self._server.run()  # type: ignore[union-attr]
        except BaseException as exc:  # noqa: BLE001 - reported by caller
            self._error = exc

    def wait_ready(self, timeout: float = 12.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server is not None and self._server.started:
                return True
            if self._thread is not None and not self._thread.is_alive():
                return False
            time.sleep(0.05)
        return False

    def stop(self, timeout: float = 10.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def error(self) -> Optional[BaseException]:
        return self._error
