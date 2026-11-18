#!/usr/bin/env python3
"""Minimal Python client for the ABUTRON MT5 Gateway.

Two uses:

1. As a library — copy the ``GatewayClient`` class into your bot.
2. As a smoke test — run a full round trip against a local gateway:

       export ABUTRON_GATEWAY_KEY=abk_demo_...
       python examples/gateway_client.py --demo

   ``--demo`` provisions a simulated account, reads candles, opens a
   position, streams a few WebSocket events, then closes the position and
   prints the deal history.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

import httpx


class GatewayError(RuntimeError):
    def __init__(self, status_code: int, payload: Dict[str, Any]):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"{status_code}: {payload}")


class GatewayClient:
    """Synchronous REST client. One instance = one tenant API key."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url, timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}",
                     "User-Agent": "abutron-gateway-example/0.1"})

    # ---- plumbing ----
    def _call(self, method: str, path: str, **kwargs) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {"error": {"code": "HTTP_ERROR",
                                     "message": response.text[:200]}}
            raise GatewayError(response.status_code, payload)
        return response.json()

    def close(self) -> None:
        self._client.close()

    # ---- accounts ----
    def create_account(self, *, login: int, server: str,
                       symbols: Optional[List[str]] = None,
                       market_data_mode: str = "simulated",
                       environment: str = "demo",
                       control_mode: str = "api", **extra) -> Dict[str, Any]:
        body = {"login": login, "server": server,
                "market_data_mode": market_data_mode,
                "environment": environment, "control_mode": control_mode,
                "symbols": symbols or ["EURUSD", "GBPUSD", "XAUUSD"]}
        body.update(extra)
        return self._call("POST", "/v1/accounts", json=body)

    def accounts(self) -> List[Dict[str, Any]]:
        return self._call("GET", "/v1/accounts")["items"]

    def account(self, account_id: str) -> Dict[str, Any]:
        return self._call("GET", f"/v1/accounts/{account_id}")

    def account_information(self, account_id: str) -> Dict[str, Any]:
        return self._call("GET", f"/v1/accounts/{account_id}/information")

    def set_trading(self, account_id: str, enabled: bool) -> Dict[str, Any]:
        return self._call("PATCH", f"/v1/accounts/{account_id}",
                          json={"trading_enabled": enabled})

    # ---- market data ----
    def symbols(self, account_id: str) -> List[Dict[str, Any]]:
        return self._call("GET", f"/v1/accounts/{account_id}/symbols")

    def price(self, account_id: str, symbol: str) -> Dict[str, Any]:
        return self._call(
            "GET", f"/v1/accounts/{account_id}/symbols/{symbol}/price")

    def candles(self, account_id: str, symbol: str, timeframe: str = "5m",
                limit: int = 100) -> List[Dict[str, Any]]:
        return self._call(
            "GET", f"/v1/accounts/{account_id}/symbols/{symbol}/candles",
            params={"timeframe": timeframe, "limit": limit})

    # ---- state ----
    def positions(self, account_id: str) -> List[Dict[str, Any]]:
        return self._call("GET", f"/v1/accounts/{account_id}/positions")

    def orders(self, account_id: str) -> List[Dict[str, Any]]:
        return self._call("GET", f"/v1/accounts/{account_id}/orders")

    def deals(self, account_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self._call("GET", f"/v1/accounts/{account_id}/history/deals",
                          params={"limit": limit})

    def snapshot(self, account_id: str) -> Dict[str, Any]:
        return self._call("GET", f"/v1/accounts/{account_id}/snapshot")

    # ---- trading ----
    def market_order(self, account_id: str, *, side: str, symbol: str,
                     volume: float, stop_loss: Optional[float] = None,
                     take_profit: Optional[float] = None,
                     comment: str = "sdk",
                     idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        return self._trade(account_id, {
            "action": "market", "side": side, "symbol": symbol,
            "volume": volume, "stop_loss": stop_loss,
            "take_profit": take_profit, "comment": comment,
        }, idempotency_key)

    def pending_order(self, account_id: str, *, side: str, symbol: str,
                      volume: float, price: float, order_type: str = "limit",
                      stop_loss: Optional[float] = None,
                      take_profit: Optional[float] = None,
                      idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        return self._trade(account_id, {
            "action": "pending", "side": side, "symbol": symbol,
            "volume": volume, "price": price, "order_type": order_type,
            "stop_loss": stop_loss, "take_profit": take_profit,
        }, idempotency_key)

    def _trade(self, account_id: str, body: Dict[str, Any],
               idem: Optional[str]) -> Dict[str, Any]:
        headers = {"Idempotency-Key": idem} if idem else {}
        response = self._client.post(f"/v1/accounts/{account_id}/trade",
                                     json=body, headers=headers)
        payload = response.json()
        if response.status_code >= 400:
            raise GatewayError(response.status_code, payload)
        return payload

    def close_position(self, account_id: str, position_id: str,
                       volume: Optional[float] = None) -> Dict[str, Any]:
        return self._call(
            "POST", f"/v1/accounts/{account_id}/positions/{position_id}/close",
            json={"volume": volume} if volume else {})

    def modify_position(self, account_id: str, position_id: str, *,
                        stop_loss: Optional[float] = None,
                        take_profit: Optional[float] = None) -> Dict[str, Any]:
        return self._call(
            "POST", f"/v1/accounts/{account_id}/positions/{position_id}/modify",
            json={"stop_loss": stop_loss, "take_profit": take_profit})

    def cancel_order(self, account_id: str, order_id: str) -> Dict[str, Any]:
        return self._call(
            "POST", f"/v1/accounts/{account_id}/orders/{order_id}/cancel")

    def command(self, account_id: str, command_id: str) -> Dict[str, Any]:
        return self._call(
            "GET", f"/v1/accounts/{account_id}/commands/{command_id}")


# ---------------------------------------------------------------------------
# WebSocket client (async)
# ---------------------------------------------------------------------------
async def stream_events(base_url: str, api_key: str, account_id: str,
                        seconds: float = 10.0,
                        channels: str = "quotes,positions,trades,commands"):
    """Print events for ``seconds``. Requires the ``websockets`` package."""
    import websockets

    ws_base = base_url.replace("https://", "wss://").replace(
        "http://", "ws://")
    url = (f"{ws_base}/v1/stream?account_id={account_id}"
           f"&channels={channels}&api_key={api_key}")
    async with websockets.connect(url) as ws:
        deadline = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < deadline:
            timeout = max(0.1, deadline - asyncio.get_event_loop().time())
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            message = json.loads(raw)
            if message.get("type") == "quote":
                data = message.get("data", {})
                print(f"  quote {data.get('symbol')} "
                      f"{data.get('bid')}/{data.get('ask')}")
            else:
                print(f"  {message.get('type')} "
                      f"{json.dumps(message.get('data'))[:120]}")


# ---------------------------------------------------------------------------
# Demo round trip
# ---------------------------------------------------------------------------
def run_demo(client: GatewayClient) -> None:
    print("1. provisioning a simulated account")
    account = client.create_account(
        login=900001, server="SDK-Demo", broker="sdk",
        symbols=["EURUSD", "XAUUSD"], name="SDK smoke test",
        max_lot=2.0, max_open_positions=5)
    aid = account["account_id"]
    print(f"   account_id={aid} status={account['status']}")

    print("2. account information")
    info = client.account_information(aid)
    print(f"   balance={info['balance']} equity={info['equity']} "
          f"currency={info['currency']}")

    print("3. market data")
    candles = client.candles(aid, "EURUSD", "5m", 5)
    price = client.price(aid, "EURUSD")
    print(f"   last close={candles[-1]['close']} bid/ask="
          f"{price['bid']}/{price['ask']}")

    print("4. opening a market position (idempotent)")
    trade = client.market_order(
        aid, side="buy", symbol="EURUSD", volume=0.10,
        stop_loss=round(price["bid"] * 0.99, 5),
        take_profit=round(price["ask"] * 1.01, 5),
        idempotency_key="sdk-demo-1")
    print(f"   {trade['status']} position={trade['position_id']} "
          f"price={trade['price']}")
    replay = client.market_order(
        aid, side="buy", symbol="EURUSD", volume=0.10,
        stop_loss=round(price["bid"] * 0.99, 5),
        take_profit=round(price["ask"] * 1.01, 5),
        idempotency_key="sdk-demo-1")
    print(f"   replay returned idempotent_replay="
          f"{replay.get('idempotent_replay')}")

    print("5. streaming a few events")
    try:
        asyncio.run(stream_events(
            client.base_url,
            client._client.headers["Authorization"].split(" ", 1)[1],
            aid, seconds=3.0))
    except Exception as exc:
        print(f"   (streaming skipped: {type(exc).__name__}: {exc})")

    print("6. closing the position")
    pid = trade["position_id"]
    closed = client.close_position(aid, pid)
    print(f"   {closed['status']} deal={closed.get('deal_id')}")
    deals = client.deals(aid, limit=5)
    print(f"   deals recorded: {len(deals)}")
    for deal in deals[:3]:
        print(f"   - {deal['symbol']} {deal['side']} {deal['volume']} "
              f"@ {deal['price']} pnl={deal['profit']}")

    print("7. snapshot")
    snap = client.snapshot(aid)
    print(f"   positions={len(snap['positions'])} "
          f"orders={len(snap['orders'])} "
          f"prices={len(snap['prices'])}")
    print("\nDone. Docs: /docs · Audit: /v1/audit")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv(
        "ABUTRON_GATEWAY_URL", "http://127.0.0.1:19744"))
    parser.add_argument("--api-key", default=os.getenv(
        "ABUTRON_GATEWAY_KEY", ""))
    parser.add_argument("--account", default=os.getenv(
        "ABUTRON_GATEWAY_ACCOUNT", ""))
    parser.add_argument("--demo", action="store_true",
                        help="provision a simulated account and round-trip it")
    args = parser.parse_args()

    if not args.api_key:
        parser.error("set --api-key or ABUTRON_GATEWAY_KEY")
    client = GatewayClient(args.base_url, args.api_key)
    try:
        if args.demo or not args.account:
            run_demo(client)
        else:
            print(json.dumps(client.snapshot(args.account), indent=2))
    except GatewayError as exc:
        print(f"gateway error {exc.status_code}: "
              f"{json.dumps(exc.payload, indent=2)}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
