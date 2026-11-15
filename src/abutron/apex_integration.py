"""Integration: real vendored APEX 1.1.0 as the decision engine.

This wires the authoritative APEX source (vendored under
`vendor/abutron-apex/apex11`) into the ABUTRON platform:

  closed M5 candles (from my feed layer)
    -> AbutronApex11SignalBridge.on_closed_candle   (regime + ensemble + paper risk)
    -> candidate_contract.review_candidate           (fail-closed eligibility gate)
    -> reviewed order_candidate                       (single-writer ready)

The bridge is signal-only: it computes a research proposal and never submits
orders. The review contract produces an explicit, fail-closed *candidate*
(no order). Only my single-writer dispatcher may turn a fully-approved
candidate into a native MT5 order. Order flow + footprint remain overlay
context fed into the decision journal.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DATA_ROOT, RISKGUARD_ENABLED
from .events import EventLog, make_event
from .ops.portal_ops import portal_ops
from .risk.riskguard import RiskGuard, PortfolioState, TradeProposal
from .selflearn.engine import SelfLearningEngine
from .brain import make_brain, default_ensemble
from .config import (BRAIN_ENSEMBLE_SEED, BRAIN_MC_SAMPLES, BRAIN_SIZE_CAP)
from .selflearn.features import build_feature_vector
from .apex.eligibility import SymbolEligibility

# ---------------------------------------------------------------------------
# Locate + import the vendored APEX package.
# ---------------------------------------------------------------------------
_VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "abutron-apex" / "apex11"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))


def _load_apex():
    from apex.config import Cfg, load_config          # noqa
    from apex.core.models import Bar                    # noqa
    from abutron_replacement.bridge import (            # noqa
        AbutronApex11SignalBridge, CandleRejected, Instrument)
    from abutron_replacement.candidate_contract import (  # noqa
        AccountGate, BrokerNativeRisk, review_candidate)
    return {
        "bridge": AbutronApex11SignalBridge,
        "CandleRejected": CandleRejected,
        "Instrument": Instrument,
        "review_candidate": review_candidate,
        "AccountGate": AccountGate,
        "BrokerNativeRisk": BrokerNativeRisk,
    }


class ApexVendorMissing(RuntimeError):
    """The vendored APEX bridge source is not available on this checkout.

    The platform still runs on the NexusBrain + its own apex engine; only the
    vendored reference bridge is unavailable."""


# ---------------------------------------------------------------------------
# Instrument manifest builder
# ---------------------------------------------------------------------------
DEFAULT_DERIV_SYMBOLS = ["Volatility 100 Index.0"]
DEFAULT_FP_SYMBOLS = ["GBPUSD", "EURUSD", "XAUUSD"]

# Approved asset classes + brokers (bridge enforces these too)
_ASSET_CLASS = {"deriv": "deriv_synthetic", "fpmarkets": "fp_index"}


def build_instrument_manifest(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build the bridge's instrument manifest from registered accounts.

    Each account contributes its symbols with broker-native geometry. The
    manifest is the only thing the bridge needs to run.
    """
    manifest: List[Dict[str, Any]] = []
    seen: set = set()
    for acct in accounts:
        broker = acct.get("broker", "")
        if broker not in ("deriv", "fpmarkets"):
            continue
        symbols = [s.strip() for s in (acct.get("symbols") or "").split(",")
                   if s.strip()]
        if not symbols:
            symbols = DEFAULT_DERIV_SYMBOLS if broker == "deriv" \
                else DEFAULT_FP_SYMBOLS
        asset_class = _ASSET_CLASS.get(broker, "fx_index")
        for sym in symbols:
            key = (broker, sym)
            if key in seen:
                continue
            seen.add(key)
            manifest.append({
                "symbol": sym,
                "broker": "deriv" if broker == "deriv" else "fp_markets",
                "asset_class": asset_class,
                "tick_size": 0.01 if broker == "deriv" else 0.0001,
                "spread_bps": 20.0 if broker == "deriv" else 0.8,
                "commission_bps": 7.0,
                "annual_vol": 0.20,
                "max_weight": 0.20,
                "timeframe_seconds": 300,
            })
    return manifest


