"""Read-only mirror of an account that the APEX engine already trades.

Why this exists
---------------
One MT5 terminal has exactly one writer. Accounts driven by
``ApexNativeWriter.mq5`` (or the platform's terminal supervisor) are
already traded by the engine; letting the API gateway also send orders
would create two writers on the same account. This transport therefore
exposes those accounts **read-only**: positions, deals, order history,
quotes and candles, with trading refused at the transport layer.

Where the data comes from
-------------------------
The platform's native writer publishes two files per cycle inside the
Wine prefix (``MQL5/Files``):

* ``ABUTRON_NATIVE_ACCOUNT_V2_<nonce>.txt`` — identity + health envelope
* ``ABUTRON_NATIVE_BARS_V2_<nonce>.txt`` — ``{"bars": [...]}``

These are the same files :mod:`abutron.feeds.feed_quality` validates, so
the API never invents prices. Portfolio state (open tickets, fills,
realised P&L) comes from the engine database (``order_receipts`` and
``ledger``), which is the engine's own book of record.

Freshness is derived from the export file's mtime, not from a heartbeat
the gateway fabricates. If the file stops advancing, the account reports
``connected=false``/``stale=true`` and the API returns stale data marked
as such.
"""
from __future__ import annotations

import glob
import json
import math
import os
import time
from typing import Any, Dict, List, Optional

from ...feeds.feed_quality import normalize_native_bars
from .base import (
    MT5_TIMEFRAME_ALIASES,
    TIMEFRAME_SECONDS,
    AccountInfo,
    Candle,
    Deal,
    Order,
    Position,
    Price,
    SymbolSpec,
    TransportError,
    TransportUnavailable,
    epoch_from_iso,
    iso_from_epoch,
    iso_now,
    resolve_symbol,
)

# A native export that has not advanced in this many seconds is stale. The
# writer's default cycle is 5s, so this tolerates ~18 missed cycles without
# flapping when the terminal is briefly busy.
EXPORT_STALE_AFTER_S = 90.0
# The export reader touches the disk; don't do it on every websocket frame.
READ_CACHE_S = 2.0


def _timeframe_seconds(tf: str) -> int:
    if not tf:
        return 0
    key = tf.strip()
    if key in TIMEFRAME_SECONDS:
        return TIMEFRAME_SECONDS[key]
    alias = MT5_TIMEFRAME_ALIASES.get(key.upper())
    return TIMEFRAME_SECONDS.get(alias or "", 0)


def _canonical_timeframe(tf: str) -> str:
    key = (tf or "").strip()
    if key in TIMEFRAME_SECONDS:
        return key
    return MT5_TIMEFRAME_ALIASES.get(key.upper(), key.lower() or "5m")


def _contract_size(symbol: str) -> float:
    upper = symbol.upper()
    if upper.startswith(("XAU", "XAG", "XPT", "XPD")):
        return 100.0
    if any(upper.startswith(prefix) for prefix in
           ("US30", "US500", "NAS100", "GER40", "UK100", "JP225")):
        return 1.0
    if upper.startswith(("BTC", "ETH")):
        return 1.0
    return 100000.0


def _guess_digits(price: float) -> int:
    if price <= 0:
        return 5
    if price >= 1000:
        return 2
    if price >= 100:
        return 3
    if price >= 10:
        return 3
    return 5


def export_directory(engine_connection_id: str,
                     mt5_root: Optional[str] = None) -> str:
    """Best-effort path to a connection's ``MQL5/Files`` directory."""
    if mt5_root is None:
        from ... import config
        mt5_root = getattr(config, "MT5_ROOT", "")
    root = mt5_root or ""
    if root:
        candidate = os.path.join(
            root, f"prefix-{engine_connection_id}", "drive_c",
            "Program Files", "MetaTrader 5", "MQL5", "Files")
        if os.path.isdir(candidate):
            return candidate
        # Older prefixes installed the terminal under Program Files (x86).
        candidate = os.path.join(
            root, f"prefix-{engine_connection_id}", "drive_c",
            "Program Files (x86)", "MetaTrader 5", "MQL5", "Files")
        if os.path.isdir(candidate):
            return candidate
    from ... import config
    fallback = getattr(config, "MT5_EXPORT_DIR", "")
    return fallback if fallback and os.path.isdir(fallback) else ""


