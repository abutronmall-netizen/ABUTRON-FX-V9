"""Feed quality contract (blueprint section 04).

Every native export must pass validation before it can feed decisions:
account/connection binding, closed-candle integrity, provenance hash and
quote/cost fields. Stale, future, duplicated or in-progress candles are
rejected — they can never be merged as fresh evidence.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class FeedQualityError(Exception):
    pass


@dataclass
class Candle:
    symbol: str
    timeframe: str
    open_time: str          # ISO UTC, bar open
    close_time: str         # ISO UTC, bar close (open + tf seconds)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    spread: float = 0.0
    tick_size: float = 0.0
    point: float = 0.0
    closed: bool = True
    source_hash: str = ""

    @property
    def bar_key(self) -> str:
        return f"{self.symbol}|{self.timeframe}|{self.open_time}"

    def replay_key(self, connection_id: str, direction: str = "") -> str:
        return f"{connection_id}|{self.bar_key}|{direction}"


def _tf_seconds(timeframe: str) -> int:
    return {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400,
            "D1": 86400}[timeframe]


def validate_candle(c: Candle, *, connection_id: str, login: int,
                    broker: str, now_utc: Optional[str] = None) -> List[str]:
    """Return a list of violations; empty list = accepted."""
    errors: List[str] = []
    now = now_utc or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # provenance
    if not connection_id or not login:
        errors.append("PROVENANCE_MISSING")
    if broker not in ("deriv", "fpmarkets"):
        errors.append("PROVENANCE_BAD_BROKER")

    # closed-candle integrity: close_time must equal open_time + tf
    try:
        ot = time.mktime(time.strptime(c.open_time, "%Y-%m-%dT%H:%M:%SZ"))
        ct = time.mktime(time.strptime(c.close_time, "%Y-%m-%dT%H:%M:%SZ"))
        expected = ct - ot
        if expected != _tf_seconds(c.timeframe):
            errors.append("CANDLE_GEOMETRY_MISMATCH")
    except (ValueError, TypeError):
        errors.append("CANDLE_TIMESTAMP_INVALID")

    # not in the future
    if c.close_time > now:
        errors.append("CANDLE_FUTURE")

    # not stale (older than 24h of the primary timeframe's bar count)
    if c.close_time < time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(time.time() - 86400)):
        errors.append("CANDLE_STALE")

    if not c.closed:
        errors.append("CANDLE_NOT_CLOSED")

    # OHLC sanity
    if not (c.low <= c.open <= c.high and c.low <= c.close <= c.high):
        errors.append("CANDLE_OHLC_INVALID")
    if c.high < c.low:
        errors.append("CANDLE_OHLC_INVALID")

    # quote / cost fields required for executable sizing
    if c.tick_size <= 0 or c.point <= 0:
        errors.append("CONTRACT_GEOMETRY_MISSING")

    # source hash
    if not c.source_hash:
        errors.append("SOURCE_HASH_MISSING")
    return errors


def candle_source_hash(c: Candle) -> str:
    blob = f"{c.symbol}|{c.timeframe}|{c.open_time}|{c.open}|{c.high}|{c.low}" \
           f"|{c.close}|{c.volume}|{c.bid}|{c.ask}"
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    spread: float
    tick_size: float
    point: float
    volume_min: float = 0.01
    volume_max: float = 100.0
    point_value: float = 1.0      # USD per point per lot (contract-specific)
    commission: float = 0.0       # USD per lot (round-turn)
    observed_at: str = ""

    def spread_in_points(self) -> float:
        return self.spread / self.point if self.point else 0.0


def normalize_native_bars(raw: Any, *, connection_id: str,
                          login: int, broker: str) -> List[Candle]:
    """Parse a native export payload (MT5 Files JSON/CSV) into Candles.

    Accepts either the full export envelope (``{"bars": [...]}``) or the bare
    bar list, because the EA's bars file is normalised to a list by the
    terminal reader. A malformed row is skipped rather than aborting the whole
    export — one bad line must not blank an account's feed.
    """
    if isinstance(raw, dict):
        rows = raw.get("bars", []) or []
    elif isinstance(raw, (list, tuple)):
        rows = list(raw)
    else:
        return []
    candles: List[Candle] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            c = Candle(
                symbol=r["symbol"], timeframe=r.get("timeframe", "M5"),
                open_time=r["open_time"], close_time=r.get("close_time", ""),
                open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]),
                volume=float(r.get("volume", 0.0)),
                bid=float(r.get("bid", 0.0)), ask=float(r.get("ask", 0.0)),
                spread=float(r.get("spread", 0.0)),
                tick_size=float(r.get("tick_size", 0.0)),
                point=float(r.get("point", 0.0)),
                closed=bool(r.get("closed", True)),
            )
        except (KeyError, TypeError, ValueError):
            continue  # malformed row — skip it, keep the rest of the export
        if not c.close_time:
            ot = time.mktime(time.strptime(c.open_time, "%Y-%m-%dT%H:%M:%SZ"))
            c.close_time = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(ot + _tf_seconds(c.timeframe)))
        c.source_hash = candle_source_hash(c)
        candles.append(c)
    return candles


def validate_export_envelope(raw: Dict[str, Any], *, connection_id: str,
                             login: int, broker: str) -> List[str]:
    """Validate the export envelope: account, nonce, challenge, health."""
    errors: List[str] = []
    if raw.get("connection_id") != connection_id:
        errors.append("EXPORT_CONNECTION_MISMATCH")
    if int(raw.get("login", 0)) != login:
        errors.append("EXPORT_LOGIN_MISMATCH")
    if raw.get("broker") != broker:
        errors.append("EXPORT_BROKER_MISMATCH")
    if not raw.get("nonce"):
        errors.append("EXPORT_NONCE_MISSING")
    if not raw.get("challenge"):
        errors.append("EXPORT_CHALLENGE_MISSING")
    hb = raw.get("worker_health_ts", "")
    if not hb:
        errors.append("EXPORT_HEARTBEAT_MISSING")
    else:
        try:
            age = time.time() - time.mktime(time.strptime(hb, "%Y-%m-%dT%H:%M:%SZ"))
            if age > 120:
                errors.append("EXPORT_HEARTBEAT_STALE")
        except (ValueError, TypeError):
            errors.append("EXPORT_HEARTBEAT_INVALID")
    return errors