class ApexIntegration:
    """Runs the real APEX bridge + candidate review for one account."""

    def __init__(self, event_log: EventLog, *, warmup_bars: int = 0):
        self.log = event_log
        # The vendored APEX bridge is an OPTIONAL dependency. Load it here but
        # never let a missing vendor crash startup: the platform runs on the
        # NexusBrain + its own apex engine when the vendored source is absent,
        # and only uses the vendored bridge when it is actually present.
        try:
            self._apex_cache: Optional[Dict[str, Any]] = _load_apex()
            self._apex_error: str = ""
        except Exception as exc:
            self._apex_cache = None
            self._apex_error = str(exc)
        self._bridges: Dict[str, Any] = {}
        # Self-learning AI: online model + skill learner, bounded by guardrails
        self.selflearn = SelfLearningEngine(
            event_log, state_path=os.path.join(DATA_ROOT, "selflearn_state.json"))
        self.selflearn.load()
        # Hardened pre-trade RiskGuard (opt-in, fail-closed).
        self.riskguard = RiskGuard()
        # The NexusBrain — the AI calculation authority (lot sizing, entry
        # bias). Owned here so settlement (reconciler) can feed trade
        # outcomes back for self-learning.
        self.brain = make_brain(
            ensemble=default_ensemble(seed=BRAIN_ENSEMBLE_SEED),
            size_cap=BRAIN_SIZE_CAP)
        self.brain.ensemble.mc_samples = BRAIN_MC_SAMPLES
        # Equity-aware symbol eligibility: trade only the symbols the
        # client equity can carry (min-lot risk <= per-trade risk budget).
        self.eligibility = SymbolEligibility()
        # one-bar causal lag per symbol: (features, scores, regime, prev_close)
        self._pending: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Optional vendored-APEX access
    # ------------------------------------------------------------------
    @property
    def _apex(self) -> Optional[Dict[str, Any]]:
        return self._apex_cache

    def apex_available(self) -> bool:
        """True if the vendored APEX bridge source is loadable."""
        return self._apex_cache is not None

    # ------------------------------------------------------------------
    def selflearn_health(self) -> Dict[str, Any]:
        return self.selflearn.health()

    # ------------------------------------------------------------------
    def _bridge_for(self, connection_id: str, account: Dict[str, Any],
                    manifest: List[Dict[str, Any]]) -> Any:
        if not self.apex_available():
            raise ApexVendorMissing(
                f"vendored APEX source unavailable: {self._apex_error}")
        if connection_id not in self._bridges:
            inst = [m for m in manifest if m["broker"] ==
                    ("deriv" if account["broker"] == "deriv" else "fp_markets")]
            if not inst:
                raise ValueError("no approved instruments for account")
            cfg_path = str(_VENDOR / "config" / "abutron_signal_only.yaml")
            self._bridges[connection_id] = self._apex["bridge"](
                instruments=inst, config_path=cfg_path,
                warmup_bars=self._warmup)
        return self._bridges[connection_id]

    @property
    def _warmup(self) -> int:
        return 0

    # ------------------------------------------------------------------
    def process_candle(self, *, connection_id: str, account: Dict[str, Any],
                       candle: Dict[str, Any],
                       manifest: List[Dict[str, Any]],
                       orderflow_features: Optional[Dict[str, float]] = None,
                       footprint_features: Optional[Dict[str, float]] = None,
                       equity: float = 0.0,
                       native_geometry: Optional[Dict[str, Any]] = None,
                       gate_overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Feed one closed M5 candle through APEX -> review -> candidate.

        Returns a dict with: decision, status, side, candidate (or blockers).
        The `candidate` is only present when all engineering gates pass; it
        is NOT an order — it must go through the single-writer dispatcher.
        """
        # If the vendored APEX source is absent, do not crash: report a clear
        # non-trading status so the runtime keeps ticking on the NexusBrain.
        if not self.apex_available():
            self._record(connection_id, account, candle, "APEX_VENDOR_MISSING",
                         reason=self._apex_error)
            return {"status": "APEX_VENDOR_MISSING",
                    "reason": self._apex_error, "decision": None,
                    "candidate": None,
                    "blockers": [f"APEX_VENDOR_MISSING:{self._apex_error}"]}

        # --- Equity-aware symbol eligibility --------------------------------
        # Trade only the symbols the client equity can carry: if the
        # instrument's minimum lot already risks more than the per-trade
        # risk budget, the account is too small for it. Uses the static
        # representative-ATR threshold (single candle has no ATR series);
        # the bridge's own sizing is the authoritative final guard.
        _sym = candle["symbol"]
        _risk_pct = (float(account["per_trade_risk_pct"])
                     if account.get("per_trade_risk_pct") is not None
                     else self.eligibility.default_risk_pct)
        _min_eq = self.eligibility.min_equity_for(_sym, risk_pct=_risk_pct)
        if float(equity or 0.0) < _min_eq:
            self._record(connection_id, account, candle,
                         "SYMBOL_NOT_ELIGIBLE_EQUITY",
                         reason=f"EQUITY_BELOW_MIN_LOT(min={_min_eq:.2f})")
            self._trace_run(connection_id, account, candle, status="BLOCKED",
                            decision=None,
                            blockers=[f"SYMBOL_NOT_ELIGIBLE_EQUITY:"
                                      f"{_sym}:min={_min_eq:.2f}"])
            return {"status": "BLOCKED", "decision": None, "candidate": None,
                    "blockers": [f"SYMBOL_NOT_ELIGIBLE_EQUITY:{_sym}"],
                    "review": None}

        bridge = self._bridge_for(connection_id, account, manifest)

        # APEX expects closing-time ts, is_closed + authenticated provenance.
        raw = {
            "symbol": candle["symbol"],
            "ts": float(candle["close_time_epoch"]),
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle.get("volume", 0.0)),
            "is_closed": True,
            "provenance": "authenticated_mt5",
        }

        try:
            decision = bridge.on_closed_candle(raw, observed_at=time.time())
        except self._apex["CandleRejected"] as e:
            self._record(connection_id, account, candle, "REJECTED",
                         reason=f"APEX_CANDLIE_REJECTED:{e}")
            return {"status": "REJECTED", "reason": str(e),
                    "decision": None, "candidate": None, "blockers": [str(e)]}

        status = decision["status"]

        # --- Self-learning AI: causal observe -> overlay loop -------------
        # 1) Settle the previous bar's outcome and update the learner.
        # 2) Produce a bounded overlay from current features and adjust the
        #    research target weight (advisory; never touches the order).
        # Runs on every closed bar so the model keeps learning.
        symbol = candle["symbol"]
        self._run_selflearn(symbol, candle, decision, orderflow_features,
                            footprint_features)

        self._record(connection_id, account, candle, status,
                     reason=decision.get("reason", ""),
                     decision=decision,
                     orderflow=orderflow_features,
                     footprint=footprint_features)

        if status != "BUY" and status != "SELL":
            # HOLD / WARMUP — no candidate
            self._trace_run(connection_id, account, candle, status=status,
                            decision=decision,
                            gemma=decision.get("gemma"),
                            blockers=["APEX_HOLD_OR_WARMUP"])
            return {"status": status, "reason": "APEX_HOLD_OR_WARMUP",
                    "decision": decision, "candidate": None,
                    "blockers": ["APEX_HOLD_OR_WARMUP"]}

        # --- Fail-closed candidate review -------------------------------
        gate = self._build_gate(connection_id, account, equity,
                                gate_overrides or {})
        native = self._build_native(native_geometry or {}, status, candle)
        review = self._apex["review_candidate"](
            signal=decision, gate=gate, native=native, now=int(time.time()),
            per_trade_risk_pct=0.001, portfolio_ceiling_pct=0.20)

        if not review["engineering_gates_passed"]:
            self._record(connection_id, account, candle, "BLOCKED",
                         reason=";".join(review["blockers"]))
            self._trace_run(connection_id, account, candle, status="BLOCKED",
                            decision=decision, gemma=decision.get("gemma"),
                            blockers=review["blockers"])
            return {"status": "BLOCKED", "decision": decision,
                    "candidate": None, "blockers": review["blockers"],
                    "review": review}

        # --- Hardened pre-trade RiskGuard (opt-in, fail-closed) ---------
        # An independent hard-limit gate (kweinmeister pattern) runs on the
        # READY candidate before it is stored for the single-writer dispatcher.
        # Disabled by default; when on, a rejection blocks the candidate.
        if RISKGUARD_ENABLED:
            cand = review["order_candidate"]
            native_geom = native_geometry or {}
            proposal = TradeProposal(
                symbol=cand["symbol"], direction=cand["side"],
                volume_lots=float(cand.get("volume_lots", 0.0)),
                price=float(native_geom.get("price", cand.get("stop_price", 0.0))),
                stop_loss=float(cand.get("stop_price", 0.0)),
                take_profit=float(cand.get("take_price", 0.0)),
                stop_risk_usd=float(review.get("nominal_stop_risk_usd", 0.0)))
            portfolio = PortfolioState(equity=float(equity or 0.0))
            rg = self.riskguard.check_trade(proposal, portfolio)
            if not rg.approved:
                self._record(connection_id, account, candle, "BLOCKED",
                             reason=f"RISKGUARD_{rg.reason}")
                self._trace_run(connection_id, account, candle,
                                status="BLOCKED", decision=decision,
                                gemma=decision.get("gemma"),
                                riskguard=rg.reason,
                                blockers=[f"RISKGUARD_{rg.reason}"])
                return {"status": "BLOCKED", "decision": decision,
                        "candidate": None,
                        "blockers": [f"RISKGUARD_{rg.reason}"],
                        "review": review, "riskguard": rg.to_dict()}

        signal_id = review["order_candidate"].get("signal_id", "")
        self._trace_run(connection_id, account, candle, status="READY",
                        decision=decision, signal_id=signal_id,
                        gemma=decision.get("gemma"),
                        riskguard="approved" if RISKGUARD_ENABLED else "")
        return {"status": "READY", "decision": decision,
                "candidate": review["order_candidate"],
                "review": review, "blockers": []}

    # ------------------------------------------------------------------
    def _run_selflearn(self, symbol: str, candle: Dict[str, Any],
                       decision: Dict[str, Any],
                       orderflow_features: Optional[Dict[str, float]],
                       footprint_features: Optional[Dict[str, float]]) -> None:
        """Causal self-learning step for one symbol's closed candle.

        Observes the prior bar's outcome (updating the online model and
        skill learner), then produces a bounded overlay and adjusts the
        decision's research target weight. Advisory only.
        """
        sl = self.selflearn
        close = float(candle["close"])
        regime = (decision.get("regime", {}) or {}).get("regime", "")

        # agent scores from the decision signals
        agent_scores: Dict[str, float] = {}
        for s in decision.get("signals", []):
            agent_scores[s.get("agent", "?")] = float(s.get("score", 0.0))

        # 1) settle prior bar's outcome if we have a pending observation
        pend = self._pending.pop(symbol, None)
        if pend is not None:
            prev_close = pend.get("prev_close", 0.0)
            if prev_close > 0 and close > 0:
                realised_ret = close / prev_close - 1.0
                summary = sl.observe_outcome(
                    symbol=symbol, features=pend["features"],
                    realised_ret=realised_ret, regime=pend["regime"],
                    agent_scores=pend["agent_scores"])
                self.log.append(make_event(
                    "selflearn.observe", symbol=symbol,
                    execution_state="UPDATED",
                    payload={"realised_ret": round(realised_ret, 6),
                             "model_error": round(summary.get("error", 0.0), 6),
                             "observations": sl.observations}))

        # 2) build current features, produce bounded overlay, adjust weight
        feats = build_feature_vector(
            orderflow=orderflow_features, footprint=footprint_features,
            regime=regime, agent_scores=agent_scores)
        overlay = sl.produce_overlay(
            symbol=symbol, orderflow=orderflow_features,
            footprint=footprint_features, regime=regime,
            agent_scores=agent_scores)

        # adjust the decision's research target weight (advisory)
        weight = float(decision.get("target_weight_research_only", 0.0))
        adjusted = weight * overlay.multiplier * overlay.per_symbol.get(
            symbol, 1.0)
        decision["target_weight_research_only"] = round(adjusted, 5)
        decision["selflearn_overlay"] = overlay.to_dict()

        self.log.append(make_event(
            "selflearn.overlay", symbol=symbol,
            execution_state="APPLIED",
            reason_code=";".join(overlay.notes) or "OK",
            payload={"weight_before": round(weight, 5),
                     "weight_after": round(adjusted, 5),
                     "multiplier": overlay.multiplier,
                     "clipped": overlay.clipped}))

        # 3) store pending for next bar's observation
        self._pending[symbol] = {
            "features": feats, "agent_scores": agent_scores,
            "regime": regime, "prev_close": close,
        }

    # ------------------------------------------------------------------
    def _build_gate(self, connection_id: str, account: Dict[str, Any],
                    equity: float, overrides: Dict[str, Any]) -> Any:
        """Construct the AccountGate from live CRM/native state.

        All booleans default to the *fail-closed* value (False) so a
        missing attestation blocks the candidate. The production supervisor
        fills these from real native evidence.
        """
        Gate = self._apex["AccountGate"]
        owner = overrides.get("owner", account.get("owner_id", ""))
        conn = overrides.get("connection_id", connection_id)
        broker = "deriv" if account["broker"] == "deriv" else "fp_markets"
        # connection/owner must be 32-hex for the contract; pad/hash as needed
        owner_h = _to_hex32(owner)
        conn_h = _to_hex32(conn)
        server = overrides.get("server", "Deriv-Demo" if broker == "deriv"
                               else "FPMarketsSC-Demo")
        return Gate(
            broker=broker,
            environment=overrides.get("environment", "DEMO"),
            server=server,
            owner=owner_h,
            connection_id=conn_h,
            account_bound=bool(overrides.get("account_bound", False)),
            worker_nonce_bound=bool(overrides.get("worker_nonce_bound", False)),
            fresh_broker_session=bool(overrides.get("fresh_broker_session", False)),
            native_ea_account_match=bool(overrides.get("native_ea_account_match",
                                                       False)),
            broker_trade_allowed=bool(overrides.get("broker_trade_allowed", False)),
            terminal_trade_allowed=bool(overrides.get("terminal_trade_allowed",
                                                      False)),
            customer_switch_desired=bool(overrides.get("customer_switch_desired",
                                                      account.get("desired_on", 0))),
            customer_switch_effective=bool(overrides.get("customer_switch_effective",
                                                        account.get("effective_on", 0))),
            crm_entitlement_approved=bool(overrides.get("crm_entitlement_approved",
                                                       False)),
            single_writer_exclusive=bool(overrides.get("single_writer_exclusive",
                                                       False)),
            existing_pending_orders=int(overrides.get("existing_pending_orders", 0)),
            broker_positions_protected=bool(overrides.get("broker_positions_protected",
                                                          False)),
            broker_exposure_reconciled=bool(overrides.get("broker_exposure_reconciled",
                                                          False)),
            account_equity_usd=float(equity or overrides.get("account_equity_usd", 0.0)),
            current_verified_stop_risk_usd=float(overrides.get(
                "current_verified_stop_risk_usd", 0.0)),
            order_history_uncertain=bool(overrides.get("order_history_uncertain",
                                                       False)),
            kill_switch_halted=bool(overrides.get("kill_switch_halted", False)),
            live_utc=int(overrides.get("live_utc", time.time())),
        )

    def _build_native(self, geom: Dict[str, Any], side: str,
                      candle: Dict[str, Any]) -> Any:
        """Construct BrokerNativeRisk from broker-native geometry."""
        Nat = self._apex["BrokerNativeRisk"]
        return Nat(
            tick_size=float(geom.get("tick_size", 0.0001)),
            tick_value_per_lot_usd=float(geom.get("tick_value_per_lot_usd", 10.0)),
            min_lot=float(geom.get("min_lot", 0.01)),
            max_lot=float(geom.get("max_lot", 100.0)),
            lot_step=float(geom.get("lot_step", 0.01)),
            min_stop_ticks=int(geom.get("min_stop_ticks", 1)),
            broker_verified=bool(geom.get("broker_verified", False)),
            price=float(geom.get("price", candle["close"])),
            stop_price=float(geom.get("stop_price", 0.0)),
            take_price=float(geom.get("take_price", 0.0)),
            slippage_budget_ticks=int(geom.get("slippage_budget_ticks", 1)),
            broker_calc_profit_verified=bool(geom.get("broker_calc_profit_verified",
                                                     False)),
            broker_order_check_verified=bool(geom.get("broker_order_check_verified",
                                                     False)),
        )

    # ------------------------------------------------------------------
    def store_ready_candidate(self, *, connection_id: str, account: Dict[str, Any],
                              ready: Dict[str, Any],
                              candle: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Persist a READY candidate as RESERVED for the dispatcher.

        Uses the candidate contract's canonical signal_id as the replay key
        (at-most-once). Returns the stored row, or None if the replay key
        was already consumed (CANDLE_ALREADY_CONSUMED).
        """
        from .. import db
        from ..db import engine_db
        cand = ready["candidate"]
        signal_id = cand["signal_id"]
        side = cand["side"]
        row = {
            "candidate_id": f"cand_{signal_id[:12]}",
            "connection_id": connection_id,
            "owner_id": account.get("owner_id", ""),
            "broker": cand["broker"],
            "symbol": cand["symbol"],
            "timeframe": "M5",
            "bar_open": candle.get("open_time", ""),
            "replay_key": signal_id,
            "direction": side,
            "conviction": float(ready.get("review", {}).get("risk_budget_usd", 0.0)
                               / max(1.0, account.get("equity", 1.0))),
            "regime": (ready.get("decision", {}).get("regime", {}) or {}).get(
                "regime", ""),
            "agent_votes": str(ready.get("decision", {})),
            "risk_fraction": 0.001,
            "stop_risk_usd": float(ready.get("review", {}).get(
                "nominal_stop_risk_usd", 0.0)),
            "volume": float(cand.get("volume_lots", 0.0)),
            "stop_loss": float(cand.get("stop_price", 0.0)),
            "take_profit": float(cand.get("take_price", 0.0)),
            "execution_state": "RESERVED",
            "reason_code": "APEX11_CANDIDATE_READY",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if not engine_db.add_candidate(row):
            self.log.append(make_event(
                "candidate.duplicate", connection_id=connection_id,
                broker=cand["broker"], symbol=cand["symbol"],
                replay_key=signal_id, execution_state="DUPLICATE",
                reason_code="CANDLE_ALREADY_CONSUMED"))
            return None
        self.log.append(make_event(
            "candidate.reserved", connection_id=connection_id,
            broker=cand["broker"], owner_id=account.get("owner_id"),
            symbol=cand["symbol"], timeframe="M5",
            native_bar_open=candle.get("open_time"),
            closed_at=candle.get("close_time"),
            replay_key=signal_id, apex_version="APEX_1_1_0",
            regime=row["regime"],
            conviction=row["conviction"],
            risk_fraction=row["risk_fraction"],
            stop_risk_usd=row["stop_risk_usd"],
            volume=row["volume"],
            execution_state="RESERVED"))
        return row

    def _record(self, connection_id, account, candle, status, reason,
                decision=None, orderflow=None, footprint=None) -> None:
        self.log.append(make_event(
            "apex.integration", connection_id=connection_id,
            broker=account.get("broker"), mt5_login=account.get("mt5_login"),
            symbol=candle.get("symbol"),
            native_bar_open=candle.get("open_time"),
            closed_at=candle.get("close_time"),
            execution_state=status, reason_code=reason,
            payload={"decision": decision, "orderflow": orderflow,
                     "footprint": footprint}))

    def _trace_run(self, connection_id, account, candle, *, status,
                   decision=None, signal_id="", gemma=None,
                   riskguard="", blockers=None) -> None:
        """Record a structured run/trace row for one decision cycle."""
        gemma = gemma or {}
        dec = decision if isinstance(decision, dict) else {}
        # Regime may be a string or a dict — coerce to a short string.
        regime = dec.get("regime", "")
        if isinstance(regime, dict):
            regime = str(regime.get("name", regime.get("regime", "")))
        portal_ops.record_run(
            connection_id=connection_id,
            owner_id=account.get("owner_id", ""),
            symbol=candle.get("symbol", ""),
            signal_id=signal_id or str(dec.get("decision_id", "")),
            decision=str(dec.get("status", status) or status),
            status=status,
            regime=str(regime or ""),
            conviction=float(dec.get("conviction", 0.0) or 0.0),
            gemma_signal=str(gemma.get("signal", "")),
            gemma_confidence=float(gemma.get("confidence", 0.0) or 0.0),
            riskguard=riskguard, blockers=blockers)


def _to_hex32(s: str) -> str:
    """Coerce an arbitrary id to a 32-hex string (SHA256, truncated).

    The candidate contract requires exactly 32 lowercase hex chars for
    owner / connection_id.
    """
    import hashlib
    return hashlib.sha256(str(s).encode()).hexdigest()[:32]
