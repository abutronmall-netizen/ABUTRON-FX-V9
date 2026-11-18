"""Run the gateway: ``python -m abutron.gateway``."""
from __future__ import annotations

import logging

import uvicorn

from .app import create_app
from .settings import GatewaySettings


def main() -> None:
    settings = GatewaySettings.from_env()
    logging.basicConfig(
        level=logging.INFO,
        format='{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
    app = create_app(settings)
    if settings.demo_enabled:
        print(f"[gateway] demo API key: {settings.demo_api_key}")
    if settings.is_insecure():
        print("[gateway] WARNING: default dev pepper/bridge secret in use. "
              "Set ABUTRON_GATEWAY_PEPPER and ABUTRON_GATEWAY_BRIDGE_SECRET.")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
