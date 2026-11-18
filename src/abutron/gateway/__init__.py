"""ABUTRON MT5 Gateway — multi-tenant MetaTrader 5 API bridge.

A MetaApi-style control plane for MT5: tenants get API keys, connect MT5
accounts, then read market data / positions / history and place trades over
REST or a WebSocket stream.

The gateway is a *second product surface* on top of the existing ABUTRON
runtime. It never competes with the APEX autotrader for order authority:
each account has exactly one `control_mode` (`api` or `read_only`), and a
trade request on an automation-owned account is rejected with
AUTOMATION_WRITER_ACTIVE rather than silently double-writing an account.

Public entry points:
    from abutron.gateway import create_app, GatewaySettings
    uvicorn abutron.gateway.app:create_app --factory
"""
from .settings import GatewaySettings
from .app import create_app

__all__ = ["GatewaySettings", "create_app"]
__version__ = "0.1.0"
