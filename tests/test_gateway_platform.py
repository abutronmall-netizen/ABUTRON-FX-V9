"""Platform-level tests: self-containment and embedded gateway.

These assert the two things the product must never regress:

1. There is **no MetaApi (or any third-party MT bridge) dependency** in the
   gateway's code path. Broker access is the terminal's own login.
2. The gateway runs inside the ABUTRON process, and its trading handler
   does not block the event loop — a bridge must be able to pull and
   answer a command while a trade request is still waiting.
"""
from __future__ import annotations

import os
import pathlib
import re
import socket
import sys
import threading
import time

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.gateway.admin import create_tenant
from abutron.gateway.auth import ApiKeyService
from abutron.gateway.embedded import EmbeddedGateway
from abutron.gateway.settings import GatewaySettings
from abutron.gateway.store import GatewayStore

ROOT = pathlib.Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def gateway_server(tmp_path):
    port = free_port()
    settings = GatewaySettings(
        db_path=str(tmp_path / "gateway.db"), host="127.0.0.1", port=port,
        demo_enabled=False, webhooks_enabled=False, webhook_worker=False,
        command_timeout_s=6.0, landing_page=False)
    server = EmbeddedGateway(settings)
    assert server.start(), f"gateway failed to start: {server.error}"
    base = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=base, timeout=15.0) as client:
        yield client, server, base
    server.stop()


# ---------------------------------------------------------------------------
# Self-containment
# ---------------------------------------------------------------------------
def test_no_metaapi_dependency():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "metaapi" not in requirements
    assert "metatrader5" not in requirements

    gateway_sources = list((ROOT / "src" / "abutron" / "gateway").rglob("*.py"))
    assert gateway_sources, "gateway package missing"
    import_pattern = re.compile(
        r"(?mi)^\s*(?:import|from)\s+(?:metaapi|metatrader5)\b")
    for path in gateway_sources:
        text = path.read_text(encoding="utf-8")
        assert not import_pattern.search(text), path


def test_runtime_starts_gateway_by_default():
    import abutron.config as config
    assert hasattr(config, "GATEWAY_ENABLED")
    assert hasattr(config, "GATEWAY_PORT")
    source = (ROOT / "src" / "abutron" / "main.py").read_text(encoding="utf-8")
    assert "EmbeddedGateway" in source
    assert "GATEWAY_ENABLED" in source


# ---------------------------------------------------------------------------
# Embedded process
# ---------------------------------------------------------------------------
def test_embedded_gateway_serves_health(gateway_server):
    client, server, _base = gateway_server
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["service"] == "abutron-mt5-gateway"
    assert server.running


def test_embedded_gateway_stops(gateway_server):
    _client, server, _base = gateway_server
    server.stop()
    assert not server.running
    assert server._thread is not None and not server._thread.is_alive()


