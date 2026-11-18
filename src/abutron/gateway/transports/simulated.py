"""Deterministic in-process MT5 simulator.

Why this exists: an API bridge is only as good as the ability to exercise
it. This transport implements the full contract — quotes, candles,
positions, pending orders, modifications, partial closes, deal history —
against a behaviourally plausible market, with no Wine, no terminal and no
broker credentials. It is what the demo/live preview runs on, and what the
test suite asserts against.

State can be dumped and rehydrated so a gateway restart does not silently
reset a demo portfolio.
"""
from __future__ import annotations

import math
import random
import threading
import time
import zlib
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..settings import GatewaySettings
from .base import (AccountInfo, Candle, Deal, Order, Position, Price,
                   SymbolSpec, TransportError, as_float, as_int,
                   iso_from_epoch, iso_now, normalize_timeframe,
                   normalize_volume, resolve_symbol, timeframe_seconds)


# base price, digits, contract size, description, spread in points
_BASE_META: Dict[str, Tuple[float, int, float, str, float]] = {
    "EURUSD": (1.0850, 5, 100_000, "Euro vs US Dollar", 1.2),
    "GBPUSD": (1.2650, 5, 100_000, "Great Britain Pound vs US Dollar", 1.6),
    "USDJPY": (149.50, 3, 100_000, "US Dollar vs Japanese Yen", 1.4),
    "AUDUSD": (0.6550, 5, 100_000, "Australian Dollar vs US Dollar", 1.5),
    "USDCAD": (1.3650, 5, 100_000, "US Dollar vs Canadian Dollar", 1.8),
    "USDCHF": (0.8850, 5, 100_000, "US Dollar vs Swiss Franc", 1.7),
    "EURGBP": (0.8580, 5, 100_000, "Euro vs Great Britain Pound", 2.0),
    "XAUUSD": (2350.00, 2, 100, "Gold vs US Dollar", 35),
    "XAGUSD": (28.50, 3, 5_000, "Silver vs US Dollar", 28),
    "BTCUSD": (65_000.0, 2, 1, "Bitcoin vs US Dollar", 4_500),
    "ETHUSD": (3_200.0, 2, 10, "Ethereum vs US Dollar", 320),
    "US500": (5_500.0, 2, 1, "S&P 500 index", 45),
    "NAS100": (19_500.0, 2, 1, "Nasdaq 100 index", 120),
    "US30": (39_500.0, 2, 1, "Dow Jones 30 index", 180),
    "USOIL": (78.50, 2, 1_000, "WTI Crude Oil", 3.2),
}

_DEFAULT_BASE = (1.0000, 5, 100_000, "Synthetic instrument", 2.0)


def _base_for(symbol: str, suffix_hints) -> Tuple[float, int, float, str, float]:
    upper = symbol.upper()
    if upper in _BASE_META:
        return _BASE_META[upper]
    for suffix in suffix_hints:
        if upper.endswith(suffix.upper()):
            stripped = upper[:-len(suffix)]
            if stripped in _BASE_META:
                return _BASE_META[stripped]
    return _DEFAULT_BASE


