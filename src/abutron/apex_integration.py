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

import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import (DATA_ROOT, RISKGUARD_ENABLED, DEFAULT_PER_TRADE_RISK_PCT,
                     DEFAULT_PORTFOLIO_RISK_CAP_PCT, BRAIN_MODEL_DIR)
from .events import EventLog, make_event
from .ops.portal_ops import portal_ops
from .risk.riskguard import RiskGuard, PortfolioState, TradeProposal
from .selflearn.engine import SelfLearningEngine
from .brain import make_brain
from .config import BRAIN_MC_SAMPLES, BRAIN_SIZE_CAP
from .selflearn.features import build_feature_vector
from .apex.sniper import SniperEntryEngine
from .apex.trailing import TrailingStop

# ---------------------------------------------------------------------------
# Locate + import the vendored APEX package.
# ---------------------------------------------------------------------------
_VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "abutron-apex" / "apex11"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))


def _load_apex():
    """Import the vendored bridge, adding its extra source roots to sys.path.

    `apex11/` provides the `apex` package and `apex11/` provides
    `abutron_replacement`; the native broker-authority modules additionally
    import `risk_guard` from the repository root (a sibling of `apex11`).
    Both roots are registered here so the vendored tree imports correctly
    regardless of how the process was launched (systemd, CLI, pytest).
    """
    for root in (_VENDOR, _VENDOR.parent):
        if root.is_dir() and str(root) not in sys.path:
            sys.path.insert(0, str(root))
    from apex.config import Cfg, load_config          # noqa
    from apex.core.models import Bar                    # noqa
    from abutron_replacement.bridge import (            # noqa
        AbutronApex11SignalBridge, CandleRejected, Instrument)
    from .apex.v9_candidate_contract import (  # noqa
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


class ApexManifestError(RuntimeError):
    """The instrument manifest cannot be turned into a valid APEX bridge.

    This is a *configuration* fault (e.g. an unapproved symbol, an
    incomplete geometry record, or no instruments for the account's broker).
    It must degrade to a reported non-trading status rather than propagating
    out of the engine tick loop.
    """


# ---------------------------------------------------------------------------
# Instrument manifest builder
# ---------------------------------------------------------------------------
DEFAULT_DERIV_SYMBOLS = [
    *(f"{name} {level} Index.0" for name in ("Boom", "Crash")
      for level in (150, 300, 600, 1000)),
    *(f"Volatility {level} Index.0" for level in (10, 25, 50, 75, 100)),
]
DEFAULT_FP_SYMBOLS = ["GBPUSD", "EURUSD", "XAUUSD"]

# Approved asset classes + brokers.
# The vendored bridge (`abutron_replacement.bridge.Instrument.parse`) only
# approves `deriv_synthetic` (Deriv) and `fx_index` (FP Markets). Using any
# other label makes every FP instrument fail manifest parsing, so the FP
# bridge could never be constructed. Keep these in lockstep with the
# vendored contract.
_ASSET_CLASS = {"deriv": "deriv_synthetic", "fpmarkets": "fx_index"}


def _account_risk_fraction(account: Dict[str, Any], key: str,
                           default_percent: float) -> float:
    """Convert CRM percentage-points (for example 0.5 means 0.5%) to a fraction.

    Invalid or nonpositive overrides become zero so candidate review and lot
    sizing fail closed rather than silently substituting a larger default.
    """
    raw = account.get(key, default_percent)
    if raw is None or raw == "":
        raw = default_percent
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return value / 100.0


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
        # Load only a persisted, calibrated model. A freshly initialized
        # random ensemble or the deterministic fallback is not an acceptable
        # live directional authority. Without an artifact, decisions HOLD.
        from .brain.unified_adapter import load_unified_49
        self.brain_load_error = "TRAINED_UNIFIED_BRAIN_NOT_CONFIGURED"
        try:
            self.brain = load_unified_49(
                BRAIN_MODEL_DIR, mc_samples=BRAIN_MC_SAMPLES)
            self.brain_load_error = ""
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.brain = make_brain(ensemble=None, size_cap=BRAIN_SIZE_CAP)
            self.brain_load_error = (
                f"TRAINED_BRAIN_UNAVAILABLE:{type(exc).__name__}")
        self._signal_history: Dict[str, List[Dict[str, float]]] = {}
        self._signal_history_ts: Dict[str, int] = {}
        # one-bar causal lag per symbol: (features, scores, regime, prev_close)
        self._pending: Dict[str, Dict[str, Any]] = {}
        # Sniper entry + trailing-stop geometry (the entry-precision layer).
        self.sniper = SniperEntryEngine()
        self.trailing = TrailingStop()
        # Rolling OHLC history per symbol so the sniper can find structural
        # levels (swing / EMA / Donchian) without waiting for the feed layer
        # to hand over a full history. Bounded; oldest bars evicted first.
        self._history: Dict[str, List[Dict[str, float]]] = {}

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

    def _brain_direction(self, *, symbol: str, candle: Dict[str, Any],
                         apex_decision: Dict[str, Any],
                         orderflow_features: Optional[Dict[str, float]],
                         footprint_features: Optional[Dict[str, float]]
                         ) -> tuple[str, Dict[str, Any], str]:
        """Return a trained-model BUY/SELL/HOLD; never use APEX direction.

        History is kept causal and deduplicated by bar close time. A cold or
        untrained brain abstains rather than falling back to random weights or
        the bridge's direction.
        """
        try:
            close_ts = int(float(candle["close_time_epoch"]))
            bar = {key: float(candle[key]) for key in ("open", "high", "low", "close")}
            bar["volume"] = float(candle.get("volume", 0.) or 0.)
            bar["open_time"] = candle.get("open_time", "")
        except (KeyError, TypeError, ValueError, OverflowError):
            return "HOLD", {}, "BRAIN_INPUT_INVALID"
        if close_ts > self._signal_history_ts.get(symbol, 0):
            history = self._signal_history.setdefault(symbol, [])
            history.append(bar)
            del history[:-120]
            self._signal_history_ts[symbol] = close_ts

        if not self.brain.trained:
            return "HOLD", {"trained": False,
                             "model_version": self.brain.VERSION}, self.brain_load_error
        history = self._signal_history.get(symbol, [])
        if len(history) < 32:
            return "HOLD", {"trained": True, "bars": len(history)}, \
                "UNIFIED_BRAIN_WARMUP"

        # Rebuild the causal full-signal feature slots used by training;
        # missing true tick/depth data is explicitly represented by OHLCV
        # proxies, rather than silently zeroing 9 learned feature slots.
        from .brain.signals import latest_signal_features
        snapshot = latest_signal_features(history, symbol=symbol,
                                          use_gemma=False)
        proposal = self.brain.decide(
            symbol=symbol,
            closes=[row["close"] for row in history],
            highs=[row["high"] for row in history],
            lows=[row["low"] for row in history],
            orderflow=orderflow_features or snapshot["orderflow"],
            footprint=footprint_features or snapshot["footprint"],
            agent_scores=snapshot["agent_scores"],
            regime=snapshot["regime"])
        payload = proposal.to_dict()
        if not proposal.trained:
            return "HOLD", payload, "TRAINED_BRAIN_REQUIRED"
        if any(str(reason).startswith("LOSS_PATTERN_REPEAT")
               for reason in (proposal.blockers or [])):
            return "HOLD", payload, "LOSS_PATTERN_REPEAT"
        direction = proposal.direction if proposal.direction in ("BUY", "SELL") else "HOLD"
        reason = "" if direction != "HOLD" else ";".join(proposal.blockers) or "UNIFIED_BRAIN_HOLD"
        return direction, payload, reason

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
                raise ApexManifestError(
                    f"no approved instruments for {account['broker']} account")
            cfg_path = str(_VENDOR / "config" / "abutron_signal_only.yaml")
            try:
                self._bridges[connection_id] = self._apex["bridge"](
                    instruments=inst, config_path=cfg_path,
                    warmup_bars=self._warmup)
            except Exception as exc:
                # A rejected manifest (unapproved symbol/asset class, bad
                # geometry, duplicate symbols) is a configuration fault: report
                # it per-candle instead of killing the whole engine tick.
                raise ApexManifestError(str(exc)) from exc
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

        try:
            bridge = self._bridge_for(connection_id, account, manifest)
        except ApexManifestError as exc:
            reason = f"MANIFEST_INVALID:{exc}"
            self._record(connection_id, account, candle, "MANIFEST_INVALID",
                         reason=reason)
            self._trace_run(connection_id, account, candle,
                            status="MANIFEST_INVALID", blockers=[reason])
            return {"status": "MANIFEST_INVALID", "reason": reason,
                    "decision": None, "candidate": None,
                    "blockers": [reason]}

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

        apex_status = decision["status"]

        # --- Self-learning AI: causal observe -> overlay loop -------------
        # 1) Settle the previous bar's outcome and update the learner.
        # 2) Produce a bounded overlay from current features and adjust the
        #    research target weight (advisory; never touches the order).
        # Runs on every closed bar so the model keeps learning.
        symbol = candle["symbol"]
        self._run_selflearn(symbol, candle, decision, orderflow_features,
                            footprint_features)

        # The trained NexusBrain is the sole direction authority. The APEX
        # bridge remains a causal feature/evidence source; its BUY/SELL is not
        # allowed to create candidates. Missing/un-calibrated brain artifacts
        # and insufficient history fail closed to HOLD.
        status, brain_payload, brain_reason = self._brain_direction(
            symbol=symbol, candle=candle, apex_decision=decision,
            orderflow_features=orderflow_features,
            footprint_features=footprint_features)
        decision["apex_status"] = apex_status
        decision["status"] = status
        decision["brain"] = brain_payload
        if brain_reason:
            decision["reason"] = brain_reason

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

        # --- AI entry pipeline: sniper geometry + trail + brain sizing ----
        # The APEX direction decision is an *edge*, not an entry price. Turn
        # it into a precise, trigger-based entry plan (LIMIT pullback or
        # CONFIRM continuation) with a tight invalidation and take-profit,
        # plus the trailing-stop leg and the brain-computed lot size.
        risk_fraction = _account_risk_fraction(
            account, "per_trade_risk_pct", DEFAULT_PER_TRADE_RISK_PCT)
        portfolio_fraction = _account_risk_fraction(
            account, "portfolio_cap_pct", DEFAULT_PORTFOLIO_RISK_CAP_PCT)
        plan = self.build_entry_plan(
            symbol=symbol, candle=candle, side=status, decision=decision,
            orderflow_features=orderflow_features,
            footprint_features=footprint_features, equity=equity,
            risk_pct=risk_fraction,
            native_geometry=native_geometry)
        review_geom = self.review_geometry(
            candle=candle, plan=plan, native_geometry=native_geometry)

        # --- Fail-closed candidate review -------------------------------
        gate = self._build_gate(connection_id, account, equity,
                                gate_overrides or {})
        native = self._build_native(review_geom, status, candle)
        review = self._apex["review_candidate"](
            signal=decision, gate=gate, native=native, now=int(time.time()),
            per_trade_risk_pct=risk_fraction,
            portfolio_ceiling_pct=portfolio_fraction)

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
            native_geom = review_geom
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

        # Attach the entry plan to the candidate so the dispatcher forwards
        # the real sniper/trailing geometry and the exact lot to the EA
        # (the contract itself only carries price/stop/take/volume).
        order_cand = dict(review["order_candidate"])
        # The contract validated a lot count against the 0.10% per-trade risk
        # policy AND the 20% portfolio ceiling. The brain's quality-scaled lot
        # may be larger, so it is clamped to that validated bound — sizing can
        # never breach the ceiling the contract just proved safe.
        contract_lots = float(order_cand.get("volume_lots") or 0.0)
        plan_lots = float(plan["lots"] or 0.0)
        exec_lots = min(plan_lots, contract_lots) if contract_lots > 0 \
            else plan_lots
        order_cand.update({
            "entry_type": plan["entry_type"],
            "trigger_price": plan["trigger_price"],
            "invalidation": plan["invalidation"],
            "entry_quality": plan["entry_quality"],
            "sniper_plan": plan["sniper_plan"],
            "trail_k": plan["trail_k"],
            "trail_stop": plan["trail_stop"],
            "trail_plan": plan["trail_plan"],
            # Executed lot — never above the contract's validated volume.
            "volume_lots": exec_lots,
            "contract_volume_lots": contract_lots,
            "stop_risk_usd": float(plan["stop_risk_usd"]),
            "sizing_capped_by_contract": plan_lots > contract_lots > 0,
        })

        signal_id = order_cand.get("signal_id", "")
        self._trace_run(connection_id, account, candle, status="READY",
                        decision=decision, signal_id=signal_id,
                        gemma=decision.get("gemma"),
                        riskguard="approved" if RISKGUARD_ENABLED else "")
        return {"status": "READY", "decision": decision,
                "candidate": order_cand, "entry_plan": plan,
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

    # ------------------------------------------------------------------
    # AI entry pipeline — sniper geometry, trailing stop, brain lot sizing
    # ------------------------------------------------------------------
    HISTORY_BARS = 120

    def _push_history(self, symbol: str, candle: Dict[str, Any]) -> None:
        """Append a closed bar to the rolling per-symbol OHLC history."""
        try:
            bar = {"open": float(candle["open"]), "high": float(candle["high"]),
                   "low": float(candle["low"]), "close": float(candle["close"])}
        except (KeyError, TypeError, ValueError):
            return
        if not all(v > 0 for v in bar.values()):
            return
        hist = self._history.setdefault(symbol, [])
        hist.append(bar)
        if len(hist) > self.HISTORY_BARS:
            del hist[: len(hist) - self.HISTORY_BARS]

    def _symbol_history(self, symbol: str,
                        candle: Dict[str, Any]) -> List[Dict[str, float]]:
        """Return the rolling OHLC history for a symbol (seeded if missing).

        The rolling buffer is authoritative once it holds bars. On a cold
        start the feed may hand over a warm-up slice on the candle itself
        (`history`); it is adopted once, then superseded by the buffer.
        """
        if not self._history.get(symbol):
            for row in (candle.get("history") or [])[-self.HISTORY_BARS:]:
                self._push_history(symbol, row)
        return self._history.setdefault(symbol, [])

    def build_entry_plan(self, *, symbol: str, candle: Dict[str, Any],
                         side: str, decision: Dict[str, Any],
                         orderflow_features: Optional[Dict[str, float]] = None,
                         footprint_features: Optional[Dict[str, float]] = None,
                         equity: float = 0.0,
                         risk_pct: float = DEFAULT_PER_TRADE_RISK_PCT,
                         native_geometry: Optional[Dict[str, Any]] = None
                         ) -> Dict[str, Any]:
        """Compute the precise entry plan the EA executes.

        This is the AI calculation stage between the APEX direction decision
        and the order candidate:

          1. **Sniper entry** — a LIMIT pullback to a structural level, or a
             CONFIRM continuation trigger, with a tight invalidation and a
             tight take-profit (no market-at-close entries).
          2. **Trailing stop** — the Chandelier trail leg the EA applies to
             lock profit in favour.
          3. **Brain lot sizing** — lots from client equity, the per-trade
             risk budget and the *sniper stop distance*, scaled by entry
             quality. The EA only executes the computed lot.

        Never raises: with no history the sniper falls back to a
        close-anchored plan, and the candidate contract then decides (and
        blocks) on geometry it cannot verify.
        """
        from .apex.indicators import atr as _atr

        hist = self._symbol_history(symbol, candle)
        self._push_history(symbol, candle)
        closes = [b["close"] for b in hist] or [float(candle.get("close") or 0.0)]
        highs = [b["high"] for b in hist] or [float(candle.get("high") or 0.0)]
        lows = [b["low"] for b in hist] or [float(candle.get("low") or 0.0)]
        a = float(_atr(highs, lows, closes, 14) or 0.0)
        if a <= 0:
            # No usable ATR (cold start / flat tape): fall back to a small
            # fraction of price so the plan still has a finite geometry.
            a = max(abs(closes[-1]) * 0.001, 1e-8)

        raw_regime = decision.get("regime", "")
        if isinstance(raw_regime, dict):
            regime = str(raw_regime.get("name", raw_regime.get("regime", "")) or "")
        else:
            regime = str(raw_regime or "")

        bias = self.brain.get_entry_bias(symbol, regime)
        sniper = self.sniper.plan(
            symbol=symbol, direction=side, closes=closes, highs=highs,
            lows=lows, atr_val=a,
            of_features=orderflow_features,
            fp_features=footprint_features,
            regime=regime,
            uncertainty=float(decision.get("uncertainty", 0.0) or 0.0),
            conviction=float(decision.get("conviction", 0.0) or 0.0),
            confirm_bias=float(bias.get("confirm_bias", 0.0) or 0.0),
            quality_floor=float(bias.get("quality_floor", 0.0) or 0.0))

        trail = self.trailing.plan(
            direction=side, highs=highs[-20:], lows=lows[-20:], atr_val=a,
            entry_price=sniper.trigger_price)

        # --- Brain lot sizing (equity + risk + sniper stop distance) ---
        geom = native_geometry or {}
        tick_size = float(geom.get("tick_size", 0.0) or 0.0)
        tick_value = float(geom.get("tick_value_per_lot_usd", 0.0) or 0.0)
        stop_ticks = abs(sniper.trigger_price - sniper.invalidation) / tick_size \
            if tick_size > 0 else 0.0
        sizing = self.brain.size_position(
            equity=float(equity or 0.0),
            risk_pct=float(risk_pct or DEFAULT_PER_TRADE_RISK_PCT),
            stop_distance_points=stop_ticks,
            point_value=tick_value,
            lot_step=float(geom.get("lot_step", 0.01) or 0.01),
            lot_min=float(geom.get("min_lot", 0.01) or 0.01),
            lot_max=float(geom.get("max_lot", 50.0) or 50.0),
            entry_quality=sniper.entry_quality)

        plan = {
            "entry_type": sniper.entry_type,
            "trigger_price": sniper.trigger_price,
            "invalidation": sniper.invalidation,
            "take_profit": sniper.take_profit,
            "entry_quality": sniper.entry_quality,
            "sniper_plan": sniper.to_dict(),
            "trail_k": trail.trail_k,
            "trail_stop": trail.trail_stop,
            "trail_plan": trail.to_dict(),
            "lots": float(sizing.get("lots", 0.0) or 0.0),
            "stop_risk_usd": float(sizing.get("risk_usd", 0.0) or 0.0),
            "sizing": sizing,
            "atr": round(a, 8),
            "bars": len(hist),
            "regime": regime,
        }
        self.log.append(make_event(
            "apex.entry_plan", symbol=symbol, execution_state="PLANNED",
            reason_code=sniper.entry_type, payload=plan))
        return plan

    def review_geometry(self, *, candle: Dict[str, Any], plan: Dict[str, Any],
                        native_geometry: Optional[Dict[str, Any]] = None
                        ) -> Dict[str, Any]:
        """Merge the entry plan into the broker-native review geometry.

        The candidate contract needs a price/stop/take triple that respects
        the side ordering. The plan supplies the sniper trigger and the tight
        stop/TP; the broker-native fields (tick size, lot bounds, verification
        flags) come from the terminal. When the terminal has not attested the
        symbol those flags stay False and the contract blocks — fail-closed,
        never a fabricated fill.
        """
        geom = dict(native_geometry or {})
        geom["price"] = float(plan.get("trigger_price") or candle.get("close") or 0.0)
        geom["stop_price"] = float(plan.get("invalidation") or 0.0)
        geom["take_price"] = float(plan.get("take_profit") or 0.0)
        return geom

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
            "conviction": float((ready.get("decision", {}).get("brain", {})
                                  or {}).get("conviction", 0.0) or 0.0),
            "regime": (ready.get("decision", {}).get("regime", {}) or {}).get(
                "regime", ""),
            "agent_votes": str(ready.get("decision", {})),
            "risk_fraction": float(ready.get("review", {}).get("risk_fraction", 0.0)),
            "stop_risk_usd": float(ready.get("review", {}).get(
                "nominal_stop_risk_usd", 0.0)),
            "volume": float(cand.get("volume_lots", 0.0)),
            "stop_loss": float(cand.get("stop_price", 0.0)),
            "take_profit": float(cand.get("take_price", 0.0)),
            # Sniper entry + trailing geometry (the AI entry plan). Persisted
            # so the dispatcher forwards a LIMIT trigger price and the trail
            # leg to the EA instead of a market-at-close fill.
            "price": float(cand.get("trigger_price", 0.0)),
            "entry_type": str(cand.get("entry_type", "") or ""),
            "trigger_price": float(cand.get("trigger_price", 0.0)),
            "invalidation": float(cand.get("invalidation", 0.0)),
            "entry_quality": float(cand.get("entry_quality", 0.0)),
            "sniper_plan": json.dumps(cand.get("sniper_plan", {}), default=str),
            "trail_k": float(cand.get("trail_k", 0.0)),
            "trail_stop": float(cand.get("trail_stop", 0.0)),
            "trail_plan": json.dumps(cand.get("trail_plan", {}), default=str),
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
