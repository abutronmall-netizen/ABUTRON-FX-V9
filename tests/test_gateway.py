"""ABUTRON MT5 Gateway acceptance tests.

Covers the promises the product makes:

* tenant isolation and hashed API keys;
* the account lifecycle (simulated + bridge-backed);
* market data, positions, orders, deal history;
* market/pending orders, modify, partial/full close, cancel;
* idempotent writes and rate limits;
* control-mode / kill-switch guards;
* the bridge pull/push plane, including a slow-bridge 202;
* signed webhook delivery records;
* the WebSocket stream.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.gateway.app import create_app
from abutron.gateway.settings import GatewaySettings
from abutron.gateway.webhooks import sign_payload


DEMO_KEY = "abk_demo_0123456789abcdef0123456789abcdef"


@pytest.fixture
def gw(tmp_path):
    settings = GatewaySettings(
        db_path=str(tmp_path / "gateway.db"),
        demo_enabled=False,
        webhooks_enabled=True,
        webhook_worker=False,
        guard_automation=False,
        command_timeout_s=0.25,
        demo_api_key=DEMO_KEY,
        landing_page=False,
    )
    from fastapi.testclient import TestClient
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app, settings


@pytest.fixture
def tenant(gw):
    client, app, _settings = gw
    tenant_id, key = app.state.service.create_tenant("acme")
    return {"tenant_id": tenant_id, "key": key, "headers": {
        "Authorization": f"Bearer {key}"}, "client": client, "app": app}


def new_account(tenant, **overrides):
    body = {"login": 5001234, "server": "Acme-Demo", "broker": "acme",
            "environment": "demo", "market_data_mode": "simulated",
            "symbols": ["EURUSD", "GBPUSD", "XAUUSD"],
            "max_lot": 5.0, "max_open_positions": 20,
            "name": "Test account"}
    body.update(overrides)
    response = tenant["client"].post("/v1/accounts", headers=tenant["headers"],
                                     json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Auth & isolation
# ---------------------------------------------------------------------------
def test_missing_and_bad_key(gw):
    client, _app, _ = gw
    assert client.get("/v1/accounts").status_code == 401
    bad = client.get("/v1/accounts",
                     headers={"Authorization": "Bearer abk_nope_wrong"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "UNAUTHORIZED"


def test_scripted_key_only_hash_is_stored(gw):
    client, app, _ = gw
    tid, key = app.state.service.create_tenant("hash-check")
    stored = app.state.service.store.get_api_key_by_prefix(key.split("_")[1])
    assert stored is not None
    assert key.split("_")[2] not in stored["key_hash"]
    assert stored["key_hash"] != key


def test_tenant_isolation(gw):
    client, app, _ = gw
    t1, k1 = app.state.service.create_tenant("one")
    t2, k2 = app.state.service.create_tenant("two")
    h1 = {"Authorization": f"Bearer {k1}"}
    h2 = {"Authorization": f"Bearer {k2}"}
    account = client.post("/v1/accounts", headers=h1, json={
        "login": 1, "server": "S", "market_data_mode": "simulated",
        "symbols": ["EURUSD"]}).json()
    aid = account["account_id"]
    assert client.get(f"/v1/accounts/{aid}", headers=h1).status_code == 200
    leaked = client.get(f"/v1/accounts/{aid}", headers=h2)
    assert leaked.status_code == 404
    assert client.get(f"/v1/accounts/{aid}/positions",
                      headers=h2).status_code == 404


def test_read_only_scope_cannot_trade(gw):
    client, app, _ = gw
    tid = app.state.service.store.create_tenant("scope")
    key = "abk_scopeonly_0123456789abcdef0123456789abcdef"
    app.state.service.keys.register_key(key, tid, scopes=["read"])
    headers = {"Authorization": f"Bearer {key}"}
    account = client.post("/v1/accounts", headers={
        "Authorization": f"Bearer {app.state.service.keys.create_key(tid)[0]}"},
        json={"login": 2, "server": "S", "market_data_mode": "simulated",
              "symbols": ["EURUSD"]}).json()
    response = client.post(f"/v1/accounts/{account['account_id']}/trade",
                           headers=headers,
                           json={"side": "buy", "symbol": "EURUSD",
                                 "volume": 0.1})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN_SCOPE"


def test_rate_limit(gw):
    client, app, _ = gw
    tid = app.state.service.store.create_tenant("limited")
    key = "abk_limited_0123456789abcdef0123456789abcdef"
    app.state.service.keys.register_key(key, tid, rate_limit_per_min=2)
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/accounts", headers=headers).status_code == 200
    assert client.get("/v1/accounts", headers=headers).status_code == 200
    blocked = client.get("/v1/accounts", headers=headers)
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"


# ---------------------------------------------------------------------------
# Account lifecycle
# ---------------------------------------------------------------------------
def test_create_and_list_account(tenant):
    client = tenant["client"]
    account = new_account(tenant)
    assert account["status"] == "CONNECTED"
    assert account["market_data_mode"] == "simulated"
    assert account["bridge"] is None
    assert account["credentials_accepted"] is False
    listing = client.get("/v1/accounts", headers=tenant["headers"]).json()
    assert listing["count"] == 1
    info = client.get(f"/v1/accounts/{account['account_id']}/information",
                      headers=tenant["headers"]).json()
    assert info["connected"] is True
    assert info["balance"] > 0


def test_delete_account(tenant):
    account = new_account(tenant)
    aid = account["account_id"]
    response = tenant["client"].delete(f"/v1/accounts/{aid}",
                                       headers=tenant["headers"])
    assert response.status_code == 200
    assert tenant["client"].get(f"/v1/accounts/{aid}",
                                headers=tenant["headers"]).status_code == 404
    assert tenant["client"].get("/v1/accounts",
                                headers=tenant["headers"]).json()["count"] == 0


def test_kill_switch_blocks_new_trades(tenant):
    client = tenant["client"]
    aid = new_account(tenant)["account_id"]
    response = client.patch(f"/v1/accounts/{aid}", headers=tenant["headers"],
                            json={"trading_enabled": False})
    assert response.status_code == 200
    assert response.json()["trading_enabled"] is False
    trade = client.post(f"/v1/accounts/{aid}/trade", headers=tenant["headers"],
                        json={"side": "buy", "symbol": "EURUSD",
                              "volume": 0.1})
    assert trade.status_code == 409
    assert trade.json()["error"]["code"] == "TRADING_DISABLED"


def test_read_only_control_mode(tenant):
    client = tenant["client"]
    aid = new_account(tenant, control_mode="read_only")["account_id"]
    trade = client.post(f"/v1/accounts/{aid}/trade", headers=tenant["headers"],
                        json={"side": "buy", "symbol": "EURUSD",
                              "volume": 0.1})
    assert trade.status_code == 409
    assert trade.json()["error"]["code"] == "ACCOUNT_READ_ONLY"


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
def test_symbols_prices_candles(tenant):
    client = tenant["client"]
    aid = new_account(tenant)["account_id"]
    h = tenant["headers"]
    symbols = client.get(f"/v1/accounts/{aid}/symbols", headers=h).json()
    assert {s["symbol"] for s in symbols} == {"EURUSD", "GBPUSD", "XAUUSD"}
    price = client.get(f"/v1/accounts/{aid}/symbols/EURUSD/price",
                       headers=h).json()
    assert price["ask"] >= price["bid"] > 0
    candles = client.get(
        f"/v1/accounts/{aid}/symbols/EURUSD/candles?timeframe=5m&limit=20",
        headers=h).json()
    assert len(candles) == 20
    assert candles == sorted(candles, key=lambda c: c["open_time"])
    assert all(c["high"] >= c["low"] for c in candles)
    assert all(c["high"] >= c["open"] and c["high"] >= c["close"]
               for c in candles)
    missing = client.get(f"/v1/accounts/{aid}/symbols/NOPE/price", headers=h)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "SYMBOL_NOT_FOUND"


# ---------------------------------------------------------------------------
# Trading
# ---------------------------------------------------------------------------
def test_market_order_and_idempotency(tenant):
    client = tenant["client"]
    aid = new_account(tenant)["account_id"]
    headers = {**tenant["headers"], "Idempotency-Key": "order-1"}
    payload = {"action": "market", "side": "buy", "symbol": "EURUSD",
               "volume": 0.10, "stop_loss": 1.0700, "take_profit": 1.1000}
    first = client.post(f"/v1/accounts/{aid}/trade", headers=headers,
                        json=payload)
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "EXECUTED"
    assert body["position_id"]
    positions = client.get(f"/v1/accounts/{aid}/positions",
                           headers=tenant["headers"]).json()
    assert len(positions) == 1
    assert positions[0]["volume"] == 0.1

    replay = client.post(f"/v1/accounts/{aid}/trade", headers=headers,
                         json=payload)
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert len(client.get(f"/v1/accounts/{aid}/positions",
                          headers=tenant["headers"]).json()) == 1

    changed = client.post(f"/v1/accounts/{aid}/trade", headers=headers,
                          json={**payload, "volume": 0.2})
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_stop_validation_and_volume(tenant):
    client = tenant["client"]
    aid = new_account(tenant)["account_id"]
    h = tenant["headers"]
    bad_volume = client.post(f"/v1/accounts/{aid}/trade", headers=h, json={
        "side": "buy", "symbol": "EURUSD", "volume": 500})
    assert bad_volume.status_code == 400
    assert bad_volume.json()["error"]["code"] == "INVALID_VOLUME"

    price = client.get(f"/v1/accounts/{aid}/symbols/EURUSD/price",
                       headers=h).json()
    bad_stops = client.post(f"/v1/accounts/{aid}/trade", headers=h, json={
        "side": "buy", "symbol": "EURUSD", "volume": 0.1,
        "stop_loss": price["ask"] + 1.0})
    assert bad_stops.status_code == 400
    assert bad_stops.json()["error"]["code"] == "INVALID_STOPS"


def test_modify_and_close_position(tenant):
    client = tenant["client"]
    aid = new_account(tenant)["account_id"]
    h = tenant["headers"]
    trade = client.post(f"/v1/accounts/{aid}/trade", headers=h, json={
        "side": "buy", "symbol": "EURUSD", "volume": 0.2,
        "take_profit": 1.2000}).json()
    pid = trade["position_id"]

    modified = client.post(
        f"/v1/accounts/{aid}/positions/{pid}/modify", headers=h,
        json={"stop_loss": 1.0000, "take_profit": 1.3000})
    assert modified.status_code == 200
    # Updating only the TP must preserve the existing SL (a null SL in the
    # command would otherwise clear the stop on a real EA).
    partial = client.post(
        f"/v1/accounts/{aid}/positions/{pid}/modify", headers=h,
        json={"take_profit": 1.3500})
    assert partial.status_code == 200
    position = client.get(f"/v1/accounts/{aid}/positions",
                          headers=h).json()[0]
    assert position["stop_loss"] == pytest.approx(1.0)
    assert position["take_profit"] == pytest.approx(1.35)

    closed = client.post(f"/v1/accounts/{aid}/positions/{pid}/close",
                         headers=h,
                         json={"volume": 0.1, "comment": "half"})
    assert closed.status_code == 200
    remaining = client.get(f"/v1/accounts/{aid}/positions",
                           headers=h).json()
    assert len(remaining) == 1 and remaining[0]["volume"] == pytest.approx(0.1)

    full = client.post(f"/v1/accounts/{aid}/positions/{pid}/close",
                       headers=h, json={})
    assert full.status_code == 200
    assert client.get(f"/v1/accounts/{aid}/positions",
                      headers=h).json() == []
    deals = client.get(f"/v1/accounts/{aid}/history/deals", headers=h).json()
    assert len(deals) >= 3  # entry + two closes
    assert any(d["comment"] == "half" for d in deals)


def test_pending_order_place_and_cancel(tenant):
    client = tenant["client"]
    aid = new_account(tenant)["account_id"]
    h = tenant["headers"]
    price = client.get(f"/v1/accounts/{aid}/symbols/EURUSD/price",
                       headers=h).json()
    order = client.post(f"/v1/accounts/{aid}/trade", headers=h, json={
        "action": "pending", "side": "buy", "symbol": "EURUSD",
        "volume": 0.1, "order_type": "limit",
        "price": round(price["bid"] * 0.98, 5)}).json()
    assert order["status"] == "EXECUTED"
    orders = client.get(f"/v1/accounts/{aid}/orders", headers=h).json()
    assert len(orders) == 1 and orders[0]["type"] == "limit"
    oid = orders[0]["order_id"]
    cancelled = client.post(f"/v1/accounts/{aid}/orders/{oid}/cancel",
                            headers=h)
    assert cancelled.status_code == 200
    assert client.get(f"/v1/accounts/{aid}/orders", headers=h).json() == []
    history = client.get(f"/v1/accounts/{aid}/history/orders",
                         headers=h).json()
    assert any(o["state"] == "cancelled" for o in history)


def test_max_open_positions_guard(tenant):
    client = tenant["client"]
    aid = new_account(tenant, max_open_positions=1)["account_id"]
    h = tenant["headers"]
    assert client.post(f"/v1/accounts/{aid}/trade", headers=h, json={
        "side": "buy", "symbol": "EURUSD", "volume": 0.1}).status_code == 200
    blocked = client.post(f"/v1/accounts/{aid}/trade", headers=h, json={
        "side": "buy", "symbol": "GBPUSD", "volume": 0.1})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "MAX_OPEN_POSITIONS"


def test_symbol_allowlist(tenant):
    client = tenant["client"]
    aid = new_account(tenant, symbol_allowlist=["EURUSD"])["account_id"]
    h = tenant["headers"]
    blocked = client.post(f"/v1/accounts/{aid}/trade", headers=h, json={
        "side": "buy", "symbol": "GBPUSD", "volume": 0.1})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "SYMBOL_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# Bridge plane
# ---------------------------------------------------------------------------
def _bridge_account(tenant):
    account = new_account(tenant, market_data_mode="bridge",
                          symbols=["EURUSD"])
    assert account["bridge"] is not None
    return account


def _push_state(client, account, token, *, balance=5000.0, bid=1.0850):
    state = {
        "sequence": 1,
        "account": {"login": account["login"], "currency": "GBP",
                    "leverage": 200, "balance": balance, "equity": balance,
                    "margin": 0, "free_margin": balance, "margin_level": 0,
                    "trade_allowed": True, "connected": True},
        "symbols": [{"symbol": "EURUSD", "digits": 5, "contract_size": 100000,
                     "min_volume": 0.01, "max_volume": 50,
                     "volume_step": 0.01}],
        "prices": {"EURUSD": {"bid": bid, "ask": bid + 0.0002,
                              "time": "2026-11-18T09:00:00Z"}},
        "positions": [], "orders": [], "deals": [], "history_orders": [],
    }
    response = client.post(account["bridge"]["state_url"],
                           headers={"X-Bridge-Token": token}, json=state)
    assert response.status_code == 200, response.text
    return response.json()


def test_bridge_handshake_auth_and_state(tenant):
    client = tenant["client"]
    account = _bridge_account(tenant)
    token = account["bridge"]["token"]
    url = account["bridge"]["handshake_url"]

    assert client.get(url, headers={"X-Bridge-Token": "nope"}).status_code == 401
    handshake = client.get(url, headers={"X-Bridge-Token": token})
    assert handshake.status_code == 200
    assert handshake.json()["login"] == account["login"]
    assert handshake.json()["command_poll_wait_s"] >= 1

    pushed = _push_state(client, account, token, balance=7777.0)
    assert pushed["ok"] is True
    info = client.get(
        f"/v1/accounts/{account['account_id']}/information",
        headers=tenant["headers"]).json()
    assert info["balance"] == pytest.approx(7777.0)
    assert info["connected"] is True
    price = client.get(
        f"/v1/accounts/{account['account_id']}/symbols/EURUSD/price",
        headers=tenant["headers"]).json()
    assert price["bid"] == pytest.approx(1.0850, abs=1e-6)


def test_bridge_slow_trade_returns_202_then_completes(tenant):
    client = tenant["client"]
    account = _bridge_account(tenant)
    token = account["bridge"]["token"]
    _push_state(client, account, token)
    aid = account["account_id"]
    h = tenant["headers"]

    trade = client.post(f"/v1/accounts/{aid}/trade", headers=h, json={
        "side": "buy", "symbol": "EURUSD", "volume": 0.1,
        "stop_loss": 1.0700, "take_profit": 1.1000})
    assert trade.status_code == 202, trade.text
    body = trade.json()
    assert body["status"] == "QUEUED"
    command_id = body["command_id"]
    assert body["poll_url"].endswith(command_id)

    commands = client.get(account["bridge"]["commands_url"] + "?wait=0",
                          headers={"X-Bridge-Token": token}).json()
    assert commands["count"] == 1
    command = commands["commands"][0]
    assert command["kind"] == "market"
    assert command["payload"]["symbol"] == "EURUSD"

    result_url = account["bridge"]["result_url_template"].replace(
        "{command_id}", command_id)
    result = client.post(result_url, headers={"X-Bridge-Token": token}, json={
        "accepted": True, "retcode": "DONE", "order_id": "9001",
        "position_id": "9001", "deal_id": "9002", "price": 1.0852,
        "volume": 0.1})
    assert result.status_code == 200

    polled = client.get(f"/v1/accounts/{aid}/commands/{command_id}",
                        headers=h).json()
    assert polled["status"] == "DONE"
    assert polled["result"]["position_id"] == "9001"


def test_bridge_text_command_format(tenant):
    client = tenant["client"]
    account = _bridge_account(tenant)
    token = account["bridge"]["token"]
    _push_state(client, account, token)
    client.post(f"/v1/accounts/{account['account_id']}/trade",
                headers=tenant["headers"],
                json={"side": "sell", "symbol": "EURUSD", "volume": 0.1,
                      "comment": "ea-test"})
    text = client.get(account["bridge"]["commands_url"] + "?format=text",
                      headers={"X-Bridge-Token": token})
    assert text.status_code == 200
    assert "kind=market" in text.text
    assert "side=sell" in text.text


def test_bridge_transport_resolves_waiting_command(gw):
    """The transport must return the bridge's real outcome when it arrives."""
    _client, app, settings = gw
    store = app.state.store
    tid = store.create_tenant("bridge-unit")
    account = store.create_account(
        connection_id="acc_bridge_unit", tenant_id=tid, name="b",
        broker="acme", server="S", login=42, platform="mt5",
        environment="demo", control_mode="api", status="CONNECTED",
        trading_enabled=1, market_data_mode="bridge", symbols="EURUSD",
        bridge_salt="salt", state_json="{}", state_sequence=0, last_error="")
    from abutron.gateway.transports.bridge import BridgeTransport
    transport = BridgeTransport(account, store, settings)
    command = store.create_command(tenant_id=tid,
                                   connection_id="acc_bridge_unit",
                                   kind="market", payload={}, ttl_s=30)

    def complete():
        time.sleep(0.2)
        store.complete_command("acc_bridge_unit", command["command_id"],
                               "DONE", {"accepted": True, "status": "EXECUTED",
                                        "retcode": "DONE",
                                        "position_id": "77"})

    threading.Thread(target=complete, daemon=True).start()
    result = transport.execute("market", {}, command["command_id"])
    assert result["status"] == "EXECUTED"
    assert result["position_id"] == "77"