class SimulatedTransport:
    """One simulated MT5 account."""

    kind = "simulated"

    def __init__(self, account: Dict[str, Any], settings: GatewaySettings,
                 sink: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.account = account
        self.connection_id = account["connection_id"]
        self.settings = settings
        self._sink = sink
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._rng = random.Random(
            zlib.crc32(self.connection_id.encode("utf-8")))
        self._suffix_hints = settings.symbol_suffix_hints

        requested = list(account.get("symbols") or settings.default_symbols)
        self._specs: Dict[str, SymbolSpec] = {}
        self._base: Dict[str, float] = {}
        self._mid: Dict[str, float] = {}
        self._spread: Dict[str, float] = {}
        for symbol in requested:
            self._register_symbol(symbol)

        self._balance: float = float(account.get("balance")
                                     or settings.demo_balance)
        self._leverage: int = 100
        self._currency = "USD"
        self._positions: Dict[str, Position] = {}
        self._orders: Dict[str, Order] = {}
        self._deals: List[Deal] = []
        self._history_orders: List[Order] = []
        self._seq = 0
        self._id_counter = 0
        self._executed: Dict[str, Dict[str, Any]] = {}
        self._started = time.time()
        self._thread = threading.Thread(target=self._tick_loop,
                                       name=f"sim-{self.connection_id[:8]}",
                                       daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------
    # Symbols / prices
    # ------------------------------------------------------------------
    def _register_symbol(self, symbol: str) -> None:
        base, digits, contract, description, spread_points = _base_for(
            symbol, self._suffix_hints)
        point = 10 ** (-digits)
        self._specs[symbol] = SymbolSpec(
            symbol=symbol, description=description, digits=digits,
            point=point, contract_size=contract, min_volume=0.01,
            max_volume=100.0, volume_step=0.01,
            spread=round(spread_points * point, digits + 2))
        self._base[symbol] = base
        self._spread[symbol] = spread_points * point
        # Small deterministic offset so two demo accounts are not identical.
        offset = (self._rng.random() - 0.5) * 0.0008
        self._mid[symbol] = base * (1 + offset)

    def _resolve(self, symbol: str) -> str:
        resolved = resolve_symbol(symbol, list(self._specs),
                                  self._suffix_hints)
        if resolved is None:
            raise TransportError("SYMBOL_NOT_FOUND",
                                 f"symbol {symbol} is not configured on this "
                                 f"account")
        return resolved

    def _quote(self, symbol: str) -> Tuple[float, float]:
        mid = self._mid[symbol]
        half = self._spread[symbol] / 2.0
        digits = self._specs[symbol].digits
        return round(mid - half, digits), round(mid + half, digits)

    def _tick_loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                with self._lock:
                    for symbol in list(self._mid):
                        base = self._base[symbol]
                        mid = self._mid[symbol]
                        # mean-reverting random walk around the base price
                        shock = self._rng.gauss(0.0, base * 0.00012)
                        pull = (base - mid) * 0.02
                        mid = max(base * 0.90, min(base * 1.10,
                                                   mid + shock + pull))
                        self._mid[symbol] = mid
                    quotes = [self.price(s).to_dict()
                              for s in list(self._mid)]
                for quote in quotes:
                    self._emit("quote", quote)
            except Exception:  # never let the simulator thread die
                continue

    # ------------------------------------------------------------------
    # Contract: metadata
    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return not self._stop.is_set()

    def set_event_sink(self, sink: Callable[[Dict[str, Any]], None]) -> None:
        self._sink = sink

    def _emit(self, event_type: str, data: Dict[str, Any]) -> None:
        sink = self._sink
        if sink is None:
            return
        self._seq += 1
        try:
            sink({"type": event_type, "account_id": self.connection_id,
                  "sequence": self._seq, "timestamp": iso_now(),
                  "data": data})
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Contract: account & market data
    # ------------------------------------------------------------------
    def account_information(self) -> AccountInfo:
        with self._lock:
            equity = self._equity()
            margin = self._margin()
            free = equity - margin
            level = (equity / margin * 100.0) if margin > 0 else 0.0
            return AccountInfo(
                login=as_int(self.account.get("login")),
                broker=self.account.get("broker", ""),
                server=self.account.get("server", ""),
                name=self.account.get("name", ""),
                currency=self._currency, leverage=self._leverage,
                balance=round(self._balance, 2), equity=round(equity, 2),
                margin=round(margin, 2), free_margin=round(free, 2),
                margin_level=round(level, 2), trade_allowed=True,
                connected=True,
                environment=self.account.get("environment", "demo"),
                updated_at=iso_now(), stale=False)

    def symbols(self) -> List[SymbolSpec]:
        with self._lock:
            return [self._specs[s] for s in self._specs]

    def symbol(self, symbol: str) -> SymbolSpec:
        with self._lock:
            return self._specs[self._resolve(symbol)]

    def price(self, symbol: str) -> Price:
        with self._lock:
            resolved = self._resolve(symbol)
            bid, ask = self._quote(resolved)
            return Price(symbol=resolved, bid=bid, ask=ask,
                         spread=round(ask - bid, 8), time=iso_now())

    def candles(self, symbol: str, timeframe: str,
                start_time: Optional[str], limit: int) -> List[Candle]:
        tf = normalize_timeframe(timeframe)
        seconds = timeframe_seconds(tf)
        limit = max(1, min(limit, 1000))
        with self._lock:
            resolved = self._resolve(symbol)
            spec = self._specs[resolved]
            mid = self._mid[resolved]
        now = time.time()
        end_bar = int(now // seconds) * seconds
        start_epoch = 0
        if start_time:
            from .base import epoch_from_iso
            start_epoch = epoch_from_iso(start_time, 0.0)
        if start_epoch:
            available = int((end_bar - start_epoch) // seconds) + 1
            count = max(1, min(limit, available))
        else:
            count = limit

        out: List[Candle] = []
        for i in range(count):
            open_epoch = end_bar - (count - 1 - i) * seconds
            idx = open_epoch // seconds
            o, h, l, c, v = self._synth_bar(resolved, seconds, idx, spec)
            if open_epoch == end_bar:
                c = round(mid, spec.digits)
                h = max(h, c)
                l = min(l, c)
            out.append(Candle(
                symbol=resolved, timeframe=tf,
                open_time=iso_from_epoch(open_epoch),
                close_time=iso_from_epoch(open_epoch + seconds),
                open=o, high=h, low=l, close=c, volume=round(v, 2),
                closed=open_epoch < end_bar))
        return out

    def _synth_bar(self, symbol: str, seconds: int, idx: int,
                   spec: SymbolSpec) -> Tuple[float, float, float, float, float]:
        base = self._base[symbol]
        seed = (zlib.crc32(symbol.upper().encode("utf-8")) * 1_000_003
                + seconds * 7919 + (idx % 10_000_019))
        rng = random.Random(seed)

        def close_at(i: int) -> float:
            phase = i * seconds
            h = zlib.crc32(symbol.upper().encode("utf-8")) % 100
            wave = (0.0060 * math.sin(phase / 52_000.0 + h)
                    + 0.0022 * math.sin(phase / 8_900.0 + h / 3)
                    + 0.0009 * math.sin(phase / 1_700.0 + h / 7))
            local = random.Random(seed - 1).uniform(-0.0009, 0.0009)
            return base * (1.0 + wave + local)

        open_ = close_at(idx - 1)
        close = close_at(idx)
        hi = max(open_, close) * (1 + abs(rng.uniform(0, 0.0007)))
        lo = min(open_, close) * (1 - abs(rng.uniform(0, 0.0007)))
        volume = rng.uniform(60, 1200)
        return (round(open_, spec.digits), round(hi, spec.digits),
                round(lo, spec.digits), round(close, spec.digits), volume)

    # ------------------------------------------------------------------
    # Contract: terminal state
    # ------------------------------------------------------------------
    def positions(self) -> List[Position]:
        with self._lock:
            out = []
            for pos in self._positions.values():
                mid = self._mid.get(pos.symbol, pos.open_price)
                profit = self._position_profit(pos, mid)
                digits = self._specs[pos.symbol].digits
                out.append(Position(
                    position_id=pos.position_id, symbol=pos.symbol,
                    side=pos.side, volume=pos.volume,
                    open_price=pos.open_price,
                    current_price=round(mid, digits),
                    stop_loss=pos.stop_loss, take_profit=pos.take_profit,
                    profit=round(profit - pos.commission, 2),
                    swap=pos.swap, commission=pos.commission,
                    comment=pos.comment, magic=pos.magic,
                    opened_at=pos.opened_at))
            return sorted(out, key=lambda p: p.opened_at)

    def orders(self) -> List[Order]:
        with self._lock:
            out = []
            for order in self._orders.values():
                mid = self._mid.get(order.symbol, order.price)
                digits = self._specs[order.symbol].digits
                out.append(Order(
                    order_id=order.order_id, symbol=order.symbol,
                    side=order.side, type=order.type, volume=order.volume,
                    price=order.price,
                    current_price=round(mid, digits),
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit, state=order.state,
                    comment=order.comment, created_at=order.created_at))
            return out

    def deals(self, start_time: Optional[str], end_time: Optional[str],
              limit: int) -> List[Deal]:
        from .base import epoch_from_iso
        start = epoch_from_iso(start_time, 0.0)
        end = epoch_from_iso(end_time, float("inf"))
        with self._lock:
            rows = [d for d in self._deals
                    if start <= epoch_from_iso(d.time, 0.0) <= end]
        rows.sort(key=lambda d: d.time, reverse=True)
        return rows[:max(1, min(limit, 1000))]

    def history_orders(self, start_time: Optional[str],
                       end_time: Optional[str], limit: int) -> List[Order]:
        from .base import epoch_from_iso
        start = epoch_from_iso(start_time, 0.0)
        end = epoch_from_iso(end_time, float("inf"))
        with self._lock:
            rows = [o for o in self._history_orders
                    if start <= epoch_from_iso(o.created_at, 0.0) <= end]
        rows.sort(key=lambda o: o.created_at, reverse=True)
        return rows[:max(1, min(limit, 1000))]

    # ------------------------------------------------------------------
    # Contract: execution
    # ------------------------------------------------------------------
    def execute(self, kind: str, payload: Dict[str, Any],
                command_id: str) -> Dict[str, Any]:
        with self._lock:
            cached = self._executed.get(command_id)
            if cached is not None:
                return dict(cached)
            handler = {
                "market": self._market,
                "pending": self._pending,
                "modify": self._modify,
                "close": self._close,
                "cancel": self._cancel,
            }.get(kind)
            if handler is None:
                return self._reject("UNKNOWN_COMMAND",
                                    f"unsupported command kind: {kind}")
            result = handler(payload, command_id)
            self._executed[command_id] = dict(result)
            if len(self._executed) > 2000:
                self._executed.pop(next(iter(self._executed)))
            return result

    def _market(self, payload: Dict[str, Any], command_id: str) -> Dict[str, Any]:
        try:
            symbol = self._resolve(str(payload.get("symbol", "")))
        except TransportError as exc:
            return self._reject(exc.code, exc.message)
        spec = self._specs[symbol]
        side = str(payload.get("side", "")).lower()
        volume = normalize_volume(as_float(payload.get("volume")), spec)
        if volume is None:
            return self._reject("INVALID_VOLUME",
                                f"volume must be between {spec.min_volume} "
                                f"and {spec.max_volume}")
        bid, ask = self._quote(symbol)
        price = ask if side == "buy" else bid
        sl = as_float(payload.get("stop_loss"))
        tp = as_float(payload.get("take_profit"))
        bad = self._check_stops(side, price, sl, tp, spec.digits)
        if bad:
            return self._reject("INVALID_STOPS", bad)

        position_id = str(self._next_id())
        commission = round(volume * 3.5, 2)
        position = Position(
            position_id=position_id, symbol=symbol, side=side,
            volume=volume, open_price=round(price, spec.digits),
            current_price=round(price, spec.digits), stop_loss=sl,
            take_profit=tp, profit=-commission, commission=commission,
            comment=str(payload.get("comment", ""))[:64],
            magic=as_int(payload.get("magic")), opened_at=iso_now())
        self._positions[position_id] = position
        self._deals.append(Deal(
            deal_id=str(self._next_id()), position_id=position_id,
            symbol=symbol, side=side, volume=volume,
            price=position.open_price, profit=0.0, commission=commission,
            comment="entry", time=iso_now()))
        self._emit("position", {"action": "opened",
                                "position": position.to_dict()})
        self._emit("account", self.account_information().to_dict())
        return {"accepted": True, "status": "EXECUTED", "retcode": "DONE",
                "message": "Order executed",
                "order_id": position_id, "position_id": position_id,
                "deal_id": self._deals[-1].deal_id,
                "price": position.open_price, "volume": volume}

    def _pending(self, payload: Dict[str, Any], command_id: str) -> Dict[str, Any]:
        try:
            symbol = self._resolve(str(payload.get("symbol", "")))
        except TransportError as exc:
            return self._reject(exc.code, exc.message)
        spec = self._specs[symbol]
        side = str(payload.get("side", "")).lower()
        volume = normalize_volume(as_float(payload.get("volume")), spec)
        if volume is None:
            return self._reject("INVALID_VOLUME", "volume outside limits")
        price = as_float(payload.get("price"))
        order_type = str(payload.get("order_type") or "limit").lower()
        if price <= 0:
            return self._reject("INVALID_PRICE", "pending order needs a price")
        bid, ask = self._quote(symbol)
        if order_type == "limit":
            if side == "buy" and price > ask:
                return self._reject("INVALID_PRICE",
                                    "buy limit must be below the ask")
            if side == "sell" and price < bid:
                return self._reject("INVALID_PRICE",
                                    "sell limit must be above the bid")
        elif order_type == "stop":
            if side == "buy" and price < ask:
                return self._reject("INVALID_PRICE",
                                    "buy stop must be above the ask")
            if side == "sell" and price > bid:
                return self._reject("INVALID_PRICE",
                                    "sell stop must be below the bid")
        else:
            return self._reject("INVALID_ORDER_TYPE",
                                "order_type must be limit or stop")
        sl = as_float(payload.get("stop_loss"))
        tp = as_float(payload.get("take_profit"))
        order = Order(
            order_id=str(self._next_id()), symbol=symbol, side=side,
            type=order_type, volume=volume, price=round(price, spec.digits),
            current_price=round((bid + ask) / 2, spec.digits),
            stop_loss=sl, take_profit=tp,
            comment=str(payload.get("comment", ""))[:64], created_at=iso_now())
        self._orders[order.order_id] = order
        self._emit("order", {"action": "placed", "order": order.to_dict()})
        return {"accepted": True, "status": "EXECUTED", "retcode": "DONE",
                "message": "Pending order placed",
                "order_id": order.order_id, "position_id": None,
                "deal_id": None, "price": order.price, "volume": volume}

    def _modify(self, payload: Dict[str, Any], command_id: str) -> Dict[str, Any]:
        position = self._positions.get(str(payload.get("position_id", "")))
        if position is None:
            return self._reject("POSITION_NOT_FOUND", "position not found")
        spec = self._specs[position.symbol]
        mid = self._mid[position.symbol]
        sl = payload.get("stop_loss")
        tp = payload.get("take_profit")
        new_sl = position.stop_loss if sl is None else as_float(sl)
        new_tp = position.take_profit if tp is None else as_float(tp)
        bad = self._check_stops(position.side, mid, new_sl, new_tp,
                                spec.digits)
        if bad:
            return self._reject("INVALID_STOPS", bad)
        position.stop_loss = new_sl
        position.take_profit = new_tp
        self._emit("position", {"action": "modified",
                                "position": position.to_dict()})
        return {"accepted": True, "status": "EXECUTED", "retcode": "DONE",
                "message": "Position modified",
                "position_id": position.position_id,
                "price": round(mid, spec.digits),
                "volume": position.volume}

    def _close(self, payload: Dict[str, Any], command_id: str) -> Dict[str, Any]:
        position = self._positions.get(str(payload.get("position_id", "")))
        if position is None:
            return self._reject("POSITION_NOT_FOUND", "position not found")
        spec = self._specs[position.symbol]
        volume = as_float(payload.get("volume"), position.volume)
        volume = min(volume, position.volume)
        volume = normalize_volume(volume, spec) or position.volume
        bid, ask = self._quote(position.symbol)
        close_price = bid if position.side == "buy" else ask
        profit = self._position_profit(position, close_price) * (
            volume / position.volume)
        commission_share = round(
            position.commission * (volume / position.volume), 2)
        self._balance += profit - commission_share
        deal = Deal(
            deal_id=str(self._next_id()), position_id=position.position_id,
            symbol=position.symbol,
            side="sell" if position.side == "buy" else "buy",
            volume=volume, price=round(close_price, spec.digits),
            profit=round(profit, 2), commission=commission_share,
            comment=str(payload.get("comment", "close"))[:64],
            time=iso_now())
        self._deals.append(deal)
        if volume >= position.volume:
            self._positions.pop(position.position_id, None)
            action = "closed"
        else:
            position.volume = round(position.volume - volume, 8)
            position.commission = round(
                position.commission - commission_share, 2)
            action = "partially_closed"
        self._emit("trade", {"action": action, "deal": deal.to_dict()})
        self._emit("account", self.account_information().to_dict())
        return {"accepted": True, "status": "EXECUTED", "retcode": "DONE",
                "message": "Position closed", "deal_id": deal.deal_id,
                "position_id": position.position_id,
                "price": deal.price, "volume": volume}

    def _cancel(self, payload: Dict[str, Any], command_id: str) -> Dict[str, Any]:
        order = self._orders.pop(str(payload.get("order_id", "")), None)
        if order is None:
            return self._reject("ORDER_NOT_FOUND", "order not found")
        order.state = "cancelled"
        self._history_orders.append(order)
        self._emit("order", {"action": "cancelled", "order": order.to_dict()})
        return {"accepted": True, "status": "EXECUTED", "retcode": "DONE",
                "message": "Order cancelled", "order_id": order.order_id,
                "price": order.price, "volume": order.volume}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _next_id(self) -> int:
        self._id_counter += 1
        return int(time.time() * 1000) + self._id_counter

    def _reject(self, retcode: str, message: str) -> Dict[str, Any]:
        return {"accepted": False, "status": "REJECTED", "retcode": retcode,
                "message": message, "order_id": None, "position_id": None,
                "deal_id": None, "price": None, "volume": None}

    def _check_stops(self, side: str, price: float, sl: float, tp: float,
                     digits: int) -> Optional[str]:
        if sl:
            if side == "buy" and sl >= price:
                return "stop loss must be below the entry price for a buy"
            if side == "sell" and sl <= price:
                return "stop loss must be above the entry price for a sell"
        if tp:
            if side == "buy" and tp <= price:
                return "take profit must be above the entry price for a buy"
            if side == "sell" and tp >= price:
                return "take profit must be below the entry price for a sell"
        return None

    def _position_profit(self, position: Position, price: float) -> float:
        spec = self._specs[position.symbol]
        direction = 1.0 if position.side == "buy" else -1.0
        return direction * (price - position.open_price) * \
            position.volume * spec.contract_size

    def _equity(self) -> float:
        total = self._balance
        for pos in self._positions.values():
            total += self._position_profit(pos, self._mid[pos.symbol])
            total -= pos.commission
        return total

    def _margin(self) -> float:
        total = 0.0
        for pos in self._positions.values():
            spec = self._specs[pos.symbol]
            total += (pos.volume * spec.contract_size *
                      self._mid[pos.symbol]) / float(self._leverage)
        return total

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def dump_state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "mode": "simulated",
                "balance": self._balance,
                "positions": [p.to_dict() for p in self._positions.values()],
                "orders": [o.to_dict() for o in self._orders.values()],
                "deals": [d.to_dict() for d in self._deals[-1000:]],
                "history_orders": [o.to_dict()
                                   for o in self._history_orders[-500:]],
            }

    def load_state(self, state: Dict[str, Any]) -> None:
        if not state or state.get("mode") != "simulated":
            return
        with self._lock:
            self._balance = as_float(state.get("balance"), self._balance)
            for row in state.get("positions", []):
                pos = Position(**{k: row[k] for k in row
                                  if k in Position.__dataclass_fields__})
                if pos.symbol in self._specs:
                    self._positions[pos.position_id] = pos
            for row in state.get("orders", []):
                order = Order(**{k: row[k] for k in row
                                 if k in Order.__dataclass_fields__})
                if order.symbol in self._specs:
                    self._orders[order.order_id] = order
            for row in state.get("deals", []):
                self._deals.append(Deal(
                    **{k: row[k] for k in row
                       if k in Deal.__dataclass_fields__}))
            for row in state.get("history_orders", []):
                self._history_orders.append(Order(
                    **{k: row[k] for k in row
                       if k in Order.__dataclass_fields__}))

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
