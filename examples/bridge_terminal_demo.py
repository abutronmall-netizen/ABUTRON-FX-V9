#!/usr/bin/env python3
"""Emulate AbuGatewayBridge.mq5 against a live gateway.

This is the proof that the bridge path needs neither MetaApi nor a real
MT5 terminal: a local process pushes a terminal-shaped state snapshot,
pulls queued commands exactly like the EA, executes them, and posts real
results back. The gateway's public REST API is exercised end to end.

    export ABUTRON_GATEWAY_KEY=abk_demo_...
    python examples/bridge_terminal_demo.py --demo

It creates a bridge-backed account, starts the fake terminal, opens a
market position through the public API, closes it half-way, then closes
the rest, and prints the deal history.

Use `--account acc_... --token ...` to drive an already provisioned
bridge account instead of creating one.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

BASE_PRICES = {"EURUSD": 1.0850, "GBPUSD": 1.2650, "XAUUSD": 2350.0,
               "US500": 5500.0}
SPECS = {
    "EURUSD": (5, 0.00001, 100000),
    "GBPUSD": (5, 0.00001, 100000),
    "XAUUSD": (2, 0.01, 100),
    "US500": (2, 0.01, 1),
}


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class FakeTerminal:
    """A behavioural stand-in for the MQL5 bridge EA."""

    def __init__(self, base_url: str, account: Dict[str, Any], token: str,
                 balance: float = 10_000.0):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=10.0)
        self.account = account
        self.account_id = account["account_id"]
        self.token = token
        self.bridge = account["bridge"]
        self.balance = balance
        self.symbols = list(account.get("symbols") or ["EURUSD", "XAUUSD"])
        self.mid = {s: BASE_PRICES.get(s.upper(), 1.0) for s in self.symbols}
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.deals: List[Dict[str, Any]] = []
        self.seq = 0
        self.ticket = int(time.time()) % 100000 + 700000
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.errors = 0

    # ---- helpers -----------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        return {"X-Bridge-Token": self.token}

    def _next_ticket(self) -> str:
        self.ticket += 1
        return str(self.ticket)

    def _tick(self) -> None:
        for symbol in self.mid:
            self.mid[symbol] *= 1.0 + random.uniform(-0.00012, 0.00012)

    def _price(self, symbol: str) -> Dict[str, float]:
        base = BASE_PRICES.get(symbol.upper(), 1.0)
        spread = base * 0.00002 if base > 10 else 0.00002
        mid = self.mid.get(symbol, base)
        return {"bid": round(mid - spread / 2, 6),
                "ask": round(mid + spread / 2, 6)}

    def _profit(self, position: Dict[str, Any]) -> float:
        price = self._price(position["symbol"])
        current = price["bid"] if position["side"] == "buy" else price["ask"]
        contract = SPECS.get(position["symbol"].upper(), (5, 0.00001, 100000))[2]
        direction = 1 if position["side"] == "buy" else -1
        return direction * (current - position["open_price"]) * \
            position["volume"] * contract

    # ---- bridge protocol --------------------------------------------
    def push_state(self) -> None:
        self._tick()
        self.seq += 1
        positions = []
        for position in self.positions.values():
            price = self._price(position["symbol"])
            current = price["bid"] if position["side"] == "buy" else price["ask"]
            row = dict(position)
            row["current_price"] = current
            row["profit"] = round(self._profit(position), 2)
            positions.append(row)
        equity = self.balance + sum(p["profit"] for p in positions)
        body = {
            "sequence": self.seq,
            "account": {
                "login": self.account["login"], "currency": "USD",
                "leverage": 100, "balance": round(self.balance, 2),
                "equity": round(equity, 2), "margin": 0.0,
                "free_margin": round(equity, 2), "margin_level": 0.0,
                "trade_allowed": True, "connected": True,
                "updated_at": iso_now()},
            "symbols": [{
                "symbol": s, "digits": SPECS.get(s.upper(), (5,))[0],
                "point": SPECS.get(s.upper(), (5, 0.00001))[1],
                "contract_size": SPECS.get(s.upper(), (5, 0.00001, 100000))[2],
                "min_volume": 0.01, "max_volume": 50.0,
                "volume_step": 0.01} for s in self.symbols],
            "prices": {s: {**self._price(s), "time": iso_now()}
                       for s in self.symbols},
            "positions": positions,
            "orders": list(self.orders.values()),
            "deals": self.deals[-200:],
            "history_orders": [],
        }
        response = self.http.post(self.bridge["state_url"],
                                  headers=self._headers(), json=body)
        if response.status_code >= 400:
            self.errors += 1

    def pull_commands(self) -> List[Dict[str, Any]]:
        response = self.http.get(self.bridge["commands_url"],
                                 headers=self._headers(),
                                 params={"wait": 1, "limit": 10})
        if response.status_code >= 400:
            self.errors += 1
            return []
        return response.json().get("commands", [])

    def post_result(self, command_id: str, result: Dict[str, Any]) -> None:
        url = self.bridge["result_url_template"].replace(
            "{command_id}", command_id)
        response = self.http.post(url, headers=self._headers(), json=result)
        if response.status_code >= 400:
            self.errors += 1

    # ---- execution ---------------------------------------------------
    def execute(self, command: Dict[str, Any]) -> Dict[str, Any]:
        payload = command.get("payload") or {}
        kind = command.get("kind")
        try:
            if kind == "market":
                return self._market(payload)
            if kind == "pending":
                return self._pending(payload)
            if kind == "modify":
                return self._modify(payload)
            if kind == "close":
                return self._close(payload)
            if kind == "cancel":
                return self._cancel(payload)
        except Exception as exc:  # report, never crash the loop
            return {"accepted": False, "status": "REJECTED",
                    "retcode": "TERMINAL_ERROR", "message": str(exc)}
        return {"accepted": False, "status": "REJECTED",
                "retcode": "UNKNOWN_COMMAND", "message": str(kind)}

    def _market(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(payload["symbol"])
        side = str(payload["side"])
        volume = float(payload["volume"])
        price = self._price(symbol)
        fill = price["ask"] if side == "buy" else price["bid"]
        ticket = self._next_ticket()
        self.positions[ticket] = {
            "position_id": ticket, "symbol": symbol, "side": side,
            "volume": volume, "open_price": fill,
            "current_price": fill,
            "stop_loss": float(payload.get("stop_loss") or 0),
            "take_profit": float(payload.get("take_profit") or 0),
            "profit": 0.0, "swap": 0.0, "commission": -round(volume * 3.5, 2),
            "comment": payload.get("comment") or "", "magic": 20261118,
            "opened_at": iso_now()}
        deal_id = self._next_ticket()
        self.deals.append({
            "deal_id": deal_id, "position_id": ticket, "symbol": symbol,
            "side": side, "volume": volume, "price": fill, "profit": 0.0,
            "commission": round(volume * 3.5, 2), "swap": 0.0,
            "comment": "entry", "time": iso_now()})
        return {"accepted": True, "retcode": "DONE",
                "message": "Order executed", "order_id": ticket,
                "position_id": ticket, "deal_id": deal_id,
                "price": fill, "volume": volume}

    def _pending(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ticket = self._next_ticket()
        order = {
            "order_id": ticket, "symbol": str(payload["symbol"]),
            "side": str(payload["side"]), "type": str(payload.get("order_type", "limit")),
            "volume": float(payload["volume"]),
            "price": float(payload.get("price") or 0),
            "current_price": self._price(str(payload["symbol"]))["bid"],
            "stop_loss": float(payload.get("stop_loss") or 0),
            "take_profit": float(payload.get("take_profit") or 0),
            "state": "placed", "comment": payload.get("comment") or "",
            "created_at": iso_now()}
        self.orders[ticket] = order
        return {"accepted": True, "retcode": "DONE",
                "message": "Pending order placed", "order_id": ticket,
                "position_id": None, "deal_id": None,
                "price": order["price"], "volume": order["volume"]}

    def _modify(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        position = self.positions.get(str(payload.get("position_id")))
        if position is None:
            return {"accepted": False, "retcode": "POSITION_NOT_FOUND",
                    "message": "no such position"}
        position["stop_loss"] = float(payload.get("stop_loss") or 0)
        position["take_profit"] = float(payload.get("take_profit") or 0)
        return {"accepted": True, "retcode": "DONE",
                "message": "Position modified",
                "position_id": position["position_id"],
                "price": position["current_price"],
                "volume": position["volume"]}

    def _close(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        pid = str(payload.get("position_id"))
        position = self.positions.get(pid)
        if position is None:
            return {"accepted": False, "retcode": "POSITION_NOT_FOUND",
                    "message": "no such position"}
        volume = float(payload.get("volume") or position["volume"])
        volume = min(volume, position["volume"])
        price = self._price(position["symbol"])
        close = price["bid"] if position["side"] == "buy" else price["ask"]
        profit = self._profit(position) * (volume / position["volume"])
        self.balance += profit
        deal_id = self._next_ticket()
        self.deals.append({
            "deal_id": deal_id, "position_id": pid,
            "symbol": position["symbol"],
            "side": "sell" if position["side"] == "buy" else "buy",
            "volume": volume, "price": close, "profit": round(profit, 2),
            "commission": 0.0, "swap": 0.0,
            "comment": payload.get("comment") or "close", "time": iso_now()})
        if volume >= position["volume"]:
            self.positions.pop(pid, None)
        else:
            position["volume"] = round(position["volume"] - volume, 8)
        return {"accepted": True, "retcode": "DONE",
                "message": "Position closed", "position_id": pid,
                "deal_id": deal_id, "price": close, "volume": volume}

    def _cancel(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        oid = str(payload.get("order_id"))
        order = self.orders.pop(oid, None)
        if order is None:
            return {"accepted": False, "retcode": "ORDER_NOT_FOUND",
                    "message": "no such order"}
        return {"accepted": True, "retcode": "DONE",
                "message": "Order cancelled", "order_id": oid,
                "price": order["price"], "volume": order["volume"]}

    # ---- loop --------------------------------------------------------
    def run(self) -> None:
        last_push = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            pushed = False
            if now - last_push >= 1.0:
                self.push_state()
                pushed = True
                last_push = now
            executed = False
            for command in self.pull_commands():
                result = self.execute(command)
                self.post_result(command["command_id"], result)
                executed = True
            # A real EA pushes immediately after a fill so REST/WS readers
            # see the new position without waiting for the next interval.
            if executed and not pushed:
                self.push_state()
                last_push = time.monotonic()
            self.stop.wait(0.2)

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, daemon=True,
                                       name="fake-mt5-terminal")
        self.thread.start()

    def shutdown(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)


# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv(
        "ABUTRON_GATEWAY_URL", "http://127.0.0.1:19744"))
    parser.add_argument("--api-key", default=os.getenv(
        "ABUTRON_GATEWAY_KEY", ""))
    parser.add_argument("--demo", action="store_true",
                        help="provision a bridge account and round-trip it")
    parser.add_argument("--account", default="")
    parser.add_argument("--token", default="")
    args = parser.parse_args()
    if not args.api_key:
        parser.error("set --api-key or ABUTRON_GATEWAY_KEY")

    http = httpx.Client(base_url=args.base_url.rstrip("/"), timeout=30.0,
                        headers={"Authorization": f"Bearer {args.api_key}"})

    if args.account and args.token:
        account = http.get(f"/v1/accounts/{args.account}").json()
        account["account_id"] = args.account
        account["bridge"] = {
            "state_url": f"/internal/bridge/{args.account}/state",
            "commands_url": f"/internal/bridge/{args.account}/commands",
            "result_url_template":
                f"/internal/bridge/{args.account}/commands/{{command_id}}/result",
        }
        token = args.token
    else:
        print("1. provision a bridge-backed account")
        response = http.post("/v1/accounts", json={
            "login": 910001, "server": "Local-Bridge-Demo", "broker": "local",
            "name": "Bridge terminal demo", "market_data_mode": "bridge",
            "control_mode": "api", "symbols": ["EURUSD", "XAUUSD"],
            "max_lot": 5.0, "max_open_positions": 10})
        response.raise_for_status()
        account = response.json()
        token = account["bridge"]["token"]
        print(f"   {account['account_id']} status={account['status']} "
              f"mode={account['market_data_mode']}")

    account_id = account["account_id"]
    terminal = FakeTerminal(args.base_url, account, token)
    terminal.start()
    print("2. fake terminal online (state push + command pull loop)")

    deadline = time.time() + 8
    while time.time() < deadline:
        info = http.get(f"/v1/accounts/{account_id}/information").json()
        if info.get("connected"):
            break
        time.sleep(0.2)
    print(f"   gateway sees: connected={info['connected']} "
          f"balance={info['balance']}")

    price = http.get(
        f"/v1/accounts/{account_id}/symbols/EURUSD/price").json()
    print(f"3. quote EURUSD bid/ask = {price['bid']}/{price['ask']}")

    print("4. public API market order → queued command → terminal executes")
    trade = http.post(f"/v1/accounts/{account_id}/trade", json={
        "side": "buy", "symbol": "EURUSD", "volume": 0.10,
        "stop_loss": round(price["bid"] * 0.99, 5),
        "take_profit": round(price["ask"] * 1.01, 5),
        "comment": "bridge-demo"})
    trade.raise_for_status()
    body = trade.json()
    print(f"   {body['status']} position={body.get('position_id')} "
          f"retcode={body['retcode']}")
    if body["status"] == "QUEUED":
        cid = body["command_id"]
        for _ in range(50):
            polled = http.get(
                f"/v1/accounts/{account_id}/commands/{cid}").json()
            if polled["status"] in ("DONE", "REJECTED", "TIMEOUT"):
                print(f"   command {polled['status']}: "
                      f"{polled['result'].get('retcode')}")
                break
            time.sleep(0.2)

    # The terminal's state push is asynchronous; poll briefly like a real
    # client should rather than assuming read-after-write consistency.
    positions = []
    for _ in range(30):
        positions = http.get(
            f"/v1/accounts/{account_id}/positions").json()
        if positions:
            break
        time.sleep(0.1)
    print(f"5. positions visible over REST: {len(positions)}")
    if positions:
        pid = positions[0]["position_id"]
        http.post(f"/v1/accounts/{account_id}/positions/{pid}/close",
                  json={"volume": 0.05}).raise_for_status()
        http.post(f"/v1/accounts/{account_id}/positions/{pid}/close",
                  json={}).raise_for_status()
        print("6. closed half, then the remainder")

    deals = []
    for _ in range(30):
        deals = http.get(
            f"/v1/accounts/{account_id}/history/deals").json()
        if len(deals) >= 2:
            break
        time.sleep(0.1)
    print(f"7. deal history ({len(deals)} deals):")
    for deal in deals[:4]:
        print(f"   - {deal['symbol']} {deal['side']} {deal['volume']} "
              f"@ {deal['price']} pnl={deal['profit']} {deal['comment']}")

    terminal.shutdown()
    print(f"\nDone. terminal errors={terminal.errors}. "
          f"No MetaApi, no MetaTrader5 package, no real terminal.")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print(f"HTTP {exc.response.status_code}: {exc.response.text}",
              file=sys.stderr)
        sys.exit(1)
