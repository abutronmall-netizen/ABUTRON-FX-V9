"""Broker-native M5 market data adapter for the agentic decision path.

Replaces the empty ``candles=[]`` / ``quotes=[]`` smoke test with real
broker-native, account/session-bound, fresh, closed M5 candles and
quotes read from the existing native MT5 exports
(``ABUTRON_NATIVE_BARS_V2_*.txt`` + ``ABUTRON_NATIVE_ACCOUNT_V2_*.txt``).

The adapter binds connection, broker, server, login, owner, native
source hash/freshness and source timestamps. It rejects mixed accounts,
incomplete bars, open candles, missing quotes, stale sources and
missing feature windows before any data reaches the decision engine.
No fabricated candles, prices, spreads or placeholder owner IDs.

This is a provenance-preserving adapter: it does not substitute textual
inference scores for real Candle/Quote data.
"""
from __future__ import annotations

import calendar
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..feeds.feed_quality import (Candle, Quote, candle_source_hash,
                                  normalize_native_bars,
                                  validate_export_envelope)
from ..feeds.mt5_terminal import Mt5TerminalManager, terminal_manager


class MarketDataError(Exception):
    """Raised when broker-native market data is invalid or unbound."""


@dataclass
class MarketDataResult:
    """Validated broker-native market data for one decision pass."""

    candles: List[Candle]
    quotes: List[Quote]
    connection_id: str
    broker: str
    login: int
    server: str
    source_hash: str
    source_ts: float
    symbol_count: int
    evaluated_symbols: List[str] = field(default_factory=list)

    @property
    def candle_count(self) -> int:
        return len(self.candles)

    @property
    def quote_count(self) -> int:
        return len(self.quotes)


class BrokerNativeMarketDataAdapter:
    """Reads real broker-native M5 data from native MT5 exports.

    Each ``read()`` call produces a ``MarketDataResult`` bound to the
    terminal's account/server/session. The data is validated against
    the bound identity before it is returned; invalid data raises
    ``MarketDataError`` (fail-closed).
    """

    def __init__(
        self,
        manager: Optional[Mt5TerminalManager] = None,
        *,
        connection_id: str = "",
        broker: str = "",
        login: int = 0,
        server: str = "",
        min_bars: int = 1,
        max_stale_seconds: int = 120,
    ) -> None:
        self.manager = manager or terminal_manager
        self.connection_id = connection_id
        self.broker = broker
        self.login = login
        self.server = server
        self.min_bars = min_bars
        self.max_stale_seconds = max_stale_seconds

    def read(
        self,
        *,
        connection_id: Optional[str] = None,
        since: Optional[float] = None,
    ) -> MarketDataResult:
        """Read and validate broker-native M5 candles + quotes.

        ``since`` (unix timestamp) rejects stale native exports whose
        file mtime is older than the given time — consistent with the
        Issue #38 STALE_NATIVE_FILE fix.

        Raises ``MarketDataError`` if the export is missing, stale,
        wrong-account, contains open/incomplete bars, or lacks quotes.
        """
        cid = connection_id or self.connection_id
        if not cid:
            raise MarketDataError("no connection_id for market data read")

        # Read the native export (identity + bars) from the terminal.
        export = self.manager.read_native_export(cid)
        if export is None:
            raise MarketDataError(
                f"no native M5 export for {cid}: no broker-native market data")

        identity = export.get("identity", {})
        bars_raw = export.get("bars", {})

        # Validate the identity envelope (account binding, nonce, freshness).
        errors = validate_export_envelope(
            identity, connection_id=cid, login=self.login, broker=self.broker)
        if errors:
            raise MarketDataError(
                f"native export envelope invalid for {cid}: "
                + ", ".join(errors))

        if identity.get("server") != self.server or not self.server:
            raise MarketDataError("native export server does not match bound terminal")

        # Freshness check: reject stale sources by heartbeat timestamp.
        # The identity heartbeat is the authoritative source timestamp.
        hb_raw = identity.get("worker_health_ts", "")
        export_ts = _parse_health_ts(hb_raw)
        if export_ts <= 0 or export_ts > time.time() + 5:
            raise MarketDataError("native export heartbeat invalid or future")
        if since is not None and export_ts < since:
            raise MarketDataError(
                f"native M5 export stale for {cid}: "
                f"heartbeat older than run start")

        # Parse bars into Candles.
        candles = normalize_native_bars(
            bars_raw, connection_id=cid, login=self.login, broker=self.broker)

        # Validate candles: all closed, non-empty, sufficient bars.
        self._validate_candles(candles, cid)

        # Build quotes from the latest closed bar per symbol.
        quotes = self._build_quotes(candles)
        if not quotes:
            raise MarketDataError(
                f"no quotes could be built for {cid}: missing bid/ask data")

        # Source hash: SHA-256 of the bars file content + identity nonce.
        source_hash = _compute_source_hash(
            bars_raw, identity.get("nonce", ""))

        symbols = sorted({c.symbol for c in candles})
        evaluated = sorted({c.symbol for c in candles if c.closed})

        return MarketDataResult(
            candles=candles,
            quotes=quotes,
            connection_id=cid,
            broker=identity.get("broker", self.broker),
            login=int(identity.get("login", self.login)),
            server=identity.get("server", self.server),
            source_hash=source_hash,
            source_ts=export_ts,
            symbol_count=len(symbols),
            evaluated_symbols=evaluated,
        )

    # ------------------------------------------------------------------ #
    def _validate_candles(self, candles: List[Candle], cid: str) -> None:
        """Reject open candles, incomplete bars and missing feature windows."""
        if not candles:
            raise MarketDataError(f"no candles in native export for {cid}")
        if len(candles) < self.min_bars:
            raise MarketDataError(
                f"insufficient closed M5 bars for {cid}: "
                f"{len(candles)} < min {self.min_bars}")
        open_candles = [c for c in candles if not c.closed]
        if open_candles:
            raise MarketDataError(
                f"open (non-closed) candle in native export for {cid}: "
                f"{open_candles[0].symbol}")
        # Each candle must have valid OHLC.
        for c in candles:
            if c.open <= 0 or c.high <= 0 or c.low <= 0 or c.close <= 0:
                raise MarketDataError(
                    f"incomplete OHLC bar in native export for {cid}: "
                    f"{c.symbol} {c.open_time}")

    @staticmethod
    def _build_quotes(candles: List[Candle]) -> List[Quote]:
        """Build Quote objects from the latest closed bar per symbol."""
        by_symbol: Dict[str, Candle] = {}
        for c in candles:
            if c.closed:
                by_symbol[c.symbol] = c
        quotes: List[Quote] = []
        for symbol, c in by_symbol.items():
            if c.bid <= 0 or c.ask <= 0:
                continue
            spread = c.spread if c.spread > 0 else (c.ask - c.bid)
            quotes.append(Quote(
                symbol=symbol,
                bid=c.bid,
                ask=c.ask,
                spread=spread,
                tick_size=c.tick_size,
                point=c.point,
                observed_at=c.close_time,
            ))
        return quotes


