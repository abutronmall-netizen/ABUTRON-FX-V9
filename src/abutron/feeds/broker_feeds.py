"""Native broker feed readers: Deriv + FP Markets.

Each reader consumes the account-isolated MT5 terminal exports, validates
the feed-quality contract, and exposes normalized candles + quotes bound
to (connection, owner, broker, environment). No cross-account merging.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..config import DERIV_SERVERS, FP_SYMBOL_SUFFIX_HINT, utc_now
from ..events import EventLog, make_event
from .feed_quality import (Candle, Quote, FeedQualityError, normalize_native_bars,
                            validate_candle, validate_export_envelope)
from .clock_offset import clock_estimator
from .mt5_terminal import terminal_manager


@dataclass
class FeedResult:
    connection_id: str
    broker: str
    login: int
    candles: List[Candle]
    quotes: List[Quote]
    accepted: bool
    violations: List[str]
    clock_verified: bool


class BaseBrokerFeed:
    broker_name = ""

    def __init__(self, event_log: EventLog):
        self.log = event_log

    def read(self, connection_id: str, login: int,
             symbols: Optional[List[str]] = None) -> FeedResult:
        raw = terminal_manager.read_native_export(connection_id)
        if raw is None:
            self.log.append(make_event(
                "feed.read", connection_id=connection_id, broker=self.broker_name,
                mt5_login=login, execution_state="UNKNOWN",
                reason_code="PROCESS_EXITED"))
            return FeedResult(connection_id, self.broker_name, login,
                              [], [], False, ["EXPORT_MISSING"], False)

        ident = raw["identity"]
        # Validate the broker's claimed identity, never substitute our
        # expected broker name for the native export's actual assertion.
        envelope_errors = validate_export_envelope(
            ident, connection_id=connection_id, login=login,
            broker=self.broker_name)
        candles = normalize_native_bars(raw["bars"], connection_id=connection_id,
                                        login=login, broker=self.broker_name)

        violations = list(envelope_errors)
        accepted_candles: List[Candle] = []
        for c in candles:
            errs = validate_candle(c, connection_id=connection_id, login=login,
                                   broker=self.broker_name)
            if errs:
                violations.extend(f"{c.bar_key}:{e}" for e in errs)
            else:
                accepted_candles.append(c)

        quotes = self._build_quotes(accepted_candles, symbols)
        clock = clock_estimator.estimate(self.broker_name, accepted_candles,
                                         utc_now())
        clock_ok = clock.verified
        if not clock_ok:
            violations.append(clock.status)

        self.log.append(make_event(
            "feed.ingest", connection_id=connection_id, broker=self.broker_name,
            mt5_login=login, symbol=symbols[0] if symbols else None,
            native_bar_open=accepted_candles[-1].open_time if accepted_candles else None,
            closed_at=accepted_candles[-1].close_time if accepted_candles else None,
            execution_state="ACCEPTED" if (not violations and clock_ok) else "REJECTED",
            reason_code=";".join(violations[:3]) or "OK"))

        return FeedResult(connection_id, self.broker_name, login,
                          accepted_candles, quotes,
                          not violations and clock_ok, violations, clock_ok)

    def _build_quotes(self, candles: List[Candle],
                      symbols: Optional[List[str]]) -> List[Quote]:
        out: List[Quote] = []
        wanted = set(symbols or [])
        last_by_sym: Dict[str, Candle] = {}
        for c in candles:
            last_by_sym[c.symbol] = c
        for sym, c in last_by_sym.items():
            if wanted and sym not in wanted:
                continue
            out.append(Quote(
                symbol=sym, bid=c.bid, ask=c.ask,
                spread=c.spread or (c.ask - c.bid),
                tick_size=c.tick_size, point=c.point,
                observed_at=c.close_time or utc_now()))
        return out


class DerivFeed(BaseBrokerFeed):
    broker_name = "deriv"

    def validate_server(self, server: str) -> bool:
        return server in DERIV_SERVERS


class FpMarketsFeed(BaseBrokerFeed):
    broker_name = "fpmarkets"

    def normalize_symbol(self, symbol: str) -> str:
        # FP uses suffixed FX symbols, e.g. GBPUSD.r
        if symbol.endswith(FP_SYMBOL_SUFFIX_HINT):
            return symbol
        return f"{symbol}{FP_SYMBOL_SUFFIX_HINT}"


def get_feed(broker: str, event_log: EventLog) -> BaseBrokerFeed:
    if broker == "deriv":
        return DerivFeed(event_log)
    if broker == "fpmarkets":
        return FpMarketsFeed(event_log)
    raise FeedQualityError(f"unknown broker: {broker}")
