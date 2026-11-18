"""Request / response models for the public gateway API.

Names follow MetaTrader 5 terminology (positions, orders, deals) so a
developer coming from the terminal or from MetaApi maps quickly. All
timestamps are UTC ISO-8601 strings (``...Z``).
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
Side = Literal["buy", "sell"]
ControlMode = Literal["api", "read_only"]
Environment = Literal["demo", "live"]
MarketDataMode = Literal["simulated", "bridge"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ErrorBody(StrictModel):
    code: str
    message: str
    request_id: str = ""
    details: Dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(StrictModel):
    error: ErrorBody


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
class CreateAccountRequest(StrictModel):
    login: int = Field(ge=1, description="MT5 account login")
    password: Optional[str] = Field(
        default=None, max_length=256,
        description="Used once for provisioning, never stored or returned.")
    server: str = Field(min_length=1, max_length=128)
    broker: str = Field(default="custom", max_length=48)
    name: Optional[str] = Field(default=None, max_length=96)
    platform: Literal["mt5"] = "mt5"
    environment: Environment = "demo"
    control_mode: ControlMode = "api"
    market_data_mode: Optional[MarketDataMode] = None
    symbols: List[str] = Field(default_factory=list, max_length=200)
    max_lot: Optional[float] = Field(default=None, gt=0, le=1000)
    max_open_positions: Optional[int] = Field(default=None, ge=1, le=1000)
    daily_loss_limit_usd: Optional[float] = Field(default=None, gt=0)
    symbol_allowlist: Optional[List[str]] = Field(default=None, max_length=200)

    @field_validator("symbols", "symbol_allowlist")
    @classmethod
    def _clean_symbols(cls, value):
        if value is None:
            return value
        out = []
        for s in value:
            s = s.strip().upper()
            if s and s not in out:
                out.append(s)
        return out


class UpdateAccountRequest(StrictModel):
    name: Optional[str] = Field(default=None, max_length=96)
    trading_enabled: Optional[bool] = None
    control_mode: Optional[ControlMode] = None
    max_lot: Optional[float] = Field(default=None, gt=0, le=1000)
    max_open_positions: Optional[int] = Field(default=None, ge=1, le=1000)
    daily_loss_limit_usd: Optional[float] = Field(default=None, gt=0)
    symbol_allowlist: Optional[List[str]] = None
    symbols: Optional[List[str]] = None


class AccountOut(StrictModel):
    account_id: str
    tenant_id: str = ""
    name: str
    broker: str
    server: str
    login: int
    platform: str = "mt5"
    environment: str
    status: str
    control_mode: str
    trading_enabled: bool
    market_data_mode: str
    symbols: List[str] = Field(default_factory=list)
    max_lot: Optional[float] = None
    max_open_positions: Optional[int] = None
    daily_loss_limit_usd: Optional[float] = None
    symbol_allowlist: Optional[List[str]] = None
    created_at: str = ""
    updated_at: str = ""
    last_seen_at: str = ""
    last_error: str = ""


class BridgeInfo(StrictModel):
    transport: str = "pull-http"
    token: str
    state_url: str
    commands_url: str
    result_url_template: str
    handshake_url: str
    note: str = ("Bridge presents this token as X-Bridge-Token. Token is "
                 "shown once and can be rotated.")


class AccountCreateOut(AccountOut):
    bridge: Optional[BridgeInfo] = None
    credentials_accepted: bool = False


class BridgeTokenOut(StrictModel):
    account_id: str
    bridge: BridgeInfo


# ---------------------------------------------------------------------------
# Market data / terminal state
# ---------------------------------------------------------------------------
class AccountInfoOut(StrictModel):
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
    updated_at: str = ""
    stale: bool = False


class SymbolOut(StrictModel):
    symbol: str
    description: str = ""
    digits: int = 5
    point: float = 0.00001
    contract_size: float = 100000
    min_volume: float = 0.01
    max_volume: float = 100
    volume_step: float = 0.01
    spread: float = 0.0
    trade_mode: str = "full"


class PriceOut(StrictModel):
    symbol: str
    bid: float
    ask: float
    spread: float = 0.0
    time: str = ""


class CandleOut(StrictModel):
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


class PositionOut(StrictModel):
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
    opened_at: str = ""


class OrderOut(StrictModel):
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
    created_at: str = ""


class DealOut(StrictModel):
    deal_id: str
    position_id: str = ""
    symbol: str
    side: str
    volume: float
    price: float
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    comment: str = ""
    time: str = ""


class SnapshotOut(StrictModel):
    account: AccountInfoOut
    positions: List[PositionOut]
    orders: List[OrderOut]
    prices: List[PriceOut]
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Trading
# ---------------------------------------------------------------------------
class TradeRequest(StrictModel):
    action: Literal["market", "pending"] = "market"
    side: Side
    symbol: str = Field(min_length=1, max_length=64)
    volume: float = Field(gt=0, le=1000)
    stop_loss: Optional[float] = Field(default=None, ge=0)
    take_profit: Optional[float] = Field(default=None, ge=0)
    price: Optional[float] = Field(default=None, gt=0)
    order_type: Optional[Literal["limit", "stop"]] = None
    comment: str = Field(default="", max_length=64)
    client_order_id: Optional[str] = Field(default=None, max_length=64)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def _pending_needs_price(self):
        if self.action == "pending":
            if self.price is None:
                raise ValueError("price is required for pending orders")
            if self.order_type is None:
                raise ValueError("order_type is required for pending orders")
        return self


class ModifyPositionRequest(StrictModel):
    stop_loss: Optional[float] = Field(default=None, ge=0)
    take_profit: Optional[float] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _at_least_one(self):
        if self.stop_loss is None and self.take_profit is None:
            raise ValueError("provide stop_loss and/or take_profit")
        return self


class ClosePositionRequest(StrictModel):
    volume: Optional[float] = Field(default=None, gt=0, le=1000)
    comment: str = Field(default="", max_length=64)


class TradeOut(StrictModel):
    command_id: str
    status: Literal["EXECUTED", "QUEUED", "REJECTED"]
    account_id: str
    retcode: str = ""
    message: str = ""
    order_id: Optional[str] = None
    position_id: Optional[str] = None
    deal_id: Optional[str] = None
    price: Optional[float] = None
    volume: Optional[float] = None
    idempotent_replay: bool = False
    poll_url: Optional[str] = None


class CommandOut(StrictModel):
    command_id: str
    account_id: str
    kind: str
    status: str
    result: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Webhooks / audit / usage
# ---------------------------------------------------------------------------
WebhookEvent = Literal[
    "trade.executed", "trade.rejected", "position.opened", "position.closed",
    "order.placed", "order.cancelled", "account.updated",
    "account.connected", "account.disconnected", "*",
]


class WebhookCreateRequest(StrictModel):
    url: str = Field(min_length=8, max_length=1024)
    events: List[WebhookEvent] = Field(default_factory=lambda: ["*"])
    description: str = Field(default="", max_length=200)

    @field_validator("url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("url must be http(s)")
        return v


class WebhookOut(StrictModel):
    webhook_id: str
    url: str
    events: List[str]
    description: str = ""
    active: bool = True
    created_at: str = ""
    secret: Optional[str] = None  # returned once, on creation


class AuditOut(StrictModel):
    audit_id: str
    action: str
    account_id: str = ""
    key_id: str = ""
    detail: Dict[str, Any] = Field(default_factory=dict)
    ip: str = ""
    created_at: str = ""


class UsageOut(StrictModel):
    key_id: str
    tenant_id: str
    scopes: List[str]
    rate_limit_per_min: int
    requests_this_minute: int
    requests_total: int
    trades_total: int
    accounts: int
    reset_in_s: float


class Paginated(StrictModel):
    items: List[Any]
    count: int
