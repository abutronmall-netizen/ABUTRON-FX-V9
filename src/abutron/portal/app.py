"""ABUTRON FX V9 web portal — FastAPI app.

Three surfaces (blueprint section 09):
  * /client  — client portal: broker-link health, ON/OFF, positions, trades
  * /admin   — admin CRM: accounts, licenses, users, entitlements
  * /command — APEX operator command center: decisions, risk, writer state

Dashboard badges derive from reconciled native facts, never from "service
active" alone.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..config import DATA_ROOT, LIVE_TRADING_ENABLED
from ..crm.controls import CrmError, crm
from ..crm.sessions import SESSION_COOKIE, session_store
from ..db import engine_db
from ..events import EventLog
from ..feeds.mt5_terminal import Mt5Error, Mt5TerminalManager
from ..feeds.mt5_verifier import Mt5Verifier
from ..apex.engine import ApexEngine
from ..execution.dispatcher import NativeDispatcher
from ..execution.receipts import ReceiptManager
from ..execution.writer_gateway import SingleWriterGateway
from ..reconciliation.reconciler import Reconciler
from ..ops.health import HealthMonitor
from ..ops.portal_ops import portal_ops
from ..risk.risk_engine import RiskEngine
from ..risk.riskguard import RiskGuard, TradeProposal
from ..risk.simulator import PaperTradingEngine


def create_app(event_log: EventLog) -> FastAPI:
    app = FastAPI(title="ABUTRON FX V9", version="1.1.0")

    # Wire shared services
    receipt_mgr = ReceiptManager(event_log)
    writer_gateway = SingleWriterGateway(event_log)
    dispatcher = NativeDispatcher(event_log, writer_gateway, receipt_mgr)
    reconciler = Reconciler(event_log, receipt_mgr)
    health = HealthMonitor(event_log)
    risk_engine = RiskEngine(event_log)
    engine = ApexEngine(event_log, crm)
    from ..selflearn.engine import SelfLearningEngine
    selflearn = SelfLearningEngine(
        event_log, state_path=os.path.join(DATA_ROOT, "selflearn_state.json"))
    selflearn.load()
    # Hardened pre-trade RiskGuard + paper-trading simulator (kweinmeister).
    riskguard = RiskGuard()
    paper_engine = PaperTradingEngine(guard=riskguard)
    # Per-account Wine/MT5 terminal supervisor (launches a terminal on connect).
    mt5_mgr = Mt5TerminalManager()
    # Isolated native identity verifier (password never touches the request
    # path — only the verifier sees it, and only a bool + reason comes back).
    mt5_verifier = Mt5Verifier(mt5_mgr)

    app.state.services = {
        "event_log": event_log, "receipt_mgr": receipt_mgr,
        "writer_gateway": writer_gateway, "dispatcher": dispatcher,
        "reconciler": reconciler, "health": health,
        "risk_engine": risk_engine, "apex": engine, "crm": crm,
        "selflearn": selflearn, "riskguard": riskguard,
        "paper_engine": paper_engine, "portal_ops": portal_ops,
        "mt5_mgr": mt5_mgr, "mt5_verifier": mt5_verifier,
    }

    templates = Jinja2Templates(
        directory=str(Path(__file__).parent / "templates"))

    # ---------------- Client portal ----------------
    def _client_owner(request: Request) -> Optional[str]:
        sess = session_store.verify(request.cookies.get(SESSION_COOKIE))
        if sess is None or sess["role"] != "client":
            return None
        return sess["owner_id"]

    def _admin_only(request: Request) -> Optional[Dict[str, Any]]:
        """Require an authenticated admin/operator session for CRM endpoints.

        The admin surface manages accounts, licenses, users, writer arming and
        effective-ON confirmation, so it is strictly role-gated. A client
        session (or no session) is rejected with 403/401.
        """
        sess = session_store.verify(request.cookies.get(SESSION_COOKIE))
        if sess is None:
            return None
        if sess["role"] not in ("admin", "operator"):
            return None
        return sess

    def _scoped_accounts(owner_id: str) -> list:
        return [crm._control_state(a["connection_id"])
                for a in crm.list_accounts(owner_id=owner_id)]

    @app.get("/client", response_class=HTMLResponse)
    async def client_dashboard(request: Request):
        owner = _client_owner(request)
        accounts = _scoped_accounts(owner) if owner else []
        return templates.TemplateResponse(request, "client.html", {
            "accounts": accounts,
            "authed": owner is not None,
        })

    @app.post("/api/client/login")
    async def client_login(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if not username or not password:
            raise HTTPException(400, "CREDENTIAL_INVALID: username and "
                                   "password required")
        try:
            user = crm.authenticate(username, password)
        except Exception as e:
            raise HTTPException(401, f"CREDENTIAL_INVALID: {e}")
        if user is None:
            raise HTTPException(401, "CREDENTIAL_INVALID: username or password")
        if user["role"] != "client":
            raise HTTPException(403, "FORBIDDEN: client portal only")
        session = session_store.issue(user)
        resp = JSONResponse({"user_id": user["user_id"],
                             "username": user["username"],
                             "owner_id": user.get("owner_id", "")})
        resp.set_cookie(SESSION_COOKIE, session["token"], httponly=True,
                        samesite="lax", max_age=7 * 24 * 3600, path="/")
        return resp

    @app.post("/api/client/logout")
    async def client_logout(request: Request):
        session_store.revoke(request.cookies.get(SESSION_COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.post("/api/admin/login")
    async def admin_login(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if not username or not password:
            raise HTTPException(400, "CREDENTIAL_INVALID: username and "
                                   "password required")
        try:
            user = crm.authenticate(username, password)
        except Exception as e:
            raise HTTPException(401, f"CREDENTIAL_INVALID: {e}")
        if user is None:
            raise HTTPException(401, "CREDENTIAL_INVALID: username or password")
        if user["role"] not in ("admin", "operator"):
            raise HTTPException(403, "FORBIDDEN: admin portal only")
        session = session_store.issue(user)
        resp = JSONResponse({"user_id": user["user_id"],
                             "username": user["username"],
                             "role": user["role"]})
        resp.set_cookie(SESSION_COOKIE, session["token"], httponly=True,
                        samesite="lax", max_age=7 * 24 * 3600, path="/")
        return resp

    @app.get("/api/client/me")
    async def client_me(request: Request):
        sess = session_store.verify(request.cookies.get(SESSION_COOKIE))
        if sess is None:
            raise HTTPException(401, "SESSION_EXPIRED: not authenticated")
        return {"user_id": sess["user_id"], "username": sess["username"],
                "owner_id": sess["owner_id"], "role": sess["role"]}

    @app.post("/api/client/connect-account")
    async def client_connect_account(request: Request):
        """Enroll a broker connection (no password). CONNECTING state."""
        owner = _client_owner(request)
        if owner is None:
            raise HTTPException(401, "SESSION_EXPIRED: not authenticated")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "CREDENTIAL_INVALID: JSON body required")
        try:
            status = crm.start_broker_link(
                owner_id=owner,
                broker=str(body.get("broker", "")).strip().lower(),
                environment=str(body.get("environment", "demo")).strip().lower(),
                server=str(body.get("server", "")).strip(),
                mt5_login=int(body.get("mt5_login", 0)),
                display_name=str(body.get("display_name", "")).strip(),
                symbols=body.get("symbols"),
            )
        except CrmError as e:
            raise HTTPException(400, str(e))
        except ValueError as e:
            raise HTTPException(400, f"CREDENTIAL_INVALID: {e}")
        # Open the client's MT5 terminal for the broker-native session.
        try:
            mt5_mgr.launch(connection_id=status["connection_id"],
                           broker=status["broker"], login=status["mt5_login"],
                           server=status["server"])
        except Mt5Error as e:
            crm.set_conn_status(status["connection_id"], "ERROR",
                               error=f"TERMINAL_UNAVAILABLE: {e}")
            raise HTTPException(400, f"BROKER_UNAVAILABLE: terminal launch "
                                     f"failed ({e})")
        # LIVE: once the terminal has launched successfully, the live
        # session is CONNECTED (the terminal IS the broker session).
        if status.get("environment") == "live":
            status = crm.activate_live_session(status["connection_id"])
        return status

    @app.post("/api/client/accounts/{connection_id}/verify")
    async def client_verify_account(request: Request, connection_id: str):
        """One-shot native DEMO identity proof (password via isolated verifier)."""
        owner = _client_owner(request)
        if owner is None:
            raise HTTPException(401, "SESSION_EXPIRED: not authenticated")
        _owned_or_404(owner, connection_id)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "CREDENTIAL_INVALID: JSON body required")
        password = str(body.get("password", ""))
        if not password:
            raise HTTPException(400, "CREDENTIAL_REQUIRED: broker password "
                                     "required for verification")
        try:
            status = crm.verify_broker_identity(
                connection_id, password=password, verifier=mt5_verifier)
        except CrmError as e:
            raise HTTPException(409, str(e))
        return status

    @app.get("/api/client/accounts")
    async def client_accounts(request: Request):
        owner = _client_owner(request)
        if owner is None:
            raise HTTPException(401, "SESSION_EXPIRED: not authenticated")
        out = []
        for a in crm.list_accounts(owner_id=owner):
            st = crm._control_state(a["connection_id"])
            st["positions"] = engine_db.recent_open_positions(
                a["connection_id"])
            st["recent_trades"] = engine_db.list_receipts(
                a["connection_id"], limit=10)
            out.append(st)
        return out

    def _owned_or_404(owner_id: str, connection_id: str) -> Dict:
        acct = crm.get_account(connection_id)
        if acct is None or acct["owner_id"] != owner_id:
            raise HTTPException(404, "account not found")
        return acct

    @app.post("/api/client/accounts/{connection_id}/on")
    async def client_on(request: Request, connection_id: str):
        owner = _client_owner(request)
        if owner is None:
            raise HTTPException(401, "SESSION_EXPIRED: not authenticated")
        _owned_or_404(owner, connection_id)
        try:
            return crm.request_on(connection_id, by="client")
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/client/accounts/{connection_id}/off")
    async def client_off(request: Request, connection_id: str):
        owner = _client_owner(request)
        if owner is None:
            raise HTTPException(401, "SESSION_EXPIRED: not authenticated")
        _owned_or_404(owner, connection_id)
        try:
            return crm.request_off(connection_id, by="client")
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.get("/api/client/accounts/{connection_id}/risk")
    async def client_risk(request: Request, connection_id: str):
        owner = _client_owner(request)
        if owner is None:
            raise HTTPException(401, "SESSION_EXPIRED: not authenticated")
        acct = _owned_or_404(owner, connection_id)
        return crm.owner_risk_exposure(acct["owner_id"], {}, {})

    @app.get("/api/client/accounts/{connection_id}/status")
    async def client_conn_status(request: Request, connection_id: str):
        owner = _client_owner(request)
        if owner is None:
            raise HTTPException(401, "SESSION_EXPIRED: not authenticated")
        _owned_or_404(owner, connection_id)
        return crm.connection_status(connection_id)

    @app.post("/api/client/accounts/{connection_id}/disconnect")
    async def client_disconnect(request: Request, connection_id: str):
        owner = _client_owner(request)
        if owner is None:
            raise HTTPException(401, "SESSION_EXPIRED: not authenticated")
        _owned_or_404(owner, connection_id)
        try:
            return crm.disconnect(connection_id)
        except CrmError as e:
            raise HTTPException(409, str(e))

    # ---------------- Admin CRM ----------------
    @app.get("/admin", response_class=HTMLResponse)
    async def admin_dashboard(request: Request):
        # The page is a shell; account/user data is fetched client-side from
        # the role-gated /api/admin/* endpoints, never embedded server-side.
        authed = _admin_only(request) is not None
        return templates.TemplateResponse(request, "admin.html", {
            "authed": authed,
            "accounts": [],
            "users": [],
        })

    @app.get("/api/admin/accounts")
    async def admin_accounts(request: Request, broker: str | None = None,
                             owner_id: str | None = None):
        if _admin_only(request) is None:
            raise HTTPException(401, "SESSION_EXPIRED: admin authentication required")
        return crm.list_accounts(broker, owner_id)

    @app.post("/api/admin/accounts")
    async def admin_register_account(request: Request, body: Dict[str, Any]):
        if _admin_only(request) is None:
            raise HTTPException(401, "SESSION_EXPIRED: admin authentication required")
        try:
            return crm.register_account(**body)
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/admin/accounts/{connection_id}/link")
    async def admin_link_binary(request: Request, connection_id: str,
                                body: Dict[str, Any]):
        if _admin_only(request) is None:
            raise HTTPException(401, "SESSION_EXPIRED: admin authentication required")
        try:
            return crm.link_broker_identity(connection_id, **body)
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/admin/accounts/{connection_id}/arm")
    async def admin_arm_writer(request: Request, connection_id: str,
                               body: Dict[str, Any]):
        if _admin_only(request) is None:
            raise HTTPException(401, "SESSION_EXPIRED: admin authentication required")
        acct = crm.get_account(connection_id)
        if not acct:
            raise HTTPException(404, "account not found")
        gw: SingleWriterGateway = app.state.services["writer_gateway"]
        # Derive terminal path from the authenticated account connection id,
        # never from a request-supplied filesystem path.
        mql5_files_dir = ""
        if re.fullmatch(r"[0-9a-f]{32}", connection_id):
            worker_root = Path(os.getenv("ABUTRON_MT5_WORKER_STATE_ROOT",
                                         "/var/lib/private"))
            candidate_dir = (worker_root / ("abutron-client-mt5-" + connection_id)
                             / "terminal" / "MQL5" / "Files")
            if candidate_dir.is_dir() and not candidate_dir.is_symlink():
                mql5_files_dir = str(candidate_dir)
        return gw.arm(connection_id=connection_id,
                      ea_path=body.get("ea_path", ""),
                      expected_sha256=body.get("expected_sha256", ""),
                      activation_revision=body.get("activation_revision", ""),
                      broker=acct["broker"],
                      mql5_files_dir=mql5_files_dir).to_dict()

    @app.post("/api/admin/licenses")
    async def admin_grant_license(request: Request, body: Dict[str, Any]):
        if _admin_only(request) is None:
            raise HTTPException(401, "SESSION_EXPIRED: admin authentication required")
        return crm.grant_license(**body)

    @app.post("/api/admin/users")
    async def admin_create_user(request: Request, body: Dict[str, Any]):
        if _admin_only(request) is None:
            raise HTTPException(401, "SESSION_EXPIRED: admin authentication required")
        try:
            return crm.create_user(**body)
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/admin/confirm-effective-on")
    async def admin_confirm_on(request: Request, connection_id: str,
                               activation_revision: str, ea_sha256: str):
        if _admin_only(request) is None:
            raise HTTPException(401, "SESSION_EXPIRED: admin authentication required")
        try:
            return crm.confirm_effective_on(connection_id,
                                            activation_revision=activation_revision,
                                            ea_sha256=ea_sha256)
        except Exception as e:
            raise HTTPException(400, str(e))

    # ---------------- Command center ----------------
    @app.get("/command", response_class=HTMLResponse)
    async def command_center(request: Request):
        return templates.TemplateResponse(request, "command.html", {})

    @app.get("/api/command/health")
    async def command_health():
        return health.all_health()

    @app.get("/api/command/accounts/{connection_id}/candidates")
    async def command_candidates(connection_id: str):
        from sqlalchemy import select
        from ..db import CandidateRow
        rows = engine_db.session.scalars(select(CandidateRow).where(
            CandidateRow.connection_id == connection_id)
            .order_by(CandidateRow.conviction.desc()).limit(50))
        return [_row_dict(r) for r in rows]

    @app.get("/api/command/accounts/{connection_id}/decisions")
    async def command_decisions(connection_id: str, limit: int = 30):
        journal = app.state.services["apex"].journal
        return journal.tail(connection_id, limit)

    @app.get("/api/command/accounts/{connection_id}/receipts")
    async def command_receipts(connection_id: str):
        return receipt_mgr.list_receipts(connection_id)

    @app.get("/api/command/accounts/{connection_id}/writer")
    async def command_writer(connection_id: str):
        return app.state.services["writer_gateway"].status(connection_id).to_dict()

    @app.get("/api/command/selflearn")
    async def command_selflearn():
        return app.state.services["selflearn"].health()

    @app.post("/api/command/selflearn/save")
    async def command_selflearn_save():
        app.state.services["selflearn"].save()
        return {"saved": True,
                "health": app.state.services["selflearn"].health()}

    @app.get("/api/command/riskguard")
    async def command_riskguard():
        rg = app.state.services["riskguard"]
        return {"enabled": True,
                "limits": {"max_position_lots": rg.max_position_lots,
                           "max_concentration_pct": rg.max_concentration_pct,
                           "max_total_exposure_pct": rg.max_total_exposure_pct,
                           "max_risk_per_trade_pct": rg.max_risk_per_trade_pct}}

    @app.post("/api/command/simulate")
    async def command_simulate(body: Dict[str, Any]):
        """Run an offline paper-trading backtest (no broker/writer access)."""
        pe = app.state.services["paper_engine"]
        bars = int(body.get("bars", 120))
        seed = body.get("seed")
        seed = int(seed) if seed is not None else None
        trend = float(body.get("trend", 0.0))
        vol = float(body.get("volatility", 0.0008))
        symbol = str(body.get("symbol", "SIM"))

        # A simple built-in strategy: go long on an up-move signal derived
        # from the simulated history. Override with body["strategy"] later.
        def _strategy(sim, portfolio, i):
            hist = sim.get_history()
            if len(hist) < 5:
                return None
            momentum = (hist[-1] - hist[-5]) / hist[-5]
            if momentum > 0.0006:
                return TradeProposal(
                    symbol=symbol, direction="BUY", volume_lots=1.0,
                    price=sim.current_price, stop_loss=sim.current_price * 0.999,
                    take_profit=sim.current_price * 1.002,
                    stop_risk_usd=100.0)
            return None

        result = pe.run(_strategy, bars=bars, symbol=symbol,
                        initial_price=float(body.get("initial_price", 1.1000)),
                        volatility=vol, trend=trend, seed=seed)
        return result.to_dict()

    @app.get("/api/command/analytics")
    async def command_analytics():
        return app.state.services["portal_ops"].analytics()

    @app.get("/api/command/leaderboard")
    async def command_leaderboard(limit: int = 20):
        return app.state.services["portal_ops"].leaderboard(limit=limit)

    @app.get("/api/command/runs")
    async def command_runs(connection_id: str | None = None,
                           limit: int = 50):
        return app.state.services["portal_ops"].list_runs(
            connection_id, limit=limit)

    @app.get("/api/command/model-providers")
    async def command_model_providers():
        return app.state.services["portal_ops"].list_model_providers()

    @app.post("/api/command/model-providers")
    async def command_model_provider_upsert(body: Dict[str, Any]):
        app.state.services["portal_ops"].upsert_model_provider(body)
        return {"saved": True,
                "providers": app.state.services["portal_ops"]
                .list_model_providers()}

    @app.delete("/api/command/model-providers/{provider_id}")
    async def command_model_provider_delete(provider_id: str):
        removed = app.state.services["portal_ops"].delete_model_provider(
            provider_id)
        if not removed:
            raise HTTPException(status_code=404, detail="provider not found")
        return {"deleted": True, "provider_id": provider_id}

    @app.post("/api/command/dispatch/{connection_id}")
    async def command_dispatch(connection_id: str):
        return dispatcher.dispatch_account(connection_id)

    @app.post("/api/command/reconcile/{connection_id}")
    async def command_reconcile(connection_id: str,
                                native_history: list | None = None):
        history = []
        for h in (native_history or []):
            from ..reconciliation.reconciler import NativeHistoryEntry
            history.append(NativeHistoryEntry(**h))
        return reconciler.reconcile_account(connection_id, history)

    @app.get("/api/health")
    async def api_health():
        return {"status": "ok", "service": "ABUTRON-FX-V9",
                "engine": "APEX 1.1"}

    return app


def _user_safe(u):
    if u is None:
        return None
    u = dict(u)
    u.pop("password_hash", None)
    return u


def _row_dict(row):
    return {c.name: getattr(row, c.name)
            for c in row.__table__.columns}