# ---------------------------------------------------------------------------
# Concurrency: a bridge can answer while the trade handler waits
# ---------------------------------------------------------------------------
def _create_bridge_account(client: httpx.Client, api_key: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"}
    response = client.post("/v1/accounts", headers=headers, json={
        "login": 424242, "server": "Embedded-Demo", "broker": "local",
        "market_data_mode": "bridge", "control_mode": "api",
        "symbols": ["EURUSD"], "max_lot": 5.0})
    assert response.status_code == 201, response.text
    return response.json()


def test_bridge_answers_while_trade_request_waits(gateway_server):
    client, server, _base = gateway_server
    service = server.app.state.service
    tenant_id, api_key = service.create_tenant("embedded-test")
    headers = {"Authorization": f"Bearer {api_key}"}
    account = _create_bridge_account(client, api_key)
    token = account["bridge"]["token"]
    aid = account["account_id"]

    state = {
        "sequence": 1,
        "account": {"login": 424242, "currency": "USD", "leverage": 100,
                    "balance": 10000, "equity": 10000, "margin": 0,
                    "free_margin": 10000, "margin_level": 0,
                    "trade_allowed": True, "connected": True},
        "symbols": [{"symbol": "EURUSD", "digits": 5, "point": 0.00001,
                     "contract_size": 100000, "min_volume": 0.01,
                     "max_volume": 50, "volume_step": 0.01}],
        "prices": {"EURUSD": {"bid": 1.0850, "ask": 1.0852}},
        "positions": [], "orders": [], "deals": [], "history_orders": [],
    }
    pushed = client.post(account["bridge"]["state_url"],
                         headers={"X-Bridge-Token": token}, json=state)
    assert pushed.status_code == 200

    outcome: dict = {}

    def place_trade():
        response = client.post(
            f"/v1/accounts/{aid}/trade", headers=headers,
            json={"side": "buy", "symbol": "EURUSD", "volume": 0.1,
                  "stop_loss": 1.0700, "take_profit": 1.1000})
        outcome["status"] = response.status_code
        outcome["body"] = response.json()

    worker = threading.Thread(target=place_trade, daemon=True)
    worker.start()

    commands = []
    deadline = time.time() + 5
    while time.time() < deadline:
        response = client.get(account["bridge"]["commands_url"],
                              headers={"X-Bridge-Token": token},
                              params={"wait": 0})
        commands = response.json().get("commands", [])
        if commands:
            break
        time.sleep(0.05)
    assert commands, "bridge could not pull the command while the trade waited"

    command = commands[0]
    result_url = account["bridge"]["result_url_template"].replace(
        "{command_id}", command["command_id"])
    result = client.post(result_url, headers={"X-Bridge-Token": token}, json={
        "accepted": True, "retcode": "DONE", "order_id": "1",
        "position_id": "1", "deal_id": "2", "price": 1.0852,
        "volume": 0.1})
    assert result.status_code == 200

    worker.join(timeout=8)
    assert not worker.is_alive()
    assert outcome["status"] == 200, outcome.get("body")
    assert outcome["body"]["status"] == "EXECUTED"
    assert outcome["body"]["position_id"] == "1"


# ---------------------------------------------------------------------------
# Operator CLI (offline bootstrap, no admin HTTP secret)
# ---------------------------------------------------------------------------
def test_admin_cli_mints_a_working_key(tmp_path):
    settings = GatewaySettings(db_path=str(tmp_path / "admin.db"),
                               demo_enabled=False, webhooks_enabled=False)
    store = GatewayStore(settings.db_path)
    store.init()
    keys = ApiKeyService(store, settings)
    result = create_tenant(store, keys, "cli-tenant")
    assert result["api_key"].startswith("abk_")
    ctx, _row = keys.authenticate(result["api_key"])
    assert ctx.tenant_id == result["tenant_id"]
    assert ctx.has("trade")
    listed = store.list_tenants()
    assert listed[0]["tenant_id"] == result["tenant_id"]
    assert listed[0]["keys"][0]["prefix"] in result["api_key"]
    store.close()


# ---------------------------------------------------------------------------
# Read-only mirror of engine-managed accounts
# ---------------------------------------------------------------------------
from abutron.gateway.service import GatewayService                      # noqa: E402
from abutron.gateway.transports.base import TransportError            # noqa: E402
from abutron.gateway.transports.engine import EngineFeedTransport     # noqa: E402


def _iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class FakeEngineStore:
    """Minimal stand-in for abutron.db.EngineDB."""

    def __init__(self, account=None, receipts=None, ledger=None):
        self.account = account or {}
        self.receipts = receipts or []
        self.ledger = ledger or []

    def get_account(self, connection_id):
        return dict(self.account)

    def list_accounts(self, broker=None, limit=None):
        return [dict(self.account)]

    def recent_open_positions(self, connection_id):
        return [dict(r) for r in self.receipts]

    def list_ledger(self, connection_id=None, limit=200):
        return [dict(r) for r in self.ledger][:limit]

    def list_receipts(self, connection_id=None, limit=100):
        return [dict(r) for r in self.receipts][:limit]


def _engine_fixture(now=None):
    now = now or int(time.time())
    base = now - (now % 900) - 1800
    bars = []
    for step in range(3):
        opened = base + step * 300
        bars.append({
            "symbol": "EURUSD", "timeframe": "M5",
            "open_time": _iso(opened), "close_time": _iso(opened + 300),
            "open": 1.0845, "high": 1.0855, "low": 1.0840,
            "close": 1.0850, "volume": 120,
            "bid": 1.08499, "ask": 1.08501, "spread": 0.00002,
            "point": 0.00001, "closed": True,
        })
    account = {
        "connection_id": "mt5_engine_1", "mt5_login": 910001,
        "server": "Broker-Demo", "broker": "Broker", "owner_id": "own_1",
        "display_name": "APEX EURUSD", "environment": "demo",
        "symbols": "EURUSD", "equity": 10250.5, "balance": 10000.0,
    }
    receipts = [{
        "receipt_id": "rcpt_1", "connection_id": "mt5_engine_1",
        "mt5_login": 910001, "symbol": "EURUSD", "direction": "BUY",
        "native_ticket": 555, "volume": 0.10, "fill_price": 1.0850,
        "stop_loss": 1.0800, "take_profit": 1.0900, "commission": -0.5,
        "swap": -0.2, "execution_state": "OPEN",
        "reconciled_state": "MATCHED", "received_at": _iso(now - 600),
        "reason_code": "APEX_TREND",
    }]
    ledger = [
        {"id": 1, "connection_id": "mt5_engine_1", "native_ticket": 555,
         "event": "OPEN", "symbol": "EURUSD", "direction": "BUY",
         "volume": 0.10, "price": 1.0850, "realized_pnl": 0.0,
         "commission": 0.0, "swap": 0.0,
         "observed_at": _iso(now - 600)},
        {"id": 2, "connection_id": "mt5_engine_1", "native_ticket": 777,
         "event": "CLOSE", "symbol": "EURUSD", "direction": "SELL",
         "volume": 0.10, "price": 1.0865, "realized_pnl": 15.0,
         "commission": -0.5, "swap": 0.0,
         "observed_at": _iso(now - 120)},
    ]
    export = {
        "identity": {"login": 910001, "broker": "Broker",
                     "server": "Broker-Demo", "currency": "USD",
                     "leverage": 100, "equity": 10250.5, "balance": 10000.0},
        "bars": bars, "mtime": time.time(), "path": "/tmp/export",
    }
    return account, receipts, ledger, export


def test_engine_transport_is_read_only_and_serves_native_data():
    account, receipts, ledger, export = _engine_fixture()
    store = FakeEngineStore(account, receipts, ledger)
    transport = EngineFeedTransport(
        {"connection_id": "eng_mt5_engine_1",
         "engine_connection_id": "mt5_engine_1",
         "market_data_mode": "engine"},
        engine_store=store,
        export_reader=lambda cid, root: export,
    )
    assert transport.kind == "engine"
    assert transport.connected is True

    info = transport.account_information()
    assert info.login == 910001
    assert info.equity == 10250.5
    assert info.trade_allowed is False
    assert info.stale is False

    symbols = transport.symbols()
    assert [s.symbol for s in symbols] == ["EURUSD"]
    assert symbols[0].digits == 5
    assert symbols[0].trade_mode == "disabled"

    price = transport.price("EURUSD")
    assert (price.bid, price.ask) == (1.08499, 1.08501)

    five = transport.candles("EURUSD", "5m", None, 10)
    assert len(five) == 3 and five[-1].timeframe == "5m"
    fifteen = transport.candles("EURUSD", "15m", None, 10)
    assert len(fifteen) == 1
    assert fifteen[0].open_time == five[0].open_time
    assert fifteen[0].high == max(c.high for c in five)
    with pytest.raises(TransportError) as err:
        transport.candles("EURUSD", "1m", None, 10)
    assert err.value.code == "TIMEFRAME_NOT_AVAILABLE"

    positions = transport.positions()
    assert len(positions) == 1
    position = positions[0]
    assert position.position_id == "555"
    assert position.commission == -0.5
    # (1.08499 - 1.08500) * 0.10 * 100000 = -0.10, plus -0.5 / -0.2 costs
    assert position.profit == pytest.approx(-0.8, abs=0.01)

    deals = transport.deals(None, None, 10)
    assert [d.deal_id for d in deals] == ["ldg-2"]
    assert deals[0].profit == 15.0
    assert transport.orders() == []

    with pytest.raises(TransportError) as err:
        transport.execute("market_order", {}, "cmd_1")
    assert err.value.code == "READ_ONLY_ACCOUNT"


def test_stale_export_is_flagged_and_marked_disconnected():
    account, receipts, ledger, export = _engine_fixture()
    export = dict(export, mtime=time.time() - 600)
    transport = EngineFeedTransport(
        {"connection_id": "eng_x", "engine_connection_id": "mt5_engine_1"},
        engine_store=FakeEngineStore(account, receipts, ledger),
        export_reader=lambda cid, root: export,
        stale_after_s=90,
    )
    assert transport.connected is False
    info = transport.account_information()
    assert info.stale is True and info.connected is False


def test_service_adopts_engine_accounts_read_only(tmp_path, monkeypatch):
    import abutron.db as engine_db_module

    account, receipts, ledger, _export = _engine_fixture()
    fake = FakeEngineStore(account, receipts, ledger)
    monkeypatch.setattr(engine_db_module, "engine_db", fake)

    settings = GatewaySettings(
        db_path=str(tmp_path / "gateway.db"), demo_enabled=False,
        webhooks_enabled=False, webhook_worker=False, landing_page=False)
    store = GatewayStore(settings.db_path)
    store.init()
    service = GatewayService(store, settings)
    adopted = service.adopt_engine_accounts()
    assert len(adopted) == 1
    mirror = adopted[0]
    assert mirror["connection_id"].startswith("eng_")
    assert mirror["tenant_id"] == "tnt_platform"
    assert mirror["market_data_mode"] == "engine"
    assert mirror["control_mode"] == "read_only"
    assert mirror["trading_enabled"] == 0
    assert mirror["engine_connection_id"] == "mt5_engine_1"
    assert mirror["symbols"] == ["EURUSD"]

    # Idempotent: a second boot updates instead of duplicating.
    again = service.adopt_engine_accounts()
    assert [a["connection_id"] for a in again] == [mirror["connection_id"]]
    assert len(store.list_accounts()) == 1

    # Trading through the API is refused before any transport is consulted.
    with pytest.raises(Exception) as err:
        service._guard_trade(store.get_account_any(
            mirror["connection_id"]), service._transport(mirror))
    assert getattr(err.value, "code", "") == "ENGINE_MANAGED_ACCOUNT"
