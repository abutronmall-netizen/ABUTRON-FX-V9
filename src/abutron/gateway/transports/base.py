"""Transport contract + shared value objects.

A *transport* is the last mile between the API tier and one MT5 account.
Three implementations exist:

* ``SimulatedTransport`` — deterministic in-process market/portfolio.
  Runs anywhere, no Wine, no broker. Used for development, demos and tests.
* ``BridgeTransport`` — a hosted account whose terminal runs an MQL5
  bridge EA that *pulls* commands over HTTP and *pushes* state. No inbound
  ports at the client site; works through NAT.
* (extension point) a Wine-side ``MetaTrader5`` Python transport. The
  interface below is intentionally small enough for that adapter.

The API tier never speaks to a broker directly. It calls a transport.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Sequence

TIMEFRAME_SECONDS: Dict[str, int] = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

# MT5 chart timeframe -> API timeframe, for bridges that report MT5 names.
MT5_TIMEFRAME_ALIASES: Dict[str, str] = {
    "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H4": "4h", "D1": "1d",
}


class TransportError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TransportUnavailable(TransportError):
    pass


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def epoch_from_iso(value: Optional[str], default: float = 0.0) -> float:
    if not value:
        return default
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass
class AccountInfo:
    login: int
    broker: str = ""
    server: str = ""
    name: str = ""
    currency: str = "USD"
    leverage: int = 0
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    free_margin: float = 0.0
    margin_level: float = 0.0
    trade_allowed: bool = True
    connected: bool = True
    environment: str = "demo"
    updated_at: str = field(default_factory=iso_now)
    stale: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SymbolSpec:
    symbol: str
    description: str = ""
    digits: int = 5
    point: float = 0.00001
    contract_size: float = 100000.0
    min_volume: float = 0.01
    max_volume: float = 100.0
    volume_step: float = 0.01
    spread: float = 0.0
    trade_mode: str = "full"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Price:
    symbol: str
    bid: float
    ask: float
    spread: float = 0.0
    time: str = field(default_factory=iso_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Candle:
    symbol: str
    timeframe: str
    open_time: str
    close_time: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    closed: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Position:
    position_id: str
    symbol: str
    side: str
    volume: float
    open_price: float
    current_price: float
    stop_loss: float = 0.0
    take_profit: float = 0.0
    profit: float = 0.0
    swap: float = 0.0
    commission: float = 0.0
    comment: str = ""
    magic: int = 0
    opened_at: str = field(default_factory=iso_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str
    type: str
    volume: float
    price: float
    current_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    state: str = "placed"
    comment: str = ""
    created_at: str = field(default_factory=iso_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Deal:
    deal_id: str
    symbol: str
    side: str
    volume: float
    price: float
    position_id: str = ""
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    comment: str = ""
    time: str = field(default_factory=iso_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
class Mt5Transport(Protocol):
    kind: str

    @property
    def connected(self) -> bool: ...

    def set_event_sink(self, sink) -> None: ...

    def account_information(self) -> AccountInfo: ...
    def symbols(self) -> List[SymbolSpec]: ...
    def symbol(self, symbol: str) -> SymbolSpec: ...
    def price(self, symbol: str) -> Price: ...
    def candles(self, symbol: str, timeframe: str,
                start_time: Optional[str], limit: int) -> List[Candle]: ...
    def positions(self) -> List[Position]: ...
    def orders(self) -> List[Order]: ...
    def deals(self, start_time: Optional[str], end_time: Optional[str],
              limit: int) -> List[Deal]: ...
    def history_orders(self, start_time: Optional[str],
                       end_time: Optional[str], limit: int) -> List[Order]: ...

    def execute(self, kind: str, payload: Dict[str, Any],
                command_id: str) -> Dict[str, Any]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Helpers shared by transports
# ---------------------------------------------------------------------------
def resolve_symbol(requested: str, available: Sequence[str],
                   suffixes: Sequence[str] = (".r", ".pro", "m")) -> Optional[str]:
    """Map ``EURUSD`` to a broker's ``EURUSD.r`` (and back) safely."""
    if not requested:
        return None
    wanted = requested.strip().upper()
    by_upper = {s.upper(): s for s in available}
    if wanted in by_upper:
        return by_upper[wanted]
    for suffix in suffixes:
        candidate = (wanted + suffix).upper()
        if candidate in by_upper:
            return by_upper[candidate]
    for suffix in suffixes:
        if wanted.endswith(suffix.upper()):
            stripped = wanted[:-len(suffix)]
            if stripped in by_upper:
                return by_upper[stripped]
    return None


def normalize_volume(volume: float, spec: SymbolSpec) -> Optional[float]:
    """Round to the symbol's volume step; return None if outside limits."""
    if volume < spec.min_volume or volume > spec.max_volume:
        return None
    step = spec.volume_step or 0.01
    steps = round(volume / step)
    rounded = round(steps * step, 8)
    if rounded < spec.min_volume or rounded > spec.max_volume:
        return None
    return rounded


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def timeframe_seconds(timeframe: str) -> int:
    key = timeframe.strip()
    key = MT5_TIMEFRAME_ALIASES.get(key.upper(), key.lower())
    if key not in TIMEFRAME_SECONDS:
        raise TransportError("INVALID_TIMEFRAME",
                             f"unsupported timeframe: {timeframe}")
    return TIMEFRAME_SECONDS[key]


def normalize_timeframe(timeframe: str) -> str:
    key = timeframe.strip()
    key = MT5_TIMEFRAME_ALIASES.get(key.upper(), key.lower())
    if key not in TIMEFRAME_SECONDS:
        raise TransportError("INVALID_TIMEFRAME",
                             f"unsupported timeframe: {timeframe}")
    return key
