"""FastAPI application for the multi-tenant MT5 gateway.

Three planes, one process:

* **Public API** (`/v1/...`) — tenant-scoped REST for accounts, market
  data, positions, history and trading, plus `/v1/stream` (WebSocket).
* **Bridge plane** (`/internal/bridge/...`) — token-authenticated
  pull/push endpoints used by the per-account MQL5 bridge EA. Hidden from
  the public OpenAPI schema.
* **Ops plane** (`/health`, `/`) — liveness and a human landing page.

Run: ``python -m abutron.gateway`` (env-configured) or
``uvicorn abutron.gateway.app:create_app --factory``.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import (Depends, FastAPI, Header, Query, Request,
                     Response, WebSocket, WebSocketDisconnect)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from .auth import AuthContext, verify_bridge_token
from .errors import GatewayError, bad_request, not_found, unauthorized
from .models import (AccountCreateOut, AccountInfoOut, AccountOut, AuditOut,
                     BridgeTokenOut, CandleOut, ClosePositionRequest,
                     CommandOut, CreateAccountRequest, DealOut,
                     ModifyPositionRequest, OrderOut,
                     PositionOut, PriceOut, SnapshotOut, SymbolOut,
                     TradeOut, TradeRequest, UpdateAccountRequest,
                     UsageOut, WebhookCreateRequest, WebhookOut)
from .service import GatewayService
from .settings import GatewaySettings
from .store import GatewayStore, utc_now
from .streaming import StreamHub
from .webhooks import WebhookDispatcher

TAGS = [
    {"name": "Accounts", "description": "Provision and control MT5 accounts."},
    {"name": "Market data", "description": "Symbols, quotes and candles."},
    {"name": "Trading", "description": "Orders, positions and history."},
    {"name": "Streaming", "description": "Real-time WebSocket events."},
    {"name": "Webhooks", "description": "Signed event delivery."},
    {"name": "Ops", "description": "Health, usage and audit."},
    {"name": "Admin", "description": "Tenant bootstrap (operator only)."},
]

CHANNEL_FOR_EVENT = {
    "quote": "quotes", "candle": "candles", "position": "positions",
    "positions": "positions", "order": "orders", "orders": "orders",
    "trade": "trades", "account": "account", "command": "commands",
}


class AdminTenantRequest(BaseModel):
    name: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _error(status_code: int, code: str, message: str, request: Request,
           details: Optional[Dict[str, Any]] = None,
           headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message,
                           "request_id": _request_id(request),
                           "details": details or {}}},
        headers=headers or {})


def _api_key_from_headers(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key")


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _landing_html(settings: GatewaySettings, demo_key: Optional[str],
                  health: Dict[str, Any]) -> str:
    demo = ""
    if settings.demo_enabled and demo_key:
        demo = f"""
        <div class="card demo">
          <div class="kicker">Try it now</div>
          <p>This preview gateway is running in <b>demo mode</b> with a
          simulated MT5 account. Use this key in the Swagger
          <b>Authorize</b> dialog:</p>
          <code class="key">{demo_key}</code>
          <pre>curl -s {"" if True else ""}http://localhost:{settings.port}/v1/accounts \\
  -H "Authorization: Bearer {demo_key}"</pre>
        </div>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ABUTRON MT5 Gateway</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; font:15px/1.6 ui-sans-serif,system-ui,-apple-system,sans-serif;
       background:#0a0d14; color:#e6edf7; }}
