"""Transport factory.

The account row decides the transport; no API request can pick one
arbitrarily. Bridge state is rehydrated from the persisted snapshot so a
restarted gateway immediately has last-known positions to serve.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from ..errors import GatewayError
from ..settings import GatewaySettings
from ..store import GatewayStore
from .base import Mt5Transport
from .bridge import BridgeTransport
from .engine import EngineFeedTransport
from .simulated import SimulatedTransport


def build_transport(account: Dict[str, Any], store: GatewayStore,
                    settings: GatewaySettings,
                    sink: Optional[Callable[[Dict[str, Any]], None]] = None,
                    engine_store: Any = None) -> Mt5Transport:
    mode = account.get("market_data_mode", "simulated")
    if mode == "simulated":
        if not settings.allow_simulated_accounts:
            raise GatewayError(403, "SIMULATED_DISABLED",
                              "simulated accounts are disabled on this gateway")
        transport = SimulatedTransport(account, settings, sink=sink)
        # Rehydrate a demo portfolio across restarts.
        import json
        try:
            state = json.loads(account.get("state_json") or "{}")
        except (ValueError, TypeError):
            state = {}
        if state:
            transport.load_state(state)
        return transport
    if mode == "bridge":
        transport = BridgeTransport(account, store, settings, sink=sink)
        return transport
    if mode == "engine":
        # Read-only mirror: the engine remains the only writer.
        return EngineFeedTransport(account, engine_store=engine_store)
    raise GatewayError(400, "INVALID_MARKET_DATA_MODE",
                       f"unknown market_data_mode: {mode}")