# ---------------------------------------------------------------------------
# Webhooks & audit
# ---------------------------------------------------------------------------
def test_webhook_records_delivery(tenant):
    client = tenant["client"]
    h = tenant["headers"]
    hook = client.post("/v1/webhooks", headers=h, json={
        "url": "https://example.com/hook", "events": ["*"]})
    assert hook.status_code == 201
    assert hook.json()["secret"].startswith("whsec_")
    aid = new_account(tenant)["account_id"]
    trade = client.post(f"/v1/accounts/{aid}/trade", headers=h, json={
        "side": "buy", "symbol": "EURUSD", "volume": 0.1})
    assert trade.status_code == 200
    deliveries = tenant["app"].state.store.list_deliveries(
        tenant["tenant_id"])
    event_types = {d["event_type"] for d in deliveries}
    assert "trade.executed" in event_types
    assert "position.opened" in event_types
    # Listing never re-reveals the secret.
    listed = client.get("/v1/webhooks", headers=h).json()
    assert listed[0]["secret"] is None


def test_webhook_signature_verifies():
    secret = "whsec_test"
    body = json.dumps({"hello": "world"})
    timestamp = 1_700_000_000
    signature = sign_payload(secret, timestamp, body)
    import hashlib
    import hmac
    expected = hmac.new(secret.encode(),
                        f"{timestamp}.{body}".encode(),
                        hashlib.sha256).hexdigest()
    assert signature == expected


