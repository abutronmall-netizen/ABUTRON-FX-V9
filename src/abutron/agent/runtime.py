"""DEMO-gated agentic runtime for ABUTRON V9.

Replaces the deployed ``agentic_runtime_ro.py`` empty-candles smoke test
with a real broker-native decision pass, gated by connection environment
and activation state.

Gating rules (fail-closed):

* **Shadow decision** (real M5 candles + quotes -> brain): runs for any
  authenticated DEMO connection. LIVE connections may run the shadow
  pass read-only but will never dispatch orders.
* **Order dispatch**: only DEMO connections with ``effective_on=1`` and
  independently certified exposure may dispatch via the existing
  ``NativeDispatcher``. LIVE connections are blocked unless
  ``effective_on=1`` AND a distinct live authorization exists.
* **HOLD/reject preserved**: the brain's actual decision is recorded.
  No BUY/SELL is forced and no candidate is fabricated.
* **No fake ACK**: broker execution status is distinct from the brain
  decision. A candidate is NOT a broker-certified fill.

The existing ``AgenticDecisionEngine``, ``BrokerNativeMarketDataAdapter``,
``NativeDispatcher``, RiskGate and CRM are used unchanged — this module
wires them together with the correct gating.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..db import engine_db
from ..events import make_event
from ..execution.dispatcher import NativeDispatcher
from ..execution.receipts import ReceiptManager
from ..execution.writer_gateway import SingleWriterGateway
from .market_data import (BrokerNativeMarketDataAdapter, MarketDataError,
                          run_shadow_decision_pass)
from .operator_override import OperatorOverride, load_override
from .production import AgenticDecisionEngine


@dataclass
class AgenticRuntimeResult:
    """Result of one agentic runtime pass."""

    connection_id: str
    environment: str
    effective_on: bool
    decision_mode: str
    evaluated_symbols: List[str] = field(default_factory=list)
    candle_count: int = 0
    quote_count: int = 0
    candidates_generated: int = 0
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    dispatch_results: List[Dict[str, Any]] = field(default_factory=list)
    dispatch_blocked: bool = False
    dispatch_reason: str = ""
    broker_native_signal_certified: bool = False
    shadow_decision_evaluated: bool = False
    source_hash: str = ""
    source_ts: float = 0.0
    persisted_candidate_ids: List[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "environment": self.environment,
            "effective_on": self.effective_on,
            "decision_mode": self.decision_mode,
            "evaluated_symbols": self.evaluated_symbols,
            "candle_count": self.candle_count,
            "quote_count": self.quote_count,
            "candidates_generated": self.candidates_generated,
            "candidates": self.candidates,
            "dispatch_results": self.dispatch_results,
            "dispatch_blocked": self.dispatch_blocked,
            "dispatch_reason": self.dispatch_reason,
            "broker_native_signal_certified": self.broker_native_signal_certified,
            "shadow_decision_evaluated": self.shadow_decision_evaluated,
            "source_hash": self.source_hash,
            "source_ts": self.source_ts,
            "persisted_candidate_ids": self.persisted_candidate_ids,
            "error": self.error,
        }


def _load_account(connection_id: str) -> Optional[Dict[str, Any]]:
    """Load the account row, or None if not found."""
    return engine_db.get_account(connection_id)


def _is_demo(account: Dict[str, Any]) -> bool:
    return str(account.get("environment", "")).lower() == "demo"


def _is_live_authorized(connection_id: str,
                        account: Dict[str, Any]) -> bool:
    """Check distinct live authorization.

    LIVE requires ``effective_on=1`` AND an authenticated live intent
    record. DEMO effective-ON does NOT authorize LIVE trading.
    """
    if not account.get("effective_on"):
        return False
    intent = engine_db.get_live_intent(connection_id)
    if intent is None:
        return False
    return bool(intent.get("authorized", 0))




def _persist_candidates(
    candidates: List[Any],
    *,
    connection_id: str,
    event_log: Any,
) -> List[str]:
    """Persist brain-generated candidates into the dispatch queue.

    Converts each ``Candidate`` to a ``CandidateRow`` dict via
    ``to_row()`` and persists it with ``execution_state="RESERVED"`` so
    the existing ``NativeDispatcher.dispatch_account()`` can pick it up.

    Returns the list of persisted candidate IDs. Fail-closed: if
    persistence fails for a candidate, it is skipped and logged.
    """
    from ..apex.engine import Candidate

    persisted_ids: List[str] = []
    for cand in candidates:
        if not isinstance(cand, Candidate):
            continue
        cand.reserved = True
        row = cand.to_row()
        row["execution_state"] = "RESERVED"
        try:
            if engine_db.add_candidate(row):
                persisted_ids.append(cand.candidate_id)
                event_log.append(make_event(
                    "agentic.candidate_persisted",
                    connection_id=connection_id,
                    order_request_id=cand.candidate_id,
                    execution_state="RESERVED",
                    reason_code="DECISION_TO_DISPATCH_CONNECTED",
                    payload={
                        "symbol": cand.symbol,
                        "direction": cand.direction,
                        "volume": cand.volume,
                        "stop_loss": cand.stop_loss,
                        "take_profit": cand.take_profit,
                    }))
            else:
                # Duplicate replay_key (at-most-once) — skip, don't re-add.
                event_log.append(make_event(
                    "agentic.candidate_duplicate",
                    connection_id=connection_id,
                    order_request_id=cand.candidate_id,
                    execution_state="RESERVED",
                    reason_code="DUPLICATE_REPLAY_KEY",
                    payload={"replay_key": cand.replay_key}))
        except Exception as exc:
            event_log.append(make_event(
                "agentic.candidate_persist_error",
                connection_id=connection_id,
                order_request_id=cand.candidate_id,
                execution_state="PERSIST_ERROR",
                reason_code="CANDIDATE_PERSIST_FAILED",
                payload={"error": str(exc)}))
    return persisted_ids

def run_agentic_pass(
    connection_id: str,
    *,
    broker: str = "",
    owner_id: str = "",
    engine: Optional[AgenticDecisionEngine] = None,
    adapter: Optional[BrokerNativeMarketDataAdapter] = None,
    dispatcher: Optional[NativeDispatcher] = None,
    since: Optional[float] = None,
    event_log: Optional[Any] = None,
    override: Optional[OperatorOverride] = None,
) -> AgenticRuntimeResult:
    """Run one full agentic pass: real data -> brain -> gated dispatch.

    ``override`` is an explicit, logged, scoped operator override that
    allows dispatch past the ``DEMO_NOT_EFFECTIVE_ON`` and
    ``LIVE_NOT_AUTHORIZED`` policy gates. It does NOT bypass:

    * Market data errors (data correctness — non-negotiable)
    * Exposure certification inside ``NativeDispatcher`` (20% ceiling,
      0.15%/trade, R:R >= 1.50 — real risk invariants)

    Every bypass is logged with the operator identity, the gate that
    was bypassed, and the current risk state.
    """
    log = event_log or engine_db.event_log

    # --- Load account and determine gating --------------------------
    account = _load_account(connection_id)
    if account is None:
        return AgenticRuntimeResult(
            connection_id=connection_id, environment="unknown",
            effective_on=False, decision_mode="ACCOUNT_NOT_FOUND",
            error="account not found for connection_id")

    is_demo = _is_demo(account)
    environment = account.get("environment", "unknown")
    effective_on = bool(account.get("effective_on"))

    # Resolve broker/owner from account if not explicitly provided.
    if not broker:
        broker = account.get("broker", "")
    if not owner_id:
        owner_id = account.get("owner_id", "")

    # --- Build the adapter and engine --------------------------------
    if adapter is None:
        adapter = BrokerNativeMarketDataAdapter(
            connection_id=connection_id,
            broker=broker,
            login=int(account.get("mt5_login", 0)),
            server=account.get("server", ""),
        )
    if engine is None:
        engine = AgenticDecisionEngine(live=False)

    # --- Run the real-data shadow decision pass ---------------------
    try:
        shadow = run_shadow_decision_pass(
            engine, adapter,
            connection_id=connection_id,
            broker=broker,
            owner_id=owner_id,
            account=account,
            equity=float(account.get("equity", 0.0) or 0.0),
            since=since,
        )
    except MarketDataError as exc:
        log.append(make_event(
            "agentic.market_data_error", connection_id=connection_id,
            broker=broker, execution_state="MARKET_DATA_ERROR",
            reason_code="MARKET_DATA_REJECTED",
            payload={"error": str(exc)}))
        return AgenticRuntimeResult(
            connection_id=connection_id, environment=environment,
            effective_on=effective_on,
            decision_mode="MARKET_DATA_ERROR",
            error=str(exc))

    result = AgenticRuntimeResult(
        connection_id=connection_id,
        environment=environment,
        effective_on=effective_on,
        decision_mode=shadow.get("decision_mode", "UNKNOWN"),
        evaluated_symbols=shadow.get("evaluated_symbols", []),
        candle_count=shadow.get("candle_count", 0),
        quote_count=shadow.get("quote_count", 0),
        candidates_generated=shadow.get("candidates_generated", 0),
        candidates=shadow.get("candidates", []),
        broker_native_signal_certified=shadow.get(
            "broker_native_signal_certified", False),
        shadow_decision_evaluated=shadow.get(
            "shadow_decision_evaluated", False),
        source_hash=shadow.get("source_hash", ""),
        source_ts=shadow.get("source_ts", 0.0),
    )

    # --- Persist candidates into the dispatch queue ----------------
    # Connect the decision engine to the dispatcher: persist brain-
    # generated candidates as RESERVED rows so NativeDispatcher can
    # pick them up. This closes the gap where decisions were generated
    # but never reached the dispatch queue.
    raw_candidates = shadow.get("_raw_candidates", [])
    if raw_candidates:
        persisted_ids = _persist_candidates(
            raw_candidates, connection_id=connection_id, event_log=log)
        result.persisted_candidate_ids = persisted_ids
        # candidates_generated reflects the brain's output; the persisted
        # list shows what reached the dispatch queue.

    # --- Gated dispatch ------------------------------------------
    # DEMO: dispatch only if effective_on AND exposure certified.
    # LIVE: never dispatch unless effective_on AND distinct live intent.
    # An operator override can bypass the policy gates (effective_on,
    # live authorization) but NOT market data errors or exposure
    # certification. Every bypass is logged.
    candidates = shadow.get("candidates", [])

    if not candidates:
        # No approved candidate -- HOLD/reject. Nothing to dispatch.
        log.append(make_event(
            "agentic.hold_no_dispatch", connection_id=connection_id,
            broker=broker, execution_state="HOLD",
            reason_code="NO_APPROVED_CANDIDATE"))
        return result

    # Resolve the override (load from config if not passed directly).
    if override is None:
        override = load_override()

    def _log_bypass(gate: str, ov: OperatorOverride) -> None:
        log.append(make_event(
            "agentic.override_bypass", connection_id=connection_id,
            broker=broker, execution_state="DISPATCH_OVERRIDDEN",
            reason_code=gate,
            payload={
                "authorized_by": ov.authorized_by,
                "authorized_at": ov.authorized_at,
                "reason": ov.reason,
                "gate_bypassed": gate,
                "risk_state": {
                    "environment": environment,
                    "effective_on": effective_on,
                    "candidates_generated": result.candidates_generated,
                },
            }))

    if is_demo:
        if not effective_on and not (override and override.covers(connection_id)):
            result.dispatch_blocked = True
            result.dispatch_reason = "DEMO_NOT_EFFECTIVE_ON"
            log.append(make_event(
                "agentic.dispatch_blocked", connection_id=connection_id,
                broker=broker, execution_state="BLOCKED",
                reason_code="DEMO_NOT_EFFECTIVE_ON"))
            return result
        if not effective_on and override and override.covers(connection_id):
            _log_bypass("DEMO_NOT_EFFECTIVE_ON", override)
        # DEMO dispatch is gated by the existing exposure certification
        # inside NativeDispatcher.dispatch_account(). No additional gate
        # is needed -- the dispatcher blocks on BLOCKED_INCOMPLETE_EXPOSURE.
    elif not _is_live_authorized(connection_id, account):
        if override and override.covers(connection_id):
            _log_bypass("LIVE_NOT_AUTHORIZED", override)
        else:
            result.dispatch_blocked = True
            result.dispatch_reason = "LIVE_NOT_AUTHORIZED"
            log.append(make_event(
                "agentic.dispatch_blocked", connection_id=connection_id,
                broker=broker, execution_state="BLOCKED",
                reason_code="LIVE_NOT_AUTHORIZED"))
            return result

    # --- Dispatch via the existing NativeDispatcher -----------------
    if dispatcher is None:
        dispatcher = NativeDispatcher(
            event_log=log,
            gateway=SingleWriterGateway(event_log=log),
            receipt_mgr=ReceiptManager(event_log=log),
        )

    dispatch_result = dispatcher.dispatch_account(connection_id)
    result.dispatch_results = dispatch_result.get("details", [])
    if dispatch_result.get("error"):
        result.dispatch_blocked = True
        result.dispatch_reason = dispatch_result["error"]

    log.append(make_event(
        "agentic.dispatch_complete", connection_id=connection_id,
        broker=broker, execution_state="DISPATCHED",
        payload={
            "sent": dispatch_result.get("sent", 0),
            "ack": dispatch_result.get("ack", 0),
            "unknown": dispatch_result.get("unknown", 0),
            "rejected": dispatch_result.get("rejected", 0),
            "error": dispatch_result.get("error", ""),
        }))

    return result