def _parse_health_ts(hb: str) -> float:
    """Parse an ISO heartbeat timestamp to unix epoch, or 0 if invalid."""
    if not hb:
        return 0.0
    try:
        return calendar.timegm(time.strptime(hb, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0.0


def _compute_source_hash(bars_raw: Dict[str, Any], nonce: str) -> str:
    """Compute a stable SHA-256 source hash for the bars + nonce."""
    blob = repr(bars_raw) + "|" + str(nonce)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def run_shadow_decision_pass(
    engine,
    adapter: BrokerNativeMarketDataAdapter,
    *,
    connection_id: str,
    broker: str,
    owner_id: str,
    account: Optional[Dict[str, Any]] = None,
    equity: Optional[float] = None,
    since: Optional[float] = None,
) -> Dict[str, Any]:
    """Run a real broker-native shadow decision pass.

    Reads real M5 candles/quotes from the adapter, supplies
    ``set_history`` with the trailing closed M5 series, then calls the
    existing ``AgenticDecisionEngine.run_decision_pass``. The result
    records the number of actually evaluated symbols and the actual
    brain decision outcome — distinct from broker execution status.

    Preserves actual AI HOLD/reject decisions; does not force
    BUY/SELL or fabricate positive candidates.
    """
    if account is None:
        account = {}

    # Read real broker-native market data.
    md = adapter.read(connection_id=connection_id, since=since)

    # Supply the brain's warm-up history per (symbol, timeframe).
    history_by_key: Dict[tuple, List[Candle]] = {}
    for c in md.candles:
        if c.closed:
            history_by_key.setdefault(
                (c.symbol, c.timeframe), []).append(c)
    latest_closed = []
    for (symbol, timeframe), candles in history_by_key.items():
        ordered = sorted(candles, key=lambda c: c.open_time)
        engine.set_history(symbol, timeframe, ordered)
        # The engine evaluates the FIRST candle per symbol it sees.
        # Give it the latest closed bar, not the oldest warm-up bar.
        latest_closed.append(ordered[-1])

    # Call the existing decision engine with real Candle/Quote objects.
    candidates = engine.run_decision_pass(
        connection_id=connection_id,
        broker=broker,
        owner_id=owner_id,
        candles=latest_closed,
        quotes=md.quotes,
        account=account,
        equity=equity if equity is not None else float(
            account.get("equity", account.get("balance", 0.0)) or 0.0),
    )

    return {
        "decision_mode": "BROKER_NATIVE_M5_SHADOW_PASS",
        "evaluated_symbols": md.evaluated_symbols,
        "evaluated_symbol_count": len(md.evaluated_symbols),
        "candle_count": md.candle_count,
        "quote_count": md.quote_count,
        "candidates_generated": len(candidates),
        "candidates": [
            {
                "symbol": c.symbol,
                "direction": c.direction,
                "conviction": c.conviction,
                "reason_code": c.reason_code,
            }
            for c in candidates
        ],
        "source_hash": md.source_hash,
        "source_ts": md.source_ts,
        # A candidate is not a broker-acknowledged trade or certified fill.
        "broker_native_signal_certified": False,
        "shadow_decision_evaluated": True,
        "connection_id": md.connection_id,
        "broker": md.broker,
        "login": md.login,
        "server": md.server,
    }
