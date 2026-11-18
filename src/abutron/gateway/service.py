"""Gateway orchestration: tenancy, accounts, trading, streaming, audit.

Invariants this layer enforces (independent of transport):

1. **Tenant isolation** — every account lookup is scoped by the tenant on
   the API key; a foreign id is a 404, never a 403.
2. **One writer per account** — API trading requires ``control_mode=api``.
   An account that APEX automation currently drives is rejected with
   ``AUTOMATION_WRITER_ACTIVE`` instead of double-writing.
3. **Honest outcomes** — an accepted trade means a transport reported an
   execution. A slow bridge yields 202 + command id, never a fabricated ACK.
4. **Idempotent writes** — ``Idempotency-Key`` replays the first response;
   reusing a key with a different body is a 409.
5. **Every mutation is audited**, and optionally webhooked.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .auth import (ApiKeyService, AuthContext, RateLimiter,
                   derive_bridge_token, split_api_key)
from .errors import GatewayError, bad_request, conflict, not_found
from .models import (ClosePositionRequest, CreateAccountRequest,
                     ModifyPositionRequest, TradeRequest,
                     UpdateAccountRequest)
from .settings import GatewaySettings
from .store import GatewayStore, utc_now
from .streaming import StreamHub
from .transports.base import (Mt5Transport, TransportError,
                              normalize_volume, resolve_symbol)
from .transports.registry import build_transport
from .webhooks import WebhookDispatcher


class GatewayService:
    def __init__(self, store: GatewayStore, settings: GatewaySettings,
                 hub: Optional[StreamHub] = None,
                 webhooks: Optional[WebhookDispatcher] = None):
        self.store = store
        self.settings = settings
        self.hub = hub or StreamHub()
        self.webhooks = webhooks or WebhookDispatcher(store, settings)
        self.keys = ApiKeyService(store, settings)
        self.limiter = RateLimiter()
        self._transports: Dict[str, Mt5Transport] = {}
        self._sequences: Dict[str, int] = {}
        self._lock = threading.RLock()
        self._started_at = time.time()

    # ==================================================================
    # Lifecycle
    # ==================================================================
    def start(self) -> None:
        try:
            self.adopt_engine_accounts()
        except Exception:
            # Adoption is an enhancement, never a start-up blocker. A missing
            # or locked engine database must not take the API down.
            pass
        for account in self.store.list_accounts():
            try:
                self._transport(account)
                self._sequences[account["connection_id"]] = int(
                    account.get("state_sequence") or 0)
            except Exception:
                continue

    def adopt_engine_accounts(self) -> List[Dict[str, Any]]:
        """Mirror engine-managed accounts into the API as read-only.

        The engine is the single writer; this method only creates/updates
        API-visible account rows and marks them ``market_data_mode=engine``
        + ``control_mode=read_only`` + ``trading_enabled=0``. Accounts are
        matched by ``engine_connection_id``, so repeated boots are
        idempotent and a renamed terminal keeps its API id.
        """
        if not self.settings.adopt_engine_accounts:
            return []
        try:
            from ..db import engine_db
            engine_accounts = engine_db.list_accounts()
        except Exception:
            return []
        if not engine_accounts:
            return []
        tenant_id = self.settings.engine_tenant_id
        self.store.ensure_tenant(tenant_id, self.settings.engine_tenant_name)

        existing = self.store.list_accounts(include_deleted=True)
        by_engine = {a["engine_connection_id"]: a for a in existing
                     if a.get("engine_connection_id")}
        by_login = {(int(a["login"]), (a["server"] or "").lower())
                    for a in existing}
        adopted: List[Dict[str, Any]] = []
        for row in engine_accounts:
            engine_cid = str(row.get("connection_id") or "")
            if not engine_cid:
                continue
            symbols = [s for s in str(row.get("symbols") or "").split(",")
                       if s.strip()]
            login = int(row.get("mt5_login") or 0)
            server = str(row.get("server") or "")
            name = str(row.get("display_name") or "") or \
                f"{login} @ {server}"
            current = by_engine.get(engine_cid)
            if current is not None:
                if current["status"] == "DELETED":
                    continue
                patch: Dict[str, Any] = {"status": "CONNECTED"}
                if symbols:
                    patch["symbols"] = ",".join(symbols)
                if row.get("display_name"):
                    patch["name"] = name
                if server:
                    patch["server"] = server
                if row.get("broker"):
                    patch["broker"] = str(row["broker"])
                updated = self.store.update_account(
                    current["connection_id"], **patch)
                if updated is not None:
                    adopted.append(updated)
                    # Make sure a transport exists after an in-process restart.
                    self._transport(updated)
                continue
            # An account already registered explicitly under the same login
            # is not re-adopted: the explicit row wins.
            if login and (login, server.lower()) in by_login:
                continue
            gateway_cid = ("eng_" + engine_cid)[:64]
            suffix = 0
            while self.store.get_account_any(gateway_cid) is not None:
                suffix += 1
                gateway_cid = (f"eng_{engine_cid[:40]}_{suffix}")[:64]
            account = self.store.create_account(
                connection_id=gateway_cid,
                tenant_id=tenant_id,
                name=name,
                broker=str(row.get("broker") or ""),
                server=server,
                login=login,
                platform="mt5",
                environment=str(row.get("environment") or "demo"),
                control_mode="read_only",
                status="CONNECTED",
                trading_enabled=0,
                market_data_mode="engine",
                engine_connection_id=engine_cid,
                symbols=",".join(symbols),
                max_lot=0.0,
                max_open_positions=0,
                daily_loss_limit_usd=0.0,
                symbol_allowlist="",
                bridge_salt="",
                state_json="{}",
                state_sequence=0,
                last_error="",
            )
            self._transport(account)
            adopted.append(account)
            by_engine[engine_cid] = account
            if login:
                by_login.add((login, server.lower()))
        return adopted

    def bootstrap_demo(self) -> Optional[str]:
        """Idempotently create the demo tenant, key and preview account."""
        raw = self.settings.demo_api_key
        parsed = split_api_key(raw)
        if parsed is None:
            return None
        prefix, secret = parsed
        if self.store.get_api_key_by_prefix(prefix) is not None:
            return None
        tenant_id = self.store.create_tenant("Demo tenant")
        self.keys.register_key(
            raw, tenant_id, label="demo",
            scopes=["read", "trade", "manage"], rate_limit_per_min=10_000)
        ctx = AuthContext(tenant_id=tenant_id, key_id="key_demo",
                          scopes=["read", "trade", "manage"],
                          rate_limit_per_min=10_000)
        try:
            self.create_account(
                ctx,
                CreateAccountRequest(
                    login=100_000_001, server="ABUTRON-Demo",
                    broker="abutron", name="Preview account",
                    environment="demo", market_data_mode="simulated",
                    symbols=list(self.settings.default_symbols),
                    max_lot=5.0, max_open_positions=20),
                idem=None, ip="bootstrap", actor="bootstrap")
        except GatewayError:
            pass
        return tenant_id

    def close(self) -> None:
        with self._lock:
            transports = list(self._transports.values())
            self._transports.clear()
        for transport in transports:
            try:
                transport.close()
            except Exception:
                pass

    # ==================================================================
    # Auth
    # ==================================================================
    def authenticate(self, raw_key: str, request=None) -> AuthContext:
        ctx, _row = self.keys.authenticate(raw_key)
        allowed, remaining, reset_in = self.limiter.hit(
            ctx.key_id, ctx.rate_limit_per_min)
        if request is not None:
            request.state.rate_limit = (ctx.rate_limit_per_min, remaining,
                                        reset_in)
        if not allowed:
            raise GatewayError(
                429, "RATE_LIMITED", "rate limit exceeded",
                {"limit": ctx.rate_limit_per_min,
                 "retry_after_s": round(reset_in, 1)})
        return ctx

    def check_trade_rate(self, ctx: AuthContext) -> None:
        allowed, remaining, reset_in = self.limiter.hit(
            ctx.key_id + ":trade", self.settings.trade_rate_limit_per_min,
            trade=True)
        if not allowed:
            raise GatewayError(
                429, "TRADE_RATE_LIMITED",
                "trade rate limit exceeded",
                {"limit": self.settings.trade_rate_limit_per_min,
                 "retry_after_s": round(reset_in, 1)})

    # ==================================================================
    # Tenancy helpers (used by the bootstrap/admin surface)
    # ==================================================================
    def create_tenant(self, name: str, *, rate_limit_per_min: Optional[int] = None
                      ) -> Tuple[str, str]:
        tenant_id = self.store.create_tenant(name)
        raw, _key_id = self.keys.create_key(
            tenant_id, label="default", scopes=["read", "trade", "manage"],
            rate_limit_per_min=rate_limit_per_min)
        return tenant_id, raw

    # ==================================================================
    # Transports
    # ==================================================================
    def _transport(self, account: Dict[str, Any]) -> Mt5Transport:
        cid = account["connection_id"]
        with self._lock:
            existing = self._transports.get(cid)
            if existing is not None:
                return existing
            transport = build_transport(account, self.store, self.settings,
                                        sink=self.hub.publish)
            self._transports[cid] = transport
            self._sequences.setdefault(
                cid, int(account.get("state_sequence") or 0))
            return transport

    def _persist(self, account: Dict[str, Any], transport: Mt5Transport) -> None:
        if transport.kind != "simulated":
            return
        with self._lock:
            seq = self._sequences.get(account["connection_id"], 0) + 1
            self._sequences[account["connection_id"]] = seq
        try:
            self.store.save_state(account["connection_id"],
                                  transport.dump_state(), seq,  # type: ignore[attr-defined]
                                  status="CONNECTED")
        except Exception:
            pass

    # ==================================================================
    # Accounts
    # ==================================================================
    def _account(self, ctx: AuthContext, account_id: str) -> Dict[str, Any]:
        account = self.store.get_account(ctx.tenant_id, account_id)
        if account is None or account["status"] == "DELETED":
            raise not_found("account")
        return account

    def _display_status(self, account: Dict[str, Any],
                        transport: Mt5Transport) -> str:
        if account["status"] == "DELETED":
            return "DELETED"
        if transport.kind == "simulated":
            return "CONNECTED"
        if transport.connected:
            return "CONNECTED"
        if account["status"] == "PROVISIONING" and not account["last_seen_at"]:
            return "PROVISIONING"
        return "DISCONNECTED"

    def account_out(self, account: Dict[str, Any]) -> Dict[str, Any]:
        transport = self._transport(account)
        return {
            "account_id": account["connection_id"],
            "tenant_id": account["tenant_id"],
            "name": account["name"] or f"{account['login']} @ {account['server']}",
            "broker": account["broker"], "server": account["server"],
            "login": account["login"], "platform": account["platform"],
            "environment": account["environment"],
            "status": self._display_status(account, transport),
            "control_mode": account["control_mode"],
            "trading_enabled": bool(account["trading_enabled"]),
            "market_data_mode": account["market_data_mode"],
            "symbols": account["symbols"],
            "max_lot": account["max_lot"],
            "max_open_positions": account["max_open_positions"],
            "daily_loss_limit_usd": account["daily_loss_limit_usd"],
            "symbol_allowlist": account["symbol_allowlist"],
            "created_at": account["created_at"],
            "updated_at": account["updated_at"],
            "last_seen_at": account["last_seen_at"],
            "last_error": account["last_error"],
        }

    def _bridge_info(self, account: Dict[str, Any]) -> Dict[str, Any]:
        cid = account["connection_id"]
        token = derive_bridge_token(
            self.settings.bridge_secret, cid, account["bridge_salt"])
        base = f"/internal/bridge/{cid}"
        return {
            "transport": "pull-http",
            "token": token,
            "state_url": f"{base}/state",
            "commands_url": f"{base}/commands",
            "result_url_template": f"{base}/commands/{{command_id}}/result",
            "handshake_url": f"{base}/handshake",
        }

    def create_account(self, ctx: AuthContext, req: CreateAccountRequest, *,
                       idem: Optional[str], ip: str,
                       actor: Optional[str] = None) -> Dict[str, Any]:
        if self.store.count_accounts(ctx.tenant_id) >= \
                self.settings.max_accounts_per_tenant:
            raise conflict("ACCOUNT_QUOTA_EXCEEDED",
                           "account quota reached for this tenant")
        mode = req.market_data_mode or self.settings.default_market_data_mode
        if mode == "simulated" and not self.settings.allow_simulated_accounts:
            raise GatewayError(403, "SIMULATED_DISABLED",
                              "simulated accounts are disabled")
        symbols = req.symbols or list(self.settings.default_symbols)
        if mode == "bridge" and not symbols:
            raise bad_request("SYMBOLS_REQUIRED",
                              "bridge accounts require at least one symbol")
        if req.control_mode == "api":
            self._assert_no_automation_writer(req.login, req.server)

        connection_id = "acc_" + secrets.token_hex(6)
        while self.store.get_account_any(connection_id) is not None:
            connection_id = "acc_" + secrets.token_hex(6)
        salt = secrets.token_hex(16) if mode == "bridge" else ""
        account = self.store.create_account(
            connection_id=connection_id, tenant_id=ctx.tenant_id,
            name=req.name or f"{req.login} @ {req.server}",
            broker=req.broker, server=req.server, login=req.login,
            platform="mt5", environment=req.environment,
            control_mode=req.control_mode,
            status="PROVISIONING" if mode == "bridge" else "CONNECTED",
            trading_enabled=1, market_data_mode=mode,
            symbols=",".join(symbols),
            max_lot=req.max_lot,
            max_open_positions=req.max_open_positions,
            daily_loss_limit_usd=req.daily_loss_limit_usd,
            symbol_allowlist=",".join(req.symbol_allowlist or []),
            bridge_salt=salt, state_json="{}", state_sequence=0,
            last_error="")
        transport = self._transport(account)
        body = self.account_out(account)
        body["credentials_accepted"] = req.password is not None
        if mode == "bridge":
            body["bridge"] = self._bridge_info(account)
        self._audit(ctx, "account.created", connection_id, {
            "login": req.login, "server": req.server, "mode": mode,
            "password_received": req.password is not None,
            "actor": actor or "api"}, ip)
        self._event(ctx.tenant_id, "account.updated", connection_id, body)
        _ = transport
        return body

    def list_accounts(self, ctx: AuthContext) -> List[Dict[str, Any]]:
        return [self.account_out(a)
                for a in self.store.list_accounts(ctx.tenant_id)]

    def get_account(self, ctx: AuthContext, account_id: str) -> Dict[str, Any]:
        return self.account_out(self._account(ctx, account_id))

    def update_account(self, ctx: AuthContext, account_id: str,
                       req: UpdateAccountRequest, *, ip: str) -> Dict[str, Any]:
        account = self._account(ctx, account_id)
        patch = req.model_dump(exclude_none=True)
        if not patch:
            return self.account_out(account)
        if account.get("engine_connection_id"):
            if patch.get("control_mode") == "api" or patch.get(
                    "trading_enabled") is True:
                raise conflict(
                    "ENGINE_MANAGED_ACCOUNT",
                    "this account is mirrored read-only from the APEX engine; "
                    "enable trading in the platform, not through the API")
        if patch.get("control_mode") == "api":
            self._assert_no_automation_writer(account["login"],
                                              account["server"])
        if "symbols" in patch:
            if not patch["symbols"]:
                raise bad_request("SYMBOLS_REQUIRED",
                                  "an account must publish at least one symbol")
            if account["market_data_mode"] == "simulated":
                transport = self._transport(account)
                if transport.positions():
                    raise conflict(
                        "SYMBOLS_LOCKED",
                        "close open positions before changing the symbol set")
                snapshot = transport.dump_state()  # type: ignore[attr-defined]
                transport.close()
                with self._lock:
                    self._transports.pop(account_id, None)
                patch["symbols"] = ",".join(patch["symbols"])
                account = self.store.update_account(account_id, **patch)
                rebuilt = self._transport(account)  # type: ignore[arg-type]
                rebuilt.load_state(snapshot)  # type: ignore[attr-defined]
            else:
                patch["symbols"] = ",".join(patch["symbols"])
        if "symbol_allowlist" in patch:
            patch["symbol_allowlist"] = ",".join(patch["symbol_allowlist"])
        if "trading_enabled" in patch:
            patch["trading_enabled"] = 1 if patch["trading_enabled"] else 0
        account = self.store.update_account(account_id, **patch)
        body = self.account_out(account)  # type: ignore[arg-type]
        self._audit(ctx, "account.updated", account_id, patch, ip)
        self._event(ctx.tenant_id, "account.updated", account_id, body)
        if "trading_enabled" in patch:
            self.hub.publish({"type": "account", "account_id": account_id,
                              "data": body})
        return body

    def delete_account(self, ctx: AuthContext, account_id: str, *,
                       ip: str) -> Dict[str, Any]:
        account = self._account(ctx, account_id)
        with self._lock:
            transport = self._transports.pop(account_id, None)
        if transport is not None:
            transport.close()
        self.store.delete_account(account_id)
        self._audit(ctx, "account.deleted", account_id,
                    {"login": account["login"]}, ip)
        self._event(ctx.tenant_id, "account.updated", account_id,
                    {"account_id": account_id, "status": "DELETED"})
        return {"account_id": account_id, "status": "DELETED"}

    def rotate_bridge_token(self, ctx: AuthContext, account_id: str, *,
                            ip: str) -> Dict[str, Any]:
        account = self._account(ctx, account_id)
        if account["market_data_mode"] != "bridge":
            raise bad_request("NOT_A_BRIDGE_ACCOUNT",
                              "only bridge accounts have a bridge token")
        account = self.store.update_account(
            account_id, bridge_salt=secrets.token_hex(16))
        self._audit(ctx, "account.bridge_token_rotated", account_id, {}, ip)
        return {"account_id": account_id,
                "bridge": self._bridge_info(account)}  # type: ignore[arg-type]

    # ==================================================================
    # Reads
    # ==================================================================
    def _read(self, ctx: AuthContext, account_id: str, fn):
        account = self._account(ctx, account_id)
        transport = self._transport(account)
        try:
            return fn(transport)
        except TransportError as exc:
            raise self._transport_error(exc) from exc

    @staticmethod
    def _transport_error(exc: TransportError) -> GatewayError:
        mapping = {
            "SYMBOL_NOT_FOUND": 404, "INVALID_TIMEFRAME": 400,
            "TIMEFRAME_NOT_AVAILABLE": 400, "READ_ONLY_ACCOUNT": 409,
            "PRICE_UNAVAILABLE": 503, "INVALID_STATE": 400,
        }
        return GatewayError(mapping.get(exc.code, 503), exc.code, exc.message)

    def account_information(self, ctx: AuthContext,
                            account_id: str) -> Dict[str, Any]:
        return self._read(ctx, account_id,
                          lambda t: t.account_information().to_dict())

    def symbols(self, ctx: AuthContext, account_id: str) -> List[Dict[str, Any]]:
        return self._read(ctx, account_id,
                          lambda t: [s.to_dict() for s in t.symbols()])

    def symbol(self, ctx: AuthContext, account_id: str,
               symbol: str) -> Dict[str, Any]:
        return self._read(ctx, account_id,
                          lambda t: t.symbol(symbol).to_dict())

    def price(self, ctx: AuthContext, account_id: str,
              symbol: str) -> Dict[str, Any]:
        return self._read(ctx, account_id,
                          lambda t: t.price(symbol).to_dict())

    def candles(self, ctx: AuthContext, account_id: str, symbol: str,
                timeframe: str, start_time: Optional[str],
                limit: int) -> List[Dict[str, Any]]:
        return self._read(
            ctx, account_id,
            lambda t: [c.to_dict() for c in t.candles(
                symbol, timeframe, start_time, limit)])

    def positions(self, ctx: AuthContext,
                  account_id: str) -> List[Dict[str, Any]]:
        return self._read(ctx, account_id,
                          lambda t: [p.to_dict() for p in t.positions()])

    def orders(self, ctx: AuthContext, account_id: str) -> List[Dict[str, Any]]:
        return self._read(ctx, account_id,
                          lambda t: [o.to_dict() for o in t.orders()])

    def deals(self, ctx: AuthContext, account_id: str,
              start_time: Optional[str], end_time: Optional[str],
              limit: int) -> List[Dict[str, Any]]:
        return self._read(
            ctx, account_id,
            lambda t: [d.to_dict() for d in t.deals(
                start_time, end_time, limit)])

    def history_orders(self, ctx: AuthContext, account_id: str,
                       start_time: Optional[str], end_time: Optional[str],
                       limit: int) -> List[Dict[str, Any]]:
        return self._read(
            ctx, account_id,
            lambda t: [o.to_dict() for o in t.history_orders(
                start_time, end_time, limit)])

    def snapshot(self, ctx: AuthContext, account_id: str) -> Dict[str, Any]:
        account = self._account(ctx, account_id)
        transport = self._transport(account)
        info = transport.account_information()
        positions = [p.to_dict() for p in transport.positions()]
        orders = [o.to_dict() for o in transport.orders()]
        prices = []
        for spec in transport.symbols():
            try:
                prices.append(transport.price(spec.symbol).to_dict())
            except TransportError:
                continue
        return {"account": info.to_dict(), "positions": positions,
                "orders": orders, "prices": prices,
                "updated_at": info.updated_at}

    # ==================================================================
    # Trading
    # ==================================================================
    def _guard_trade(self, account: Dict[str, Any],
                     transport: Mt5Transport) -> None:
        if account.get("engine_connection_id"):
            raise conflict(
                "ENGINE_MANAGED_ACCOUNT",
                "this account is traded by the APEX engine and is exposed "
                "read-only through the API")
        if account["control_mode"] != "api":
            raise conflict("ACCOUNT_READ_ONLY",
                           "account control_mode is read_only")
        if not account["trading_enabled"]:
            raise conflict("TRADING_DISABLED",
                           "trading is disabled for this account")
        if not transport.connected:
            raise conflict("ACCOUNT_NOT_CONNECTED",
                           "no live terminal/bridge session for this account")

    def _assert_symbol_allowed(self, account: Dict[str, Any],
                               symbol: str) -> None:
        allowlist = account.get("symbol_allowlist") or []
        if not allowlist:
            return
        wanted = symbol.upper()
        for allowed in allowlist:
            if resolve_symbol(wanted, [allowed],
                              self.settings.symbol_suffix_hints):
                return
        raise conflict("SYMBOL_NOT_ALLOWED",
                       f"{symbol} is outside this account's symbol allowlist",
                       {"allowlist": allowlist})

    @staticmethod
    def _validate_stops(side: str, price: float, sl: Optional[float],
                        tp: Optional[float]) -> None:
        if sl:
            if side == "buy" and sl >= price:
                raise bad_request(
                    "INVALID_STOPS",
                    "stop_loss must be below the entry price for a buy")
            if side == "sell" and sl <= price:
                raise bad_request(
                    "INVALID_STOPS",
                    "stop_loss must be above the entry price for a sell")
        if tp:
            if side == "buy" and tp <= price:
                raise bad_request(
                    "INVALID_STOPS",
                    "take_profit must be above the entry price for a buy")
            if side == "sell" and tp >= price:
                raise bad_request(
                    "INVALID_STOPS",
                    "take_profit must be below the entry price for a sell")

    def _realized_today(self, transport: Mt5Transport) -> float:
        start = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime())
        deals = transport.deals(start, None, 1000)
        return sum(d.profit - d.commission for d in deals
                   if d.comment != "entry")

    def _request_hash(self, payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def trade(self, ctx: AuthContext, account_id: str, req: TradeRequest,
              idem_key: Optional[str], ip: str) -> Tuple[int, Dict[str, Any]]:
        account = self._account(ctx, account_id)
        endpoint = "trade"
        request_hash = self._request_hash(req.model_dump(mode="json"))
        replay = self._idem_replay(ctx, idem_key, endpoint, request_hash)
        if replay is not None:
            return replay

        transport = self._transport(account)
        self._guard_trade(account, transport)
        self.check_trade_rate(ctx)
        try:
            spec = transport.symbol(req.symbol)
            current = transport.price(spec.symbol)
        except TransportError as exc:
            raise self._transport_error(exc) from exc
        self._assert_symbol_allowed(account, spec.symbol)
        volume = normalize_volume(req.volume, spec)
        if volume is None:
            raise bad_request(
                "INVALID_VOLUME",
                f"volume must be between {spec.min_volume} and "
                f"{spec.max_volume} in steps of {spec.volume_step}",
                {"min_volume": spec.min_volume, "max_volume": spec.max_volume,
                 "volume_step": spec.volume_step})
        if account["max_lot"] is not None and volume > account["max_lot"]:
            raise conflict("LOT_LIMIT_EXCEEDED",
                           f"volume {volume} exceeds max_lot "
                           f"{account['max_lot']}")
        if account["max_open_positions"] is not None and \
                len(transport.positions()) >= account["max_open_positions"]:
            raise conflict("MAX_OPEN_POSITIONS",
                           "max_open_positions reached for this account")
        if account["daily_loss_limit_usd"] is not None:
            realized = self._realized_today(transport)
            if realized <= -abs(account["daily_loss_limit_usd"]):
                raise conflict(
                    "DAILY_LOSS_LIMIT",
                    f"realized P&L {realized:.2f} reached the daily loss limit",
                    {"realized_today": round(realized, 2)})

        entry_price = current.ask if req.side == "buy" else current.bid
        check_price = req.price if req.action == "pending" else entry_price
        self._validate_stops(req.side, float(check_price or entry_price),
                             req.stop_loss, req.take_profit)

        payload = {
            "symbol": spec.symbol, "side": req.side, "volume": volume,
            "stop_loss": req.stop_loss, "take_profit": req.take_profit,
            "price": req.price, "order_type": req.order_type,
            "comment": req.comment or "api",
            "client_order_id": req.client_order_id,
        }
        kind = "market" if req.action == "market" else "pending"
        return self._dispatch(ctx, account, transport, kind, payload, {
            "endpoint": endpoint, "idem_key": idem_key,
            "request_hash": request_hash, "ip": ip})

    def modify_position(self, ctx: AuthContext, account_id: str,
                        position_id: str, req: ModifyPositionRequest,
                        idem_key: Optional[str], ip: str
                        ) -> Tuple[int, Dict[str, Any]]:
        account = self._account(ctx, account_id)
        request_hash = self._request_hash(
            {"position_id": position_id, **req.model_dump(mode="json")})
        replay = self._idem_replay(ctx, idem_key, "modify_position",
                                   request_hash)
        if replay is not None:
            return replay
        transport = self._transport(account)
        self._guard_trade(account, transport)
        self.check_trade_rate(ctx)
        positions = {p.position_id: p for p in transport.positions()}
        position = positions.get(position_id)
        if position is None:
            raise not_found("position")
        self._assert_symbol_allowed(account, position.symbol)
        self._validate_stops(
            position.side, position.current_price,
            req.stop_loss if req.stop_loss is not None else position.stop_loss,
            req.take_profit if req.take_profit is not None
            else position.take_profit)
        # Send the resolved values, not nulls: a real EA that receives
        # sl=0 because the field was omitted would *clear* an existing stop.
        payload = {
            "position_id": position_id,
            "stop_loss": req.stop_loss if req.stop_loss is not None
            else position.stop_loss,
            "take_profit": req.take_profit if req.take_profit is not None
            else position.take_profit,
        }
        return self._dispatch(ctx, account, transport, "modify", payload, {
            "endpoint": "modify_position", "idem_key": idem_key,
            "request_hash": request_hash, "ip": ip,
            "is_trade": False})

    def close_position(self, ctx: AuthContext, account_id: str,
                       position_id: str, req: ClosePositionRequest,
                       idem_key: Optional[str], ip: str
                       ) -> Tuple[int, Dict[str, Any]]:
        account = self._account(ctx, account_id)
        request_hash = self._request_hash(
            {"position_id": position_id, **req.model_dump(mode="json")})
        replay = self._idem_replay(ctx, idem_key, "close_position",
                                   request_hash)
        if replay is not None:
            return replay
        transport = self._transport(account)
        self._guard_trade(account, transport)
        self.check_trade_rate(ctx)
        positions = {p.position_id: p for p in transport.positions()}
        if position_id not in positions:
            raise not_found("position")
        volume = req.volume
        if volume is not None:
            volume = normalize_volume(volume, transport.symbol(
                positions[position_id].symbol))
            if volume is None:
                raise bad_request("INVALID_VOLUME", "invalid close volume")
        payload = {"position_id": position_id, "volume": volume,
                   "comment": req.comment or "api-close"}
        return self._dispatch(ctx, account, transport, "close", payload, {
            "endpoint": "close_position", "idem_key": idem_key,
            "request_hash": request_hash, "ip": ip, "is_trade": False})

    def cancel_order(self, ctx: AuthContext, account_id: str, order_id: str,
                     idem_key: Optional[str], ip: str
                     ) -> Tuple[int, Dict[str, Any]]:
        account = self._account(ctx, account_id)
        request_hash = self._request_hash({"order_id": order_id})
        replay = self._idem_replay(ctx, idem_key, "cancel_order",
                                   request_hash)
        if replay is not None:
            return replay
        transport = self._transport(account)
        self._guard_trade(account, transport)
        self.check_trade_rate(ctx)
        if order_id not in {o.order_id for o in transport.orders()}:
            raise not_found("order")
        payload = {"order_id": order_id}
        return self._dispatch(ctx, account, transport, "cancel", payload, {
            "endpoint": "cancel_order", "idem_key": idem_key,
            "request_hash": request_hash, "ip": ip, "is_trade": False})

    def _dispatch(self, ctx: AuthContext, account: Dict[str, Any],
                  transport: Mt5Transport, kind: str,
                  payload: Dict[str, Any], meta: Dict[str, Any]
                  ) -> Tuple[int, Dict[str, Any]]:
        cid = account["connection_id"]
        ttl = max(120.0, self.settings.command_timeout_s * 6)
        command = self.store.create_command(
            tenant_id=ctx.tenant_id, connection_id=cid, kind=kind,
            payload=payload, ttl_s=ttl)
        result = transport.execute(kind, payload, command["command_id"])
        status = result.get("status", "REJECTED")
        accepted = bool(result.get("accepted"))

        if status == "QUEUED":
            body = {
                "command_id": command["command_id"], "status": "QUEUED",
                "account_id": cid, "retcode": "PENDING",
                "message": result.get("message", "awaiting bridge"),
                "order_id": None, "position_id": payload.get("position_id"),
                "deal_id": None, "price": None, "volume": payload.get("volume"),
                "poll_url": f"/v1/accounts/{cid}/commands/"
                            f"{command['command_id']}",
            }
            code = 202
        elif accepted:
            self.store.complete_command(cid, command["command_id"], "DONE",
                                        result)
            body = {
                "command_id": command["command_id"], "status": "EXECUTED",
                "account_id": cid,
                "retcode": str(result.get("retcode", "DONE")),
                "message": str(result.get("message", "executed")),
                "order_id": result.get("order_id"),
                "position_id": result.get("position_id"),
                "deal_id": result.get("deal_id"),
                "price": result.get("price"),
                "volume": result.get("volume"),
            }
            code = 200
        else:
            self.store.complete_command(cid, command["command_id"], "REJECTED",
                                        result)
            body = {
                "command_id": command["command_id"], "status": "REJECTED",
                "account_id": cid,
                "retcode": str(result.get("retcode", "BROKER_REJECTED")),
                "message": str(result.get("message", "rejected")),
                "order_id": None, "position_id": payload.get("position_id"),
                "deal_id": None, "price": None, "volume": payload.get("volume"),
            }
            code = 422

        if code in (200,):
            self._persist(account, transport)
        if transport.kind == "simulated":
            event_type = ("trade.executed" if code == 200
                          else "trade.rejected")
            self._event(ctx.tenant_id, event_type, cid, body)
            self._event_for_kind(ctx.tenant_id, cid, kind, body)
            self.hub.publish({"type": "command", "account_id": cid,
                              "data": body})
        if meta.get("idem_key"):
            self.store.put_idempotency(
                tenant_id=ctx.tenant_id, key=meta["idem_key"],
                endpoint=meta["endpoint"], request_hash=meta["request_hash"],
                status_code=code, response=body,
                command_id=command["command_id"])
        self._audit(ctx, f"{kind}.{str(body['status']).lower()}", cid,
                    {"command_id": command["command_id"],
                     "retcode": body["retcode"],
                     "volume": payload.get("volume")}, meta.get("ip", ""))
        return code, body

    def _idem_replay(self, ctx: AuthContext, key: Optional[str],
                     endpoint: str, request_hash: str
                     ) -> Optional[Tuple[int, Dict[str, Any]]]:
        if not key:
            return None
        prior = self.store.get_idempotency(ctx.tenant_id, key)
        if prior is None:
            return None
        if prior["request_hash"] != request_hash:
            raise GatewayError(
                409, "IDEMPOTENCY_KEY_REUSED",
                "this Idempotency-Key was used with a different request body",
                {"endpoint": prior["endpoint"]})
        response = dict(prior["response"])
        response["idempotent_replay"] = True
        return prior["status_code"], response

    def command_status(self, ctx: AuthContext, account_id: str,
                       command_id: str) -> Dict[str, Any]:
        self._account(ctx, account_id)
        self.store.expire_commands()
        command = self.store.get_command(account_id, command_id)
        if command is None:
            raise not_found("command")
        return {
            "command_id": command["command_id"], "account_id": account_id,
            "kind": command["kind"], "status": command["status"],
            "result": command["result"], "created_at": command["created_at"],
            "updated_at": command["updated_at"],
        }

    # ==================================================================
    # Bridge plane
    # ==================================================================
    def bridge_handshake(self, account: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "account_id": account["connection_id"],
            "login": account["login"], "server": account["server"],
            "broker": account["broker"], "platform": "mt5",
            "symbols": account["symbols"],
            "control_mode": account["control_mode"],
            "trading_enabled": bool(account["trading_enabled"]),
            "command_poll_wait_s": min(20, self.settings.bridge_poll_max_s),
            "state_push_interval_s": 5,
        }

    def apply_bridge_state(self, account: Dict[str, Any],
                           body: Dict[str, Any]) -> Dict[str, Any]:
        cid = account["connection_id"]
        transport = self._transport(account)
        if transport.kind != "bridge":
            raise bad_request("NOT_A_BRIDGE_ACCOUNT",
                              "this account is not bridge-backed")
        was_connected = transport.connected
        transport.apply_state(body)  # type: ignore[attr-defined]
        incoming_seq = body.get("sequence")
        seq = int(incoming_seq) if isinstance(incoming_seq, int) else \
            int(account.get("state_sequence") or 0) + 1
        self.store.save_state(cid, transport.snapshot(),  # type: ignore[attr-defined]
                              seq, status="CONNECTED")
        account = self.store.get_account_any(cid) or account
        self._sequences[cid] = seq

        info = transport.account_information()
        self.hub.publish({"type": "account", "account_id": cid,
                          "data": info.to_dict()})
        prices = (transport.snapshot().get("prices") or {})  # type: ignore[attr-defined]
        for symbol, row in prices.items():
            if isinstance(row, dict):
                quote = {"symbol": symbol,
                         "bid": row.get("bid"), "ask": row.get("ask"),
                         "spread": row.get("spread", 0),
                         "time": row.get("time", utc_now())}
            elif isinstance(row, (list, tuple)) and len(row) >= 2:
                quote = {"symbol": symbol, "bid": row[0], "ask": row[1],
                         "spread": 0, "time": utc_now()}
            else:
                continue
            self.hub.publish({"type": "quote", "account_id": cid,
                              "data": quote})
        positions = [p.to_dict() for p in transport.positions()]
        orders = [o.to_dict() for o in transport.orders()]
        self.hub.publish({"type": "positions", "account_id": cid,
                          "data": {"positions": positions}})
        self.hub.publish({"type": "orders", "account_id": cid,
                          "data": {"orders": orders}})
        if not was_connected:
            self._event(account["tenant_id"], "account.connected", cid,
                        {"account_id": cid, "status": "CONNECTED"})
        return {"ok": True, "account_id": cid, "sequence": seq,
                "trading_enabled": bool(account["trading_enabled"]),
                "control_mode": account["control_mode"],
                "pending_commands": 0}

    def pull_bridge_commands(self, account: Dict[str, Any],
                             limit: int) -> List[Dict[str, Any]]:
        return self.store.pull_commands(account["connection_id"], limit)

    def complete_bridge_command(self, account: Dict[str, Any],
                                command_id: str,
                                body: Dict[str, Any]) -> Dict[str, Any]:
        cid = account["connection_id"]
        accepted = bool(body.get("accepted"))
        status = "DONE" if accepted else "REJECTED"
        result = {
            "accepted": accepted,
            "status": "EXECUTED" if accepted else "REJECTED",
            "retcode": str(body.get("retcode", "DONE" if accepted
                                    else "BRIDGE_REJECTED")),
            "message": str(body.get("message", "")),
            "order_id": body.get("order_id"),
            "position_id": body.get("position_id"),
            "deal_id": body.get("deal_id"),
            "price": body.get("price"),
            "volume": body.get("volume"),
        }
        command = self.store.complete_command(cid, command_id, status, result)
        if command is None:
            raise not_found("command")
        event = dict(result)
        event.update({"command_id": command_id, "account_id": cid,
                      "kind": command["kind"]})
        self._event(account["tenant_id"],
                    "trade.executed" if accepted else "trade.rejected",
                    cid, event)
        if accepted:
            self._event_for_kind(account["tenant_id"], cid,
                                 command["kind"], event)
        self.hub.publish({"type": "command", "account_id": cid, "data": event})
        return {"ok": True, "command_id": command_id, "status": status}

    # ==================================================================
    # Webhooks / audit / usage
    # ==================================================================
    def create_webhook(self, ctx: AuthContext, url: str,
                       events: List[str], description: str,
                       ip: str) -> Dict[str, Any]:
        secret = "whsec_" + secrets.token_hex(24)
        hook = self.store.create_webhook(
            tenant_id=ctx.tenant_id, url=url, events=events or ["*"],
            description=description, secret=secret)
        self._audit(ctx, "webhook.created", "", {"url": url,
                                                 "events": events}, ip)
        return {"webhook_id": hook["webhook_id"], "url": hook["url"],
                "events": hook["events"], "description": hook["description"],
                "active": hook["active"], "created_at": hook["created_at"],
                "secret": secret}

    def list_webhooks(self, ctx: AuthContext) -> List[Dict[str, Any]]:
        return [{"webhook_id": h["webhook_id"], "url": h["url"],
                 "events": h["events"], "description": h["description"],
                 "active": h["active"], "created_at": h["created_at"],
                 "secret": None}
                for h in self.store.list_webhooks(ctx.tenant_id)]

    def delete_webhook(self, ctx: AuthContext, webhook_id: str,
                       ip: str) -> Dict[str, Any]:
        if not self.store.delete_webhook(ctx.tenant_id, webhook_id):
            raise not_found("webhook")
        self._audit(ctx, "webhook.deleted", "", {"webhook_id": webhook_id}, ip)
        return {"webhook_id": webhook_id, "active": False}

    def list_audit(self, ctx: AuthContext, limit: int) -> List[Dict[str, Any]]:
        return self.store.list_audit(ctx.tenant_id, limit)

    def usage(self, ctx: AuthContext) -> Dict[str, Any]:
        stats = self.limiter.stats(ctx.key_id, ctx.rate_limit_per_min)
        return {
            "key_id": ctx.key_id, "tenant_id": ctx.tenant_id,
            "scopes": ctx.scopes,
            "rate_limit_per_min": ctx.rate_limit_per_min,
            "requests_this_minute": stats["requests_this_minute"],
            "requests_total": stats["requests_total"],
            "trades_total": stats["trades_total"],
            "accounts": self.store.count_accounts(ctx.tenant_id),
            "reset_in_s": stats["reset_in_s"],
        }

    # ==================================================================
    # Internals
    # ==================================================================
    def _audit(self, ctx: AuthContext, action: str, account_id: str,
               detail: Dict[str, Any], ip: str) -> None:
        self.store.audit(tenant_id=ctx.tenant_id, key_id=ctx.key_id,
                         action=action, account_id=account_id,
                         detail=detail, ip=ip)

    def _event_for_kind(self, tenant_id: str, account_id: str, kind: str,
                        body: Dict[str, Any]) -> None:
        mapping = {
            "market": "position.opened", "pending": "order.placed",
            "close": "position.closed", "cancel": "order.cancelled",
        }
        event_type = mapping.get(kind)
        if event_type:
            self._event(tenant_id, event_type, account_id, body)

    def _event(self, tenant_id: str, event_type: str, account_id: str,
               data: Dict[str, Any]) -> None:
        payload = dict(data)
        payload.setdefault("account_id", account_id)
        try:
            self.webhooks.enqueue(event_type, tenant_id, payload)
        except Exception:
            pass

    def _assert_no_automation_writer(self, login: int, server: str) -> None:
        """Refuse to hand API order authority to an account APEX is driving.

        This is the cross-product safety seam: the gateway and the APEX
        autotrader share one broker account only if one of them is not
        writing. The check is best-effort (the engine DB may be absent on a
        standalone gateway) and fails open only when it cannot read.
        """
        if not self.settings.guard_automation:
            return
        try:
            from ..db import engine_db  # local import: optional dependency
        except Exception:
            return
        try:
            accounts = engine_db.list_accounts()
        except Exception:
            return
        for row in accounts:
            try:
                if int(row.get("mt5_login") or 0) != int(login):
                    continue
                if not row.get("effective_on"):
                    continue
                if server and row.get("server") and \
                        str(row.get("server")) != str(server):
                    continue
            except (TypeError, ValueError):
                continue
            raise conflict(
                "AUTOMATION_WRITER_ACTIVE",
                "this MT5 account is currently owned by the APEX autotrader; "
                "turn automation off before granting API order authority",
                {"connection_id": row.get("connection_id", ""),
                 "effective_on": True})

    def health(self) -> Dict[str, Any]:
        accounts = self.store.list_accounts()
        connected = 0
        for account in accounts:
            try:
                if self._transport(account).connected:
                    connected += 1
            except Exception:
                continue
        return {
            "status": "ok",
            "service": "abutron-mt5-gateway",
            "accounts": len(accounts),
            "connected_accounts": connected,
            "stream": self.hub.stats(),
            "uptime_s": round(time.time() - self._started_at, 1),
            "time": utc_now(),
        }