.wrap {{ max-width:900px; margin:0 auto; padding:56px 24px 80px; }}
.kicker {{ font-size:12px; letter-spacing:.14em; text-transform:uppercase;
           color:#4fd1c5; font-weight:700; margin-bottom:14px; }}
h1 {{ font-size:42px; line-height:1.1; margin:0 0 14px; letter-spacing:-.02em; }}
h1 span {{ color:#4fd1c5; }}
p.lead {{ color:#9fb0c7; font-size:17px; max-width:62ch; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
         gap:14px; margin:30px 0; }}
.card {{ background:#111726; border:1px solid #1e2940; border-radius:14px;
         padding:20px; }}
.card h3 {{ margin:0 0 8px; font-size:15px; }}
.card p {{ margin:0; color:#93a4bd; font-size:13.5px; }}
.card.demo {{ border-color:#2a5f5a; background:#0e1f1e; margin:24px 0 30px; }}
code, pre {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
code.key {{ display:block; margin:10px 0; padding:12px; background:#08110f;
           border:1px solid #20443f; border-radius:8px; color:#7ef0d8;
           overflow-wrap:anywhere; font-size:13px; }}
pre {{ background:#080b12; border:1px solid #1a2438; padding:14px;
       border-radius:10px; overflow:auto; font-size:12.5px; color:#c6d4e6; }}
a {{ color:#4fd1c5; }}
.row {{ display:flex; gap:22px; flex-wrap:wrap; margin-top:26px;
        padding-top:20px; border-top:1px solid #1a2438; }}
.stat b {{ display:block; font-size:22px; color:#fff; }}
.stat {{ color:#8194ae; font-size:12px; }}
.links a {{ margin-right:18px; font-weight:600; }}
</style></head><body><div class="wrap">
<div class="kicker">ABUTRON FX · MetaTrader 5</div>
<h1>One <span>MT5 API</span> for every client account.</h1>
<p class="lead">A multi-tenant control plane for MetaTrader 5: provision
accounts under isolated API keys, read balances, prices, candles, positions
and deal history, execute and manage trades, and stream every change over a
WebSocket — with signed webhooks and a full audit trail.</p>

{demo}

<div class="grid">
  <div class="card"><h3>REST + WebSocket</h3><p>MetaApi-shaped endpoints,
  familiar MT5 vocabulary. Standard OpenAPI schema, any language.</p></div>
  <div class="card"><h3>One writer per account</h3><p>An account is either
  API-driven or APEX-automated — never both. No silent double execution.</p></div>
  <div class="card"><h3>Honest execution</h3><p>A trade is EXECUTED only on a
  transport ACK. Slow bridges return 202 + a command id, never a fake fill.</p></div>
  <div class="card"><h3>Tenant isolation</h3><p>Hashed keys, scopes, rate
  limits, per-tenant audit. Cross-tenant ids return 404, not 403.</p></div>
</div>

<pre>POST /v1/accounts                 provision an account
GET  /v1/accounts/{{id}}/information
GET  /v1/accounts/{{id}}/symbols/EURUSD/candles?timeframe=5m&amp;limit=100
POST /v1/accounts/{{id}}/trade     market or pending order
POST /v1/accounts/{{id}}/positions/{{pid}}/close
GET  /v1/accounts/{{id}}/history/deals
WS   /v1/stream?account_id={{id}}  quotes · positions · trades · commands</pre>

<div class="row">
  <div class="stat"><b>{health.get('accounts', 0)}</b>accounts</div>
  <div class="stat"><b>{health.get('connected_accounts', 0)}</b>connected</div>
  <div class="stat"><b>{health.get('stream', {}).get('published', 0)}</b>
    events published</div>
</div>

<div class="links row">
  <a href="/docs">Swagger UI</a>
  <a href="/redoc">ReDoc</a>
  <a href="/openapi.json">OpenAPI schema</a>
  <a href="/health">Health</a>
</div>
<p style="color:#5c6d85;font-size:12px;margin-top:26px">
  Bridge EA endpoints under <code>/internal/bridge/...</code> are intentionally
  absent from the public schema.</p>
</div></body></html>"""


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app(settings: Optional[GatewaySettings] = None,
               store: Optional[GatewayStore] = None) -> FastAPI:
    settings = settings or GatewaySettings.from_env()
    store = store or GatewayStore(settings.db_path)
    store.init()
    hub = StreamHub()
    webhooks = WebhookDispatcher(store, settings)
    service = GatewayService(store, settings, hub, webhooks)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service.start()
        if settings.demo_enabled:
            service.bootstrap_demo()
        task: Optional[asyncio.Task] = None
        if settings.webhooks_enabled and settings.webhook_worker:
            task = asyncio.create_task(webhooks.loop())
        try:
            yield
        finally:
            webhooks.stop()
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            service.close()

    app = FastAPI(
        title="ABUTRON MT5 Gateway",
        version="0.1.0",
        description=(
            "Multi-tenant MetaTrader 5 API bridge: REST + WebSocket, "
            "scoped API keys, per-account single-writer execution, signed "
            "webhooks and audit. Authenticate with "
            "`Authorization: Bearer abk_...` or `X-API-Key`."),
        lifespan=lifespan,
        openapi_tags=TAGS,
        contact={"name": "ABUTRON FX"},
    )
    app.state.settings = settings
    app.state.store = store
    app.state.service = service
    app.state.hub = hub
    app.state.webhooks = webhooks

    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins),
        allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
        expose_headers=["X-Request-Id", "X-RateLimit-Limit",
                        "X-RateLimit-Remaining", "X-RateLimit-Reset",
                        "Retry-After"])

    # ---- middleware ---------------------------------------------------
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = (request.headers.get("x-request-id")
                                    or f"req_{uuid.uuid4().hex[:16]}")
        request.state.rate_limit = None
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["X-Gateway-Time-Ms"] = \
            f"{(time.perf_counter() - started) * 1000:.1f}"
        limits = getattr(request.state, "rate_limit", None)
        if limits:
            limit, remaining, reset_in = limits
            response.headers["X-RateLimit-Limit"] = str(limit)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            response.headers["X-RateLimit-Reset"] = f"{reset_in:.0f}"
        return response

    # ---- error handlers ----------------------------------------------
    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError):
        headers = {}
        retry = exc.details.get("retry_after_s") if exc.details else None
        if retry:
            headers["Retry-After"] = str(int(max(1, round(float(retry)))))
        return _error(exc.status_code, exc.code, exc.message, request,
                      exc.details, headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request,
                                       exc: RequestValidationError):
        errors = []
        for err in exc.errors()[:10]:
            errors.append({"loc": [str(x) for x in err.get("loc", [])],
                           "msg": err.get("msg", ""),
                           "type": err.get("type", "")})
        return _error(422, "VALIDATION_ERROR", "request validation failed",
                      request, {"errors": errors})

    # ---- auth dependencies -------------------------------------------
    async def current_ctx(request: Request) -> AuthContext:
        raw = _api_key_from_headers(request)
        if not raw:
            raise unauthorized()
        return await run_in_threadpool(
            request.app.state.service.authenticate, raw, request)

    def scoped(scope: str):
        async def dep(ctx: AuthContext = Depends(current_ctx)) -> AuthContext:
            ctx.require(scope)
            return ctx
        return dep

    read_ctx = scoped("read")
    trade_ctx = scoped("trade")
    manage_ctx = scoped("manage")

    def idem_key(idempotency_key: Optional[str] = Header(
            default=None, alias="Idempotency-Key",
            description="Replay-safe key for a write. Reusing a key with a "
                        "different body returns 409.")) -> Optional[str]:
        if idempotency_key and len(idempotency_key) > 128:
            raise bad_request("IDEMPOTENCY_KEY_TOO_LONG",
                              "Idempotency-Key must be <= 128 characters")
        return idempotency_key

    # ==================================================================
    # Ops
    # ==================================================================
    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    def landing():
        if not settings.landing_page:
            return HTMLResponse("<h1>ABUTRON MT5 Gateway</h1>"
                                "<p><a href='/docs'>API docs</a></p>")
        return HTMLResponse(_landing_html(settings, settings.demo_api_key,
                                          service.health()))

    @app.get("/health", tags=["Ops"], summary="Liveness and connection counts")
    def health():
        return service.health()

    @app.get("/v1/me", tags=["Ops"], summary="Current tenant and key")
    def me(ctx: AuthContext = Depends(read_ctx)):
        return {"tenant_id": ctx.tenant_id, "key_id": ctx.key_id,
                "scopes": ctx.scopes,
                "rate_limit_per_min": ctx.rate_limit_per_min,
                "authenticated": True}

    @app.get("/v1/usage", tags=["Ops"], response_model=UsageOut)
    def usage(ctx: AuthContext = Depends(read_ctx)):
        return service.usage(ctx)

    @app.get("/v1/audit", tags=["Ops"], response_model=List[AuditOut],
             summary="Tenant audit trail (most recent first)")
    def audit(limit: int = Query(default=100, ge=1, le=500),
                    ctx: AuthContext = Depends(read_ctx)):
        return service.list_audit(ctx, limit)

    @app.post("/v1/admin/tenants", tags=["Admin"],
              summary="Mint a tenant + API key (requires X-Admin-Secret)")
    def admin_create_tenant(
            body: AdminTenantRequest,
            x_admin_secret: Optional[str] = Header(default=None)):
        if not settings.admin_secret:
            raise GatewayError(404, "NOT_FOUND", "endpoint disabled")
        if not x_admin_secret or not hmac.compare_digest(
                x_admin_secret, settings.admin_secret):
            raise unauthorized("invalid admin secret")
        tenant_id, api_key = service.create_tenant(body.name)
        return {"tenant_id": tenant_id, "api_key": api_key,
                "scopes": ["read", "trade", "manage"]}

    # ==================================================================
    # Accounts
    # ==================================================================
    @app.post("/v1/accounts", tags=["Accounts"], status_code=201,
              response_model=AccountCreateOut,
              summary="Provision an MT5 account")
    def create_account(body: CreateAccountRequest, request: Request,
                             ctx: AuthContext = Depends(manage_ctx),
                             idem: Optional[str] = Depends(idem_key)):
        return service.create_account(
            ctx, body, idem=idem, ip=_client_ip(request))

    @app.get("/v1/accounts", tags=["Accounts"],
             summary="List this tenant's accounts")
    def list_accounts(ctx: AuthContext = Depends(read_ctx)):
        items = service.list_accounts(ctx)
        return {"items": items, "count": len(items)}

    @app.get("/v1/accounts/{account_id}", tags=["Accounts"],
             response_model=AccountOut)
    def get_account(account_id: str,
                          ctx: AuthContext = Depends(read_ctx)):
        return service.get_account(ctx, account_id)

    @app.patch("/v1/accounts/{account_id}", tags=["Accounts"],
               response_model=AccountOut,
               summary="Kill switch, limits, control mode, symbol set")
    def update_account(account_id: str, body: UpdateAccountRequest,
                             request: Request,
                             ctx: AuthContext = Depends(manage_ctx)):
        return service.update_account(ctx, account_id, body,
                                      ip=_client_ip(request))

    @app.delete("/v1/accounts/{account_id}", tags=["Accounts"],
                summary="Revoke an account and its bridge token")
    def delete_account(account_id: str, request: Request,
                             ctx: AuthContext = Depends(manage_ctx)):
        return service.delete_account(ctx, account_id,
                                      ip=_client_ip(request))

    @app.post("/v1/accounts/{account_id}/bridge-token", tags=["Accounts"],
              response_model=BridgeTokenOut,
              summary="Rotate the bridge token (invalidates the old one)")
    def rotate_bridge_token(account_id: str, request: Request,
                                  ctx: AuthContext = Depends(manage_ctx)):
        return service.rotate_bridge_token(ctx, account_id,
                                           ip=_client_ip(request))

    # ==================================================================
    # Market data
    # ==================================================================
    @app.get("/v1/accounts/{account_id}/information", tags=["Market data"],
             response_model=AccountInfoOut)
    def account_information(account_id: str,
                                  ctx: AuthContext = Depends(read_ctx)):
        return service.account_information(ctx, account_id)

    @app.get("/v1/accounts/{account_id}/symbols", tags=["Market data"],
             response_model=List[SymbolOut])
    def symbols(account_id: str, ctx: AuthContext = Depends(read_ctx)):
        return service.symbols(ctx, account_id)

    @app.get("/v1/accounts/{account_id}/symbols/{symbol}", tags=["Market data"],
             response_model=SymbolOut)
    def symbol(account_id: str, symbol: str,
                     ctx: AuthContext = Depends(read_ctx)):
        return service.symbol(ctx, account_id, symbol)

    @app.get("/v1/accounts/{account_id}/symbols/{symbol}/price",
             tags=["Market data"], response_model=PriceOut)
    def price(account_id: str, symbol: str,
                    ctx: AuthContext = Depends(read_ctx)):
        return service.price(ctx, account_id, symbol)

    @app.get("/v1/accounts/{account_id}/symbols/{symbol}/candles",
             tags=["Market data"], response_model=List[CandleOut])
    def candles(
            account_id: str, symbol: str,
            timeframe: str = Query(default="5m"),
            start_time: Optional[str] = Query(default=None, alias="startTime"),
            limit: int = Query(default=100, ge=1, le=1000),
            ctx: AuthContext = Depends(read_ctx)):
        return service.candles(ctx, account_id, symbol, timeframe,
                               start_time, limit)

    # ==================================================================
    # Terminal state / history
    # ==================================================================
    @app.get("/v1/accounts/{account_id}/snapshot", tags=["Trading"],
             response_model=SnapshotOut,
             summary="Account + positions + orders + prices in one call")
    def snapshot(account_id: str,
                       ctx: AuthContext = Depends(read_ctx)):
        return service.snapshot(ctx, account_id)

    @app.get("/v1/accounts/{account_id}/positions", tags=["Trading"],
             response_model=List[PositionOut])
    def positions(account_id: str,
                        ctx: AuthContext = Depends(read_ctx)):
        return service.positions(ctx, account_id)

    @app.get("/v1/accounts/{account_id}/orders", tags=["Trading"],
             response_model=List[OrderOut])
    def orders(account_id: str, ctx: AuthContext = Depends(read_ctx)):
        return service.orders(ctx, account_id)

    @app.get("/v1/accounts/{account_id}/history/deals", tags=["Trading"],
             response_model=List[DealOut])
    def history_deals(
            account_id: str,
            start_time: Optional[str] = Query(default=None, alias="from"),
            end_time: Optional[str] = Query(default=None, alias="to"),
            limit: int = Query(default=100, ge=1, le=1000),
            ctx: AuthContext = Depends(read_ctx)):
        return service.deals(ctx, account_id, start_time, end_time, limit)

    @app.get("/v1/accounts/{account_id}/history/orders", tags=["Trading"],
             response_model=List[OrderOut])
    def history_orders(
            account_id: str,
            start_time: Optional[str] = Query(default=None, alias="from"),
            end_time: Optional[str] = Query(default=None, alias="to"),
            limit: int = Query(default=100, ge=1, le=1000),
            ctx: AuthContext = Depends(read_ctx)):
        return service.history_orders(ctx, account_id, start_time, end_time,
                                      limit)

    # ==================================================================
    # Trading
    # ==================================================================
    @app.post("/v1/accounts/{account_id}/trade", tags=["Trading"],
              response_model=TradeOut,
              responses={202: {"description": "Queued for a bridge"},
                         422: {"description": "Broker/transport rejected"}},
              summary="Place a market or pending order")
    def trade(account_id: str, body: TradeRequest, request: Request,
                    response: Response,
                    ctx: AuthContext = Depends(trade_ctx),
                    idem: Optional[str] = Depends(idem_key)):
        code, payload = service.trade(ctx, account_id, body, idem,
                                      _client_ip(request))
        response.status_code = code
        return payload

    @app.post("/v1/accounts/{account_id}/positions/{position_id}/modify",
              tags=["Trading"], response_model=TradeOut,
              summary="Change SL/TP on an open position")
    def modify_position(account_id: str, position_id: str,
                              body: ModifyPositionRequest, request: Request,
                              response: Response,
                              ctx: AuthContext = Depends(trade_ctx),
                              idem: Optional[str] = Depends(idem_key)):
        code, payload = service.modify_position(
            ctx, account_id, position_id, body, idem, _client_ip(request))
        response.status_code = code
        return payload

    @app.post("/v1/accounts/{account_id}/positions/{position_id}/close",
              tags=["Trading"], response_model=TradeOut,
              summary="Close a position, fully or partially")
    def close_position(account_id: str, position_id: str,
                             request: Request, response: Response,
                             body: Optional[ClosePositionRequest] = None,
                             ctx: AuthContext = Depends(trade_ctx),
                             idem: Optional[str] = Depends(idem_key)):
        body = body or ClosePositionRequest()
        code, payload = service.close_position(
            ctx, account_id, position_id, body, idem, _client_ip(request))
        response.status_code = code
        return payload

    @app.post("/v1/accounts/{account_id}/orders/{order_id}/cancel",
              tags=["Trading"], response_model=TradeOut,
              summary="Cancel a pending order")
    def cancel_order(account_id: str, order_id: str, request: Request,
                           response: Response,
                           ctx: AuthContext = Depends(trade_ctx),
                           idem: Optional[str] = Depends(idem_key)):
        code, payload = service.cancel_order(ctx, account_id, order_id, idem,
                                             _client_ip(request))
        response.status_code = code
        return payload

    @app.get("/v1/accounts/{account_id}/commands/{command_id}",
             tags=["Trading"], response_model=CommandOut,
             summary="Poll a queued command (bridge-backed accounts)")
    def command_status(account_id: str, command_id: str,
                             ctx: AuthContext = Depends(read_ctx)):
        return service.command_status(ctx, account_id, command_id)

    # ==================================================================
    # Webhooks
    # ==================================================================
    @app.post("/v1/webhooks", tags=["Webhooks"], status_code=201,
              response_model=WebhookOut,
              summary="Subscribe to signed event deliveries")
    def create_webhook(body: WebhookCreateRequest, request: Request,
                             ctx: AuthContext = Depends(manage_ctx)):
        return service.create_webhook(ctx, body.url, body.events,
                                      body.description, _client_ip(request))

    @app.get("/v1/webhooks", tags=["Webhooks"],
             response_model=List[WebhookOut])
    def list_webhooks(ctx: AuthContext = Depends(manage_ctx)):
        return service.list_webhooks(ctx)

    @app.delete("/v1/webhooks/{webhook_id}", tags=["Webhooks"])
    def delete_webhook(webhook_id: str, request: Request,
                             ctx: AuthContext = Depends(manage_ctx)):
        return service.delete_webhook(ctx, webhook_id, _client_ip(request))

    # ==================================================================
    # Streaming
    # ==================================================================
    @app.websocket("/v1/stream")
    async def stream(websocket: WebSocket, account_id: str = Query(...),
                     api_key: Optional[str] = Query(default=None),
                     channels: Optional[str] = Query(default=None),
                     symbols: Optional[str] = Query(default=None)):
        token = api_key or websocket.headers.get("x-api-key")
        auth_header = websocket.headers.get("authorization", "")
        if not token and auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
        try:
            ctx = service.authenticate(token or "")
            ctx.require("read")
            service._account(ctx, account_id)  # tenant-scoped existence check
        except GatewayError as exc:
            # 4401/4404 close codes: never accept the socket for bad auth.
            await websocket.close(code=4401 if exc.status_code == 401
                                  else 4404, reason=exc.code)
            return

        await websocket.accept()
        sub_id, queue = hub.subscribe()
        wanted = set((channels or "quotes,positions,orders,trades,account,"
                                "commands").split(","))
        wanted = {c.strip() for c in wanted if c.strip()}
        symbol_filter = {s.strip().upper() for s in (symbols or "").split(",")
                         if s.strip()}

        async def send(payload: Dict[str, Any]) -> None:
            await websocket.send_text(json.dumps(payload, default=str))

        try:
            await send({"type": "connected", "account_id": account_id,
                        "channels": sorted(wanted),
                        "sequence": 0, "timestamp": utc_now()})
            if "account" in wanted:
                info = await asyncio.to_thread(
                    service.account_information, ctx, account_id)
                await send({"type": "account", "account_id": account_id,
                            "data": info, "timestamp": utc_now()})
            if "positions" in wanted or "orders" in wanted:
                snap = await asyncio.to_thread(
                    service.snapshot, ctx, account_id)
                await send({"type": "snapshot", "account_id": account_id,
                            "data": snap, "timestamp": utc_now()})

            async def receiver():
                while True:
                    raw = await websocket.receive_text()
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        await send({"type": "error", "code": "BAD_JSON"})
                        continue
                    kind = message.get("type")
                    if kind == "ping":
                        await send({"type": "pong", "timestamp": utc_now()})
                    elif kind == "subscribe":
                        wanted.update(message.get("channels") or [])
                        if message.get("symbols"):
                            symbol_filter.update(
                                s.upper() for s in message["symbols"])
                        await send({"type": "subscribed",
                                    "channels": sorted(wanted)})
                    elif kind == "unsubscribe":
                        wanted.difference_update(message.get("channels") or [])
                        await send({"type": "unsubscribed",
                                    "channels": sorted(wanted)})
                    else:
                        await send({"type": "error",
                                    "code": "UNKNOWN_MESSAGE_TYPE"})

            async def sender():
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=20)
                    except asyncio.TimeoutError:
                        await send({"type": "heartbeat",
                                    "timestamp": utc_now()})
                        continue
                    if event.get("account_id") != account_id:
                        continue
                    channel = CHANNEL_FOR_EVENT.get(event.get("type", ""), "")
                    if channel and channel not in wanted:
                        continue
                    if symbol_filter and channel in ("quotes", "candles"):
                        symbol = str(
                            event.get("data", {}).get("symbol", "")).upper()
                        if symbol and symbol not in symbol_filter:
                            continue
                    await send(event)

            recv_task = asyncio.create_task(receiver())
            send_task = asyncio.create_task(sender())
            done, pending = await asyncio.wait(
                {recv_task, send_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            for task in done:
                with contextlib.suppress(asyncio.CancelledError):
                    task.result()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            hub.unsubscribe(sub_id)

    # ==================================================================
    # Bridge plane (no tenant key; per-account bridge token)
    # ==================================================================
    def _bridge_account(request_or_ws, connection_id: str,
                        token: Optional[str]) -> Dict[str, Any]:
        account = store.get_account_any(connection_id)
        if account is None or account["status"] == "DELETED" or \
                account["market_data_mode"] != "bridge":
            raise not_found("account")
        presented = token or ""
        if not verify_bridge_token(settings.bridge_secret, connection_id,
                                   account["bridge_salt"], presented):
            raise GatewayError(401, "BRIDGE_UNAUTHORIZED",
                              "invalid bridge token")
        return account

    def _bridge_token(headers_request: Any) -> Optional[str]:
        auth = headers_request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return headers_request.headers.get("x-bridge-token")

    @app.get("/internal/bridge/{connection_id}/handshake",
             include_in_schema=False)
    def bridge_handshake(connection_id: str, request: Request):
        account = _bridge_account(request, connection_id,
                                  _bridge_token(request))
        return service.bridge_handshake(account)

    @app.post("/internal/bridge/{connection_id}/state",
              include_in_schema=False)
    async def bridge_state(connection_id: str, request: Request):
        account = _bridge_account(request, connection_id,
                                  _bridge_token(request))
        try:
            body = await request.json()
        except ValueError:
            raise bad_request("BAD_JSON", "state body must be JSON")
        return await run_in_threadpool(service.apply_bridge_state, account, body)

    @app.get("/internal/bridge/{connection_id}/commands",
             include_in_schema=False)
    async def bridge_commands(connection_id: str, request: Request,
                              wait: int = Query(default=0, ge=0, le=60),
                              limit: int = Query(default=10, ge=1, le=50),
                              format: str = Query(default="json")):
        account = _bridge_account(request, connection_id,
                                  _bridge_token(request))
        wait = min(wait, settings.bridge_poll_max_s)
        deadline = time.monotonic() + wait
        commands: List[Dict[str, Any]] = []
        while True:
            commands = await run_in_threadpool(
                service.pull_bridge_commands, account, limit)
            if commands or wait == 0 or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.25)
        if format == "text":
            lines = []
            for command in commands:
                fields = {"command_id": command["command_id"],
                          "kind": command["kind"]}
                for key, value in (command.get("payload") or {}).items():
                    if value is None:
                        continue
                    text = str(value).replace(";", ",").replace("\n", " ")
                    fields[key] = text
                lines.append(";".join(f"{k}={v}" for k, v in fields.items()))
            return PlainTextResponse("\n".join(lines),
                                     media_type="text/plain")
        return {"commands": commands, "count": len(commands)}

    @app.post("/internal/bridge/{connection_id}/commands/{command_id}/result",
              include_in_schema=False)
    async def bridge_result(connection_id: str, command_id: str,
                            request: Request):
        account = _bridge_account(request, connection_id,
                                  _bridge_token(request))
        try:
            body = await request.json()
        except ValueError:
            raise bad_request("BAD_JSON", "result body must be JSON")
        if not isinstance(body, dict):
            raise bad_request("BAD_JSON", "result body must be an object")
        return await run_in_threadpool(
            service.complete_bridge_command, account, command_id, body)

    return app