def _latest(pattern: str) -> Optional[str]:
    matches = glob.glob(pattern)
    if not matches:
        return None
    try:
        return max(matches, key=os.path.getmtime)
    except OSError:
        return matches[-1]


def read_engine_export(engine_connection_id: str,
                       mt5_root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Read the newest native export for an engine connection.

    Returns ``{identity, bars, mtime, path}`` or ``None``. Never raises:
    a broken export must degrade to "no data", not to a 500.
    """
    # Prefer the supervisor's live terminal: it knows the real export dir
    # even when a prefix was created with a non-default layout.
    try:
        from ...feeds.mt5_terminal import terminal_manager
        terminal = terminal_manager.get(engine_connection_id)
        if terminal is not None and terminal.export_dir:
            directory = terminal.export_dir
        else:
            directory = export_directory(engine_connection_id, mt5_root)
    except Exception:
        directory = export_directory(engine_connection_id, mt5_root)
    if not directory:
        return None

    identity_path = _latest(os.path.join(
        directory, "ABUTRON_NATIVE_ACCOUNT_V2_*.txt"))
    bars_path = _latest(os.path.join(
        directory, "ABUTRON_NATIVE_BARS_V2_*.txt"))
    if not identity_path or not bars_path:
        return None
    try:
        with open(identity_path, "r", encoding="utf-8") as fh:
            identity = json.load(fh)
        with open(bars_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        mtime = max(os.path.getmtime(identity_path),
                    os.path.getmtime(bars_path))
    except (OSError, ValueError):
        return None
    if not isinstance(identity, dict):
        return None
    if isinstance(payload, dict):
        bars = payload.get("bars") or []
    elif isinstance(payload, list):
        bars = payload
    else:
        return None
    return {
        "identity": identity,
        "bars": bars,
        "mtime": mtime,
        "path": directory,
    }


class EngineFeedTransport:
    """Read-only transport over an engine-managed MT5 account."""

    kind = "engine"
    is_read_only = True

    def __init__(self, account: Dict[str, Any], *, engine_store=None,
                 export_reader=None, mt5_root: Optional[str] = None,
                 stale_after_s: float = EXPORT_STALE_AFTER_S):
        self.account = account
        self.engine_connection_id = (
            account.get("engine_connection_id") or account["connection_id"])
        self._engine = engine_store
        self._export_reader = export_reader or read_engine_export
        self._mt5_root = mt5_root
        self._stale_after_s = stale_after_s
        self._cache_at = 0.0
        self._cache: Optional[Dict[str, Any]] = None
        self._sink = None

    # ------------------------------------------------------------------
    # Engine access
    # ------------------------------------------------------------------
    def _engine_store(self):
        if self._engine is not None:
            return self._engine
        from ...db import engine_db
        return engine_db

    def _engine_account(self) -> Optional[Dict[str, Any]]:
        try:
            return self._engine_store().get_account(self.engine_connection_id)
        except Exception:
            return None

    def _export(self) -> Optional[Dict[str, Any]]:
        now = time.monotonic()
        if self._cache is not None and now - self._cache_at < READ_CACHE_S:
            return self._cache
        try:
            data = self._export_reader(self.engine_connection_id,
                                       self._mt5_root)
        except Exception:
            data = None
        self._cache = data
        self._cache_at = now
        return data

    # ------------------------------------------------------------------
    # Contract
    # ------------------------------------------------------------------
    def set_event_sink(self, sink) -> None:
        # A read-only mirror never emits execution events. The engine is the
        # only writer and already owns its own event stream.
        self._sink = sink

    @property
    def connected(self) -> bool:
        export = self._export()
        if not export:
            return False
        return (time.time() - float(export.get("mtime") or 0)) <= \
            self._stale_after_s

    def _identity(self) -> Dict[str, Any]:
        export = self._export() or {}
        identity = export.get("identity") or {}
        return identity if isinstance(identity, dict) else {}

    def account_information(self) -> AccountInfo:
        engine = self._engine_account() or {}
        identity = self._identity()
        export = self._export()
        updated_at = iso_now()
        if export and export.get("mtime"):
            updated_at = iso_from_epoch(float(export["mtime"]))
        equity = float(identity.get("equity") or 0) or float(
            engine.get("equity") or 0)
        balance = float(identity.get("balance") or 0) or float(
            engine.get("balance") or 0)
        stale = not self.connected
        # If the export is stale the engine book is only an estimate; keep
        # the value but flag it so clients don't size trades from it.
        return AccountInfo(
            login=int(engine.get("mt5_login") or identity.get("login") or 0),
            broker=str(engine.get("broker") or identity.get("broker") or ""),
            server=str(engine.get("server") or identity.get("server") or ""),
            name=str(engine.get("display_name") or ""),
            currency=str(identity.get("currency") or "USD"),
            leverage=int(identity.get("leverage") or 0),
            balance=round(balance, 2),
            equity=round(equity, 2),
            margin=float(identity.get("margin") or 0),
            free_margin=float(identity.get("free_margin") or 0),
            margin_level=float(identity.get("margin_level") or 0),
            trade_allowed=False,
            connected=not stale,
            environment=str(engine.get("environment") or "demo"),
            updated_at=updated_at,
            stale=stale,
        )

    def _native_candles(self) -> List[Any]:
        export = self._export()
        if not export:
            return []
        engine = self._engine_account() or {}
        try:
            return normalize_native_bars(
                export.get("bars") or [],
                connection_id=self.engine_connection_id,
                login=int(engine.get("mt5_login") or 0),
                broker=str(engine.get("broker") or ""),
            )
        except Exception:
            return []

    def _symbol_names(self) -> List[str]:
        names: List[str] = []
        engine = self._engine_account() or {}
        for raw in str(engine.get("symbols") or "").split(","):
            name = raw.strip()
            if name:
                names.append(name)
        for candle in self._native_candles():
            if candle.symbol and candle.symbol not in names:
                names.append(candle.symbol)
        return names

    def _spec(self, symbol: str) -> SymbolSpec:
        candle = next(
            (c for c in self._native_candles()
             if c.symbol.upper() == symbol.upper()), None)
        point = float(getattr(candle, "point", 0) or 0)
        digits = _guess_digits(float(getattr(candle, "close", 0) or 0))
        if point > 0:
            derived = int(round(-math.log10(point)))
            if 0 <= derived <= 8:
                digits = derived
        else:
            point = 10 ** (-digits)
        return SymbolSpec(
            symbol=symbol,
            description=symbol,
            digits=digits,
            point=point,
            contract_size=_contract_size(symbol),
            min_volume=0.01,
            max_volume=100.0,
            volume_step=0.01,
            spread=float(getattr(candle, "spread", 0) or 0),
            trade_mode="disabled",
        )

    def symbols(self) -> List[SymbolSpec]:
        return [self._spec(name) for name in self._symbol_names()]

    def symbol(self, symbol: str) -> SymbolSpec:
        available = self._symbol_names()
        resolved = resolve_symbol(symbol, available)
        if resolved is None:
            raise TransportError("SYMBOL_NOT_FOUND",
                                 f"{symbol!r} is not exported for this account")
        return self._spec(resolved)

    def price(self, symbol: str) -> Price:
        resolved = resolve_symbol(symbol, self._symbol_names())
        if resolved is None:
            raise TransportError("SYMBOL_NOT_FOUND",
                                 f"{symbol!r} is not exported for this account")
        candles = [c for c in self._native_candles()
                   if c.symbol.upper() == resolved.upper()]
        if not candles:
            raise TransportUnavailable(
                "PRICE_UNAVAILABLE",
                f"no native bars for {resolved}; the writer may be offline")
        latest = max(candles, key=lambda c: epoch_from_iso(c.open_time))
        bid = float(getattr(latest, "bid", 0) or 0)
        ask = float(getattr(latest, "ask", 0) or 0)
        close = float(latest.close or 0)
        spread = float(getattr(latest, "spread", 0) or 0)
        if bid <= 0 or ask <= 0:
            bid = ask = close
            spread = 0.0
        if spread <= 0:
            spread = max(0.0, ask - bid)
        return Price(
            symbol=resolved,
            bid=bid,
            ask=ask,
            spread=round(spread, 8),
            time=latest.close_time or latest.open_time or iso_now(),
        )

    def candles(self, symbol: str, timeframe: str,
                start_time: Optional[str], limit: int) -> List[Candle]:
        resolved = resolve_symbol(symbol, self._symbol_names())
        if resolved is None:
            raise TransportError("SYMBOL_NOT_FOUND",
                                 f"{symbol!r} is not exported for this account")
        source = [c for c in self._native_candles()
                  if c.symbol.upper() == resolved.upper()]
        if not source:
            return []
        source.sort(key=lambda c: epoch_from_iso(c.open_time))
        source_tf = _canonical_timeframe(source[0].timeframe or "5m")
        source_s = _timeframe_seconds(source_tf) or 300
        target_tf = _canonical_timeframe(timeframe)
        target_s = _timeframe_seconds(target_tf)
        if target_s <= 0:
            raise TransportError("INVALID_TIMEFRAME",
                                 f"unsupported timeframe {timeframe!r}")
        if target_s < source_s:
            # Upsampling would be an invention. Refuse instead of emitting
            # synthetic bars the writer never produced.
            raise TransportError(
                "TIMEFRAME_NOT_AVAILABLE",
                f"native bars are {source_tf}; {target_tf} would be synthetic")

        now = time.time()
        rows: List[Candle] = []
        if target_s == source_s:
            for c in source:
                open_epoch = epoch_from_iso(c.open_time)
                close_epoch = epoch_from_iso(c.close_time,
                                             open_epoch + source_s)
                rows.append(Candle(
                    symbol=resolved,
                    timeframe=target_tf,
                    open_time=c.open_time,
                    close_time=iso_from_epoch(close_epoch),
                    open=float(c.open),
                    high=float(c.high),
                    low=float(c.low),
                    close=float(c.close),
                    volume=float(c.volume or 0),
                    closed=bool(c.closed) and close_epoch <= now + 1,
                ))
        else:
            buckets: Dict[int, Dict[str, Any]] = {}
            for c in source:
                open_epoch = epoch_from_iso(c.open_time)
                if open_epoch <= 0:
                    continue
                key = int(open_epoch // target_s) * target_s
                bucket = buckets.get(key)
                if bucket is None:
                    buckets[key] = {
                        "open": float(c.open),
                        "high": float(c.high),
                        "low": float(c.low),
                        "close": float(c.close),
                        "volume": float(c.volume or 0),
                    }
                else:
                    bucket["high"] = max(bucket["high"], float(c.high))
                    bucket["low"] = min(bucket["low"], float(c.low))
                    bucket["close"] = float(c.close)
                    bucket["volume"] += float(c.volume or 0)
            for key in sorted(buckets):
                bucket = buckets[key]
                rows.append(Candle(
                    symbol=resolved,
                    timeframe=target_tf,
                    open_time=iso_from_epoch(key),
                    close_time=iso_from_epoch(key + target_s),
                    open=bucket["open"],
                    high=bucket["high"],
                    low=bucket["low"],
                    close=bucket["close"],
                    volume=bucket["volume"],
                    closed=(key + target_s) <= now + 1,
                ))

        if start_time:
            start_epoch = epoch_from_iso(start_time)
            if start_epoch > 0:
                rows = [r for r in rows
                        if epoch_from_iso(r.open_time) >= start_epoch]
        return rows[-max(1, int(limit)):]

    def positions(self) -> List[Position]:
        try:
            receipts = self._engine_store().recent_open_positions(
                self.engine_connection_id)
            ledger = self._engine_store().list_ledger(
                self.engine_connection_id, limit=1000)
        except Exception:
            return []
        closed = {int(row.get("native_ticket") or 0)
                  for row in ledger
                  if str(row.get("event") or "").upper() in ("CLOSE", "CASH")}
        out: List[Position] = []
        for receipt in receipts:
            ticket = int(receipt.get("native_ticket") or 0)
            if ticket and ticket in closed:
                continue
            symbol = str(receipt.get("symbol") or "")
            side = str(receipt.get("direction") or "").lower()
            volume = float(receipt.get("volume") or 0)
            entry = float(receipt.get("fill_price") or 0)
            try:
                current = self.price(symbol).bid if symbol else entry
            except TransportError:
                current = entry
            direction = 1.0 if side == "buy" else -1.0
            gross = (current - entry) * direction * volume * \
                _contract_size(symbol)
            swap = float(receipt.get("swap") or 0)
            commission = float(receipt.get("commission") or 0)
            out.append(Position(
                position_id=str(ticket or receipt.get("receipt_id") or ""),
                symbol=symbol,
                side=side,
                volume=volume,
                open_price=entry,
                current_price=current,
                stop_loss=float(receipt.get("stop_loss") or 0),
                take_profit=float(receipt.get("take_profit") or 0),
                profit=round(gross + swap + commission, 2),
                swap=swap,
                commission=commission,
                comment=str(receipt.get("reason_code") or ""),
                opened_at=str(receipt.get("received_at") or iso_now()),
            ))
        return out

    def orders(self) -> List[Order]:
        # Pending orders live only inside the terminal and are not part of the
        # native export; inventing them would be worse than returning none.
        return []

    def deals(self, start_time: Optional[str], end_time: Optional[str],
              limit: int) -> List[Deal]:
        try:
            ledger = self._engine_store().list_ledger(
                self.engine_connection_id, limit=max(200, int(limit) * 4))
        except Exception:
            return []
        start_epoch = epoch_from_iso(start_time) if start_time else 0.0
        end_epoch = epoch_from_iso(end_time) if end_time else 0.0
        out: List[Deal] = []
        for row in ledger:
            event = str(row.get("event") or "").upper()
            if event not in ("CLOSE", "CASH"):
                continue
            moment = str(row.get("observed_at") or "")
            epoch = epoch_from_iso(moment)
            if start_epoch and epoch < start_epoch:
                continue
            if end_epoch and epoch > end_epoch:
                continue
            out.append(Deal(
                deal_id=f"ldg-{row.get('id')}",
                symbol=str(row.get("symbol") or ""),
                side=str(row.get("direction") or "").lower(),
                volume=float(row.get("volume") or 0),
                price=float(row.get("price") or 0),
                position_id=str(row.get("native_ticket") or ""),
                profit=float(row.get("realized_pnl") or 0),
                commission=float(row.get("commission") or 0),
                swap=float(row.get("swap") or 0),
                comment=event.lower(),
                time=moment or iso_now(),
            ))
        out.sort(key=lambda d: epoch_from_iso(d.time), reverse=True)
        return out[:max(1, int(limit))]

    def history_orders(self, start_time: Optional[str],
                       end_time: Optional[str], limit: int) -> List[Order]:
        try:
            receipts = self._engine_store().list_receipts(
                self.engine_connection_id, limit=max(200, int(limit) * 4))
        except Exception:
            return []
        start_epoch = epoch_from_iso(start_time) if start_time else 0.0
        end_epoch = epoch_from_iso(end_time) if end_time else 0.0
        out: List[Order] = []
        for receipt in receipts:
            moment = str(receipt.get("received_at") or "")
            epoch = epoch_from_iso(moment)
            if start_epoch and epoch < start_epoch:
                continue
            if end_epoch and epoch > end_epoch:
                continue
            state = str(receipt.get("reconciled_state")
                        or receipt.get("execution_state") or "unknown").lower()
            out.append(Order(
                order_id=str(receipt.get("native_ticket")
                             or receipt.get("receipt_id") or ""),
                symbol=str(receipt.get("symbol") or ""),
                side=str(receipt.get("direction") or "").lower(),
                type="market",
                volume=float(receipt.get("volume") or 0),
                price=float(receipt.get("fill_price") or 0),
                stop_loss=float(receipt.get("stop_loss") or 0),
                take_profit=float(receipt.get("take_profit") or 0),
                state=state,
                comment=str(receipt.get("reason_code") or ""),
                created_at=moment or iso_now(),
            ))
        out.sort(key=lambda o: epoch_from_iso(o.created_at), reverse=True)
        return out[:max(1, int(limit))]

    def execute(self, kind: str, payload: Dict[str, Any],
                command_id: str) -> Dict[str, Any]:
        raise TransportError(
            "READ_ONLY_ACCOUNT",
            "this account is traded by the APEX engine and is exposed "
            "read-only through the API; send orders through the platform",
        )

    def close(self) -> None:
        self._cache = None