def test_audit_trail(tenant):
    client = tenant["client"]
    account = new_account(tenant)
    client.post(f"/v1/accounts/{account['account_id']}/trade",
                headers=tenant["headers"],
                json={"side": "buy", "symbol": "EURUSD", "volume": 0.1})
    audit = client.get("/v1/audit", headers=tenant["headers"]).json()
    actions = {row["action"] for row in audit}
    assert "account.created" in actions
    assert any(a.startswith("market.") for a in actions)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
def test_websocket_stream(tenant):
    client = tenant["client"]
    account = new_account(tenant)
    aid = account["account_id"]
    key = tenant["key"]
    with client.websocket_connect(
            f"/v1/stream?account_id={aid}&api_key={key}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "connected"
        assert hello["account_id"] == aid
        types = set()
        for _ in range(12):
            message = ws.receive_json()
            types.add(message["type"])
            if message["type"] in ("account", "snapshot", "quote"):
                continue
        assert "account" in types
        assert "snapshot" in types
        assert "quote" in types


def test_websocket_rejects_foreign_account(tenant, gw):
    client = tenant["client"]
    _c, app, _s = gw
    other_tid, other_key = app.state.service.create_tenant("other")
    account = new_account(tenant)
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
                f"/v1/stream?account_id={account['account_id']}"
                f"&api_key={other_key}") as ws:
            ws.receive_json()


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------
def test_health_and_openapi(gw):
    client, _app, _ = gw
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["service"] == "abutron-mt5-gateway"
    schema = client.get("/openapi.json").json()
    assert "/v1/accounts/{account_id}/trade" in schema["paths"]
    # The bridge plane is deliberately absent from the public schema.
    assert not any(path.startswith("/internal") for path in schema["paths"])
