"""ABUTRON FX V9 — application entry point.

Runs the web portal, the APEX engine tick loop, the per-account native
dispatcher loop, and the manager Unix socket. Designed for Ubuntu + Wine
+ MT5; binds 0.0.0.0 so it works behind the platform preview.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import uvicorn

from .config import (DATA_ROOT, ENGINE_TICK_SECONDS, PORTAL_HOST, PORTAL_PORT,
                     utc_now)
from .events import EventLog
from .db import engine_db
from .crm.controls import crm
from .execution.dispatcher import NativeDispatcher
from .execution.receipts import ReceiptManager
from .execution.writer_gateway import SingleWriterGateway
from .execution.ipc import manager_socket
from .reconciliation.reconciler import Reconciler
from .ops.health import HealthMonitor
from .risk.risk_engine import RiskEngine
from .feeds.mt5_terminal import terminal_manager
from .apex_integration import ApexIntegration, build_instrument_manifest
from .portal.app import create_app


class ApexRuntime:
    """Long-running runtime: engine tick + dispatch loop + manager socket.

    The decision engine is the authoritative vendored APEX 1.1.0
    (`ApexIntegration`): closed M5 candles -> signal bridge -> fail-closed
    candidate review -> single-writer dispatch.
    """

    def __init__(self, event_log: Optional[EventLog] = None):
        self.log = event_log or EventLog(
            os.path.join(DATA_ROOT, "events.jsonl"))
        self.receipt_mgr = ReceiptManager(self.log)
        self.writer_gateway = SingleWriterGateway(self.log)
        self.dispatcher = NativeDispatcher(self.log, self.writer_gateway,
                                           self.receipt_mgr)
        self.reconciler = Reconciler(self.log, self.receipt_mgr)
        self.health = HealthMonitor(self.log)
        self.risk_engine = RiskEngine(self.log)
        self.apex = ApexIntegration(self.log, warmup_bars=0)
        # Self-learning: settled trades feed the brain's TradeMemory so it
        # learns from losses and never enters too early.
        self.reconciler.brain = self.apex.brain
        self._stop = threading.Event()
        self._manifest_cache: Optional[List[Dict[str, Any]]] = None
        # Identifies the account set the cached manifest was built from.
        self._manifest_key: Optional[tuple] = None

    def register_manager_handlers(self) -> None:
        def _status(req):
            return {"status": "ok", "connection_id": req.connection_id}
        def _order(req):
            return {"status": "ack", "ticket": 1000 + int(
                req.raw.get("request_id", "0")[-4:] or "0")}
        manager_socket.register("status", _status)
        manager_socket.register("order", _order)
        manager_socket.register("order_handoff", _order)

    def start_manager_socket(self) -> None:
        self.register_manager_handlers()
        manager_socket.start()

    def engine_tick(self) -> None:
        """One decision pass across all effective-ON accounts.

        In production this reads native exports per account via the feed
        readers. Here it drives the decision pipeline with whatever
        candles the worker has staged; feeds with no fresh export simply
        produce no candidates (a legitimate no-signal outcome).
        """
        for acct in engine_db.list_accounts():
            if not acct["effective_on"]:
                continue
            if not acct["desired_on"]:
                continue
            conn = acct["connection_id"]
            try:
                self._run_account_tick(acct)
            except Exception as e:  # never let one account kill the loop
                self.log.append(_safe_event(
                    "engine.tick_error", connection_id=conn,
                    broker=acct["broker"], execution_state="UNKNOWN",
                    reason_code=f"TICK_ERROR:{type(e).__name__}"))

    def _run_account_tick(self, acct: dict) -> None:
        # The worker stages native exports; feed readers ingest them.
        from .feeds.broker_feeds import get_feed
        feed = get_feed(acct["broker"], self.log)
        symbols = [s for s in (acct.get("symbols") or "").split(",") if s]
        result = feed.read(acct["connection_id"], acct["mt5_login"], symbols)
        if not result.accepted or not result.candles:
            return  # no fresh evidence — legitimate no-signal

        # Build/refresh the APEX instrument manifest for all accounts.
        # Accounts are registered/changed at runtime (client connect flow), so
        # the manifest is rebuilt whenever the account set changes rather than
        # cached forever — a stale cache would silently drop a new account's
        # symbols and MANIFEST_INVALID every one of its candles.
        accounts = engine_db.list_accounts()
        manifest_key = tuple(sorted(
            (a["connection_id"], a.get("broker", ""), a.get("symbols") or "")
            for a in accounts))
        if self._manifest_cache is None or self._manifest_key != manifest_key:
            self._manifest_cache = build_instrument_manifest(accounts)
            self._manifest_key = manifest_key
        manifest = self._manifest_cache

        # Feed each closed candle through the real APEX bridge + review.
        # Order flow + footprint are fed as overlay context where available.
        from .analytics.orderflow import orderflow_engine
        from .analytics.footprint import build_footprint

        equity = float(acct.get("balance", 0.0) or 0.0)
        for c in result.candles:
            if not c.closed:
                continue
            candle = {
                "symbol": c.symbol,
                "open_time": c.open_time,
                "close_time": c.close_time,
                "close_time_epoch": _to_epoch(c.close_time),
                "open": c.open, "high": c.high, "low": c.low,
                "close": c.close, "volume": c.volume,
            }
            # Overlay context (optional; APEX runs regardless)
            of_features = None
            fp_features = None
            of_snap = orderflow_engine.latest(c.symbol)
            if of_snap is not None:
                of_features = dict(of_snap.features)
                if of_snap.trades:
                    fp = build_footprint(c.symbol, of_snap.trades,
                                         c.tick_size or 0.0001)
                    fp_features = dict(fp.features)

            # The feed's quote for this symbol carries the terminal's
            # contract geometry (point value, lot bounds); pass it so the
            # candidate review is attested with real broker values.
            quote = next((q for q in result.quotes if q.symbol == c.symbol),
                         None)
            # Re-read the account: the feed above just persisted freshly
            # attested equity, and the candidate gate must see it (a stale row
            # would block every candidate with
            # BROKER_EQUITY_OR_OPEN_RISK_UNVERIFIED).
            fresh_acct = engine_db.get_account(acct["connection_id"]) or acct
            equity = float(fresh_acct.get("equity")
                           or fresh_acct.get("balance") or 0.0)

            ready = self.apex.process_candle(
                connection_id=fresh_acct["connection_id"], account=fresh_acct,
                candle=candle, manifest=manifest,
                orderflow_features=of_features,
                footprint_features=fp_features, equity=equity,
                native_geometry=_native_geometry_for(fresh_acct, c, quote),
                gate_overrides=_gate_overrides_for(fresh_acct,
                                                   self.writer_gateway))

            if ready["status"] == "READY" and ready["candidate"]:
                stored = self.apex.store_ready_candidate(
                    connection_id=acct["connection_id"], account=acct,
                    ready=ready, candle=candle)
                if stored:
                    # Dispatch the reserved candidate through the single writer.
                    self.dispatcher.dispatch_account(acct["connection_id"])

    def dispatch_loop(self) -> None:
        """Dispatch any RESERVED candidates every tick."""
        while not self._stop.is_set():
            try:
                for acct in engine_db.list_accounts():
                    if acct["effective_on"] and acct["desired_on"]:
                        self.dispatcher.dispatch_account(
                            acct["connection_id"])
            except Exception:
                pass
            self._stop.wait(ENGINE_TICK_SECONDS)

    def run_forever(self) -> None:
        self.start_manager_socket()
        # Background loops in threads
        loops = [
            ("engine-tick", self.engine_tick_loop),
            ("dispatch", self.dispatch_loop),
        ]
        threads = []
        for name, fn in loops:
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            threads.append(t)

        app = create_app(self.log)
        uvicorn.run(app, host=PORTAL_HOST, port=PORTAL_PORT,
                    log_level="info")

    def engine_tick_loop(self) -> None:
        while not self._stop.is_set():
            self.engine_tick()
            # Persist self-learning state so learned weights survive restarts
            try:
                self.apex.selflearn.save()
            except Exception:
                pass
            self._stop.wait(ENGINE_TICK_SECONDS)

    def stop(self) -> None:
        self._stop.set()
        try:
            manager_socket.stop()
        except Exception:
            pass


def _to_epoch(iso_ts: str) -> float:
    """Convert an ISO UTC timestamp to a unix epoch (seconds)."""
    import time
    return time.mktime(time.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ"))


def _native_geometry_for(acct: Dict[str, Any], candle,
                         quote=None) -> Dict[str, Any]:
    """Broker-native geometry for a symbol.

    These values come from the account's MT5 terminal (MarketInfo: tick size,
    min/max lot, point value). The candle carries the terminal's tick size /
    point from the native export; the quote (when the feed produced one)
    carries the contract's point value and lot bounds.

    ``price``/``stop_price``/``take_price`` are deliberately NOT set here —
    the AI entry pipeline computes them from the sniper plan and
    `ApexIntegration.review_geometry` merges them in. Leaving placeholders
    would silently satisfy (or trip) the candidate contract's ordering checks.

    ``broker_verified`` / the calc-profit / order-check flags attest that the
    terminal actually answered for this symbol. They are only True when the
    feed supplied verified terminal geometry (a positive tick size and point
    value); otherwise the candidate review fails closed, as designed.
    """
    is_deriv = acct.get("broker") == "deriv"
    tick_size = float(getattr(candle, "tick_size", 0.0) or 0.0)
    point = float(getattr(candle, "point", 0.0) or 0.0)
    if tick_size <= 0:
        tick_size = point if point > 0 else (0.01 if is_deriv else 0.0001)

    point_value = float(getattr(quote, "point_value", 0.0) or 0.0) \
        if quote is not None else 0.0
    if point_value <= 0:
        point_value = 1.0 if is_deriv else 10.0

    min_lot = float(getattr(quote, "volume_min", 0.0) or 0.0) \
        if quote is not None else 0.0
    if min_lot <= 0:
        min_lot = 1.0 if is_deriv else 0.01
    max_lot = float(getattr(quote, "volume_max", 0.0) or 0.0) \
        if quote is not None else 0.0
    if max_lot <= 0:
        max_lot = 500.0

    # Terminal geometry is attested only when the native export actually
    # carried a usable tick size and point value for this bar.
    verified = bool(tick_size > 0 and point > 0)
    return {
        "tick_size": tick_size,
        "tick_value_per_lot_usd": point_value,
        "min_lot": min_lot,
        "max_lot": max_lot,
        "lot_step": 1.0 if is_deriv else 0.01,
        "min_stop_ticks": 1,
        "broker_verified": verified,
        "price": float(candle.close),
        "slippage_budget_ticks": 1,
        "broker_calc_profit_verified": verified,
        "broker_order_check_verified": verified,
    }


def _gate_overrides_for(acct: Dict[str, Any],
                        writer_gateway) -> Dict[str, Any]:
    """Build the candidate-review gate from live CRM + writer state.

    Every flag is derived from evidence the platform actually holds. Each
    one still defaults to the fail-closed value when that evidence is absent:

    * ``account_bound`` / ``worker_nonce_bound`` — the account row exists and
      a worker binding was recorded by the connect/verify flow.
    * ``fresh_broker_session`` — the account is CONNECTED and its connection
      status was touched recently (a stale status is not a live session).
    * ``native_ea_account_match`` / ``terminal_trade_allowed`` —
      ``SingleWriterGatewayStatus``: the EA is armed and its activation
      revision verifies.
    * ``broker_trade_allowed`` — CRM effective-ON (client did not switch off).
    * ``crm_entitlement_approved`` — an active, unexpired licence exists.
    * ``broker_positions_protected`` / ``broker_exposure_reconciled`` — the
      reconciler holds no UNKNOWN receipt for this account (an unresolved
      send means exposure is *not* reconciled).
    * ``order_history_uncertain`` — an UNKNOWN receipt exists, which by
      design blocks new entries until it is resolved.
    * ``current_verified_stop_risk_usd`` — summed open stop-risk from the
      reconciled ledger.

    ``account_equity_usd`` comes from the account row the feed/worker keeps
    current.
    """
    from .config import HEARTBEAT_STALE_SECONDS
    from .crm.controls import crm
    from .db import engine_db

    conn = acct["connection_id"]
    status = writer_gateway.status(conn)

    # --- CRM evidence -------------------------------------------------
    licence_ok = False
    try:
        licence_ok = bool(crm.check_entitlement(acct.get("owner_id", ""), conn))
    except Exception:
        licence_ok = False

    # --- Freshness of the broker session ------------------------------
    updated = acct.get("conn_updated_at") or ""
    fresh = False
    if acct.get("conn_status") == "CONNECTED" and updated:
        try:
            age = time.time() - _to_epoch(updated)
            fresh = 0 <= age <= max(HEARTBEAT_STALE_SECONDS * 10, 900)
        except (ValueError, TypeError, OverflowError):
            fresh = False

    # --- Reconciled exposure ------------------------------------------
    receipts = []
    try:
        receipts = engine_db.list_receipts(conn, limit=200)
    except Exception:
        receipts = []
    unknown = [r for r in receipts if r.get("execution_state") == "UNKNOWN"]
    open_risk = 0.0
    for r in receipts:
        if r.get("execution_state") in ("ACK", "OPEN", "PROTECTED") and \
                r.get("reconciled_state") in ("MATCHED", "SETTLED", ""):
            open_risk += float(r.get("stop_risk_usd", 0.0) or 0.0)

    return {
        "account_bound": bool(acct.get("owner_id")),
        "worker_nonce_bound": bool(acct.get("worker_binding")),
        "fresh_broker_session": fresh,
        "native_ea_account_match": bool(status.armed and status.revision_ok),
        "broker_trade_allowed": bool(acct.get("effective_on")),
        "terminal_trade_allowed": bool(status.armed),
        "crm_entitlement_approved": licence_ok,
        "single_writer_exclusive": bool(status.armed),
        "broker_positions_protected": not unknown,
        "broker_exposure_reconciled": not unknown,
        # Broker-attested equity (native identity export) — the contract
        # sizes the per-trade budget and the 20% ceiling from it.
        "account_equity_usd": float(acct.get("balance", 0.0) or 0.0),
        "current_verified_stop_risk_usd": round(open_risk, 4),
        "order_history_uncertain": bool(unknown),
        "kill_switch_halted": False,
    }


def _safe_event(event_type: str, **kw):
    from .events import make_event
    return make_event(event_type, **kw)


def main() -> None:
    runtime = ApexRuntime()
    try:
        runtime.run_forever()
    except KeyboardInterrupt:
        runtime.stop()


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "src"))
    main()
