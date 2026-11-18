"""Hosted-account transport backed by a pull/push MQL5 bridge EA.

Wire model (deliberately NAT-friendly — the terminal makes only outbound
HTTP calls, so a client never opens an inbound port):

    EA  --POST /internal/bridge/{id}/state-->  gateway   (snapshot)
    EA  --GET  /internal/bridge/{id}/commands?wait=15-->  gateway
    EA  --POST /internal/bridge/{id}/commands/{cid}/result--> gateway

The public API enqueues a trade command, then waits up to
``command_timeout_s`` for the bridge to report a result. If the bridge is
slower, the API returns 202 with a command id to poll — an honest answer
instead of a fabricated ACK.

Everything this transport knows is the last snapshot the bridge pushed
(plus its own command queue in SQLite), so it stays correct across
gateway restarts.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

from ..settings import GatewaySettings
from ..store import GatewayStore
from .base import (AccountInfo, Candle, Deal, Order, Position, Price,
                   SymbolSpec, TransportError, as_float, as_int,
                   epoch_from_iso, iso_now, normalize_timeframe,
                   resolve_symbol)


def _filter_fields(cls, row: Dict[str, Any]) -> Dict[str, Any]:
    fields = cls.__dataclass_fields__  # type: ignore[attr-defined]
    return {k: v for k, v in row.items() if k in fields}


class BridgeTransport:
    """One account whose MT5 terminal is reached through a bridge EA."""

    kind = "bridge"

    def __init__(self, account: Dict[str, Any], store: GatewayStore,
                 settings: GatewaySettings,
                 sink: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.account = account
        self.connection_id = account["connection_id"]
        self.store = store
        self.settings = settings
        self._sink = sink
        self._last_seen_monotonic: float = 0.0
        self._state: Dict[str, Any] = {}
        self._executed: Dict[str, Dict[str, Any]] = {}
        try:
            self._state = json.loads(account.get("state_json") or "{}")
        except (ValueError, TypeError):
            self._state = {}

    # ------------------------------------------------------------------
    # Snapshot intake
    # ------------------------------------------------------------------
    def apply_state(self, state: Dict[str, Any]) -> None:
        if not isinstance(state, dict):
            raise TransportError("INVALID_STATE", "bridge state must be an object")
        merged = dict(self._state)
        merged.update(state)
        self._state = merged
        self._last_seen_monotonic = time.monotonic()

    def snapshot(self) -> Dict[str, Any]:
        return self._state

    @property
    def connected(self) -> bool:
        if not self._state:
            return False
        account = self._state.get("account") or {}
        if account.get("connected") is False:
            return False
        if self._last_seen_monotonic <= 0:
            # Restored from disk: fresh enough only if last_seen_at is recent.
            last = epoch_from_iso(self.account.get("last_seen_at"), 0.0)
            return (time.time() - last) <= self.settings.bridge_stale_after_s
        return (time.monotonic() - self._last_seen_monotonic) <= \
            self.settings.bridge_stale_after_s

    def set_event_sink(self, sink: Callable[[Dict[str, Any]], None]) -> None:
        self._sink = sink

    # ------------------------------------------------------------------
    # Reads (served from the last snapshot)
    # ------------------------------------------------------------------
    def account_information(self) -> AccountInfo:
        raw = dict(self._state.get("account") or {})
        connected = self.connected
        return AccountInfo(
            login=as_int(raw.get("login"), as_int(self.account.get("login"))),
            broker=str(raw.get("broker") or self.account.get("broker", "")),
            server=str(raw.get("server") or self.account.get("server", "")),
            name=str(raw.get("name") or self.account.get("name", "")),
            currency=str(raw.get("currency") or "USD"),
            leverage=as_int(raw.get("leverage")),
            balance=as_float(raw.get("balance")),
            equity=as_float(raw.get("equity")),
            margin=as_float(raw.get("margin")),
            free_margin=as_float(raw.get("free_margin")),
            margin_level=as_float(raw.get("margin_level")),
            trade_allowed=bool(raw.get("trade_allowed", connected)),
            connected=connected,
            environment=str(raw.get("environment")
                            or self.account.get("environment", "demo")),
            updated_at=str(raw.get("updated_at")
                           or self.account.get("last_seen_at") or iso_now()),
            stale=not connected)

    def symbols(self) -> List[SymbolSpec]:
        rows = self._state.get("symbols") or []
        out: List[SymbolSpec] = []
        for row in rows:
            if isinstance(row, str):
                out.append(self._spec_from_name(row))
            elif isinstance(row, dict) and row.get("symbol"):
                base = self._spec_from_name(str(row["symbol"]))
                out.append(SymbolSpec(**_filter_fields(SymbolSpec, {
                    **base.__dict__, **row})))
        if not out:
            for name in (self.account.get("symbols") or []):
                out.append(self._spec_from_name(name))
        return out

    def _spec_from_name(self, symbol: str) -> SymbolSpec:
        # Conservative defaults until the EA reports real contract specs.
        upper = symbol.upper()
        digits = 3 if upper.endswith("JPY") else 5
        if upper.startswith(("XAU", "XAG")):
            digits = 2
        if upper.startswith(("BTC", "ETH", "US500", "NAS", "US30", "USOIL")):
            digits = 2
        return SymbolSpec(
            symbol=symbol, description=symbol, digits=digits,
            point=10 ** (-digits),
            contract_size=100_000 if digits in (3, 5) else 100,
            min_volume=0.01, max_volume=100.0, volume_step=0.01)

    def symbol(self, symbol: str) -> SymbolSpec:
        specs = self.symbols()
        resolved = resolve_symbol(symbol, [s.symbol for s in specs],
                                  self.settings.symbol_suffix_hints)
        if resolved is None:
            raise TransportError("SYMBOL_NOT_FOUND",
                                 f"symbol {symbol} is not configured")
        return next(s for s in specs if s.symbol == resolved)

    def price(self, symbol: str) -> Price:
        spec = self.symbol(symbol)
        prices = self._state.get("prices") or {}
        row = prices.get(spec.symbol) or prices.get(spec.symbol.upper())
        if row is None:
            raise TransportError(
                "PRICE_UNAVAILABLE",
                f"no quote for {spec.symbol} in the last bridge snapshot")
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            bid, ask = as_float(row[0]), as_float(row[1])
            when = iso_now()
        else:
            bid = as_float(row.get("bid"))
            ask = as_float(row.get("ask"))
            when = str(row.get("time") or iso_now())
        return Price(symbol=spec.symbol, bid=bid, ask=ask,
                     spread=round(ask - bid, 8), time=when)

    def candles(self, symbol: str, timeframe: str,
                start_time: Optional[str], limit: int) -> List[Candle]:
        tf = normalize_timeframe(timeframe)
        spec = self.symbol(symbol)
        start_epoch = epoch_from_iso(start_time, 0.0)
        out: List[Candle] = []
        for row in (self._state.get("candles") or []):
            if str(row.get("symbol", "")).upper() != spec.symbol.upper():
                continue
            if normalize_timeframe(str(row.get("timeframe", "5m"))) != tf:
                continue
            if start_epoch and epoch_from_iso(str(row.get("open_time")), 0.0) < start_epoch:
                continue
            try:
                out.append(Candle(**_filter_fields(Candle, row)))
            except TypeError:
                continue
        out.sort(key=lambda c: c.open_time)
        return out[-max(1, min(limit, 1000)):]

    def positions(self) -> List[Position]:
        rows = self._state.get("positions") or []
        out = []
        for row in rows:
            try:
                out.append(Position(**_filter_fields(Position, row)))
            except TypeError:
                continue
        return out

    def orders(self) -> List[Order]:
        rows = self._state.get("orders") or []
        out = []
        for row in rows:
            try:
                out.append(Order(**_filter_fields(Order, row)))
            except TypeError:
                continue
        return out

    def deals(self, start_time: Optional[str], end_time: Optional[str],
              limit: int) -> List[Deal]:
        start = epoch_from_iso(start_time, 0.0)
        end = epoch_from_iso(end_time, float("inf"))
        rows = []
        for row in (self._state.get("deals") or []):
            try:
                deal = Deal(**_filter_fields(Deal, row))
            except TypeError:
                continue
            if start <= epoch_from_iso(deal.time, 0.0) <= end:
                rows.append(deal)
        rows.sort(key=lambda d: d.time, reverse=True)
        return rows[:max(1, min(limit, 1000))]

    def history_orders(self, start_time: Optional[str],
                       end_time: Optional[str], limit: int) -> List[Order]:
        start = epoch_from_iso(start_time, 0.0)
        end = epoch_from_iso(end_time, float("inf"))
        rows = []
        for row in (self._state.get("history_orders") or []):
            try:
                order = Order(**_filter_fields(Order, row))
            except TypeError:
                continue
            if start <= epoch_from_iso(order.created_at, 0.0) <= end:
                rows.append(order)
        rows.sort(key=lambda o: o.created_at, reverse=True)
        return rows[:max(1, min(limit, 1000))]

    # ------------------------------------------------------------------
    # Execution: wait for the bridge to answer the queued command
    # ------------------------------------------------------------------
    def execute(self, kind: str, payload: Dict[str, Any],
                command_id: str) -> Dict[str, Any]:
        cached = self._executed.get(command_id)
        if cached is not None:
            return dict(cached)
        timeout = max(0.0, float(self.settings.command_timeout_s))
        deadline = time.monotonic() + timeout
        while True:
            row = self.store.get_command(self.connection_id, command_id)
            if row is None:
                return {"accepted": False, "status": "REJECTED",
                        "retcode": "COMMAND_NOT_FOUND",
                        "message": "command row disappeared"}
            if row["status"] in ("DONE", "REJECTED", "FAILED", "TIMEOUT"):
                result = dict(row["result"])
                accepted = row["status"] == "DONE"
                result["accepted"] = accepted
                if row["status"] == "TIMEOUT":
                    result["status"] = "REJECTED"
                    result.setdefault("retcode", "COMMAND_TIMEOUT")
                    result.setdefault(
                        "message", "the bridge never answered this command")
                else:
                    result.setdefault("status",
                                      "EXECUTED" if accepted else "REJECTED")
                self._executed[command_id] = dict(result)
                return result
            if time.monotonic() >= deadline:
                return {"accepted": False, "status": "QUEUED",
                        "retcode": "PENDING",
                        "message": "bridge has not answered yet"}
            time.sleep(0.1)

    def close(self) -> None:
        return None
