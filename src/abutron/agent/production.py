"""Agentic production decision engine — replaces the apex engine.

This is the authoritative production decision path. It implements the
same ``run_decision_pass`` contract the platform's downstream consumers
(dispatcher, receipts, decision journal) already expect, so it is a
drop-in replacement for ``abutron.apex.engine.ApexEngine``.

Instead of the apex 6-step chain, every symbol runs the full agentic
pipeline:

    sense (brain signal) -> think (reasoning) -> gate (RiskGate)
    -> act (ExecutionAgent) -> monitor (ledger) -> recover

The brains (``NexusBrain`` / ``UnifiedBrain``) are the signal authority
via ``BrainSubstrate``; the V9 risk invariants are enforced by the
unbypassable ``RiskGate``; execution routes through the V9 native
dispatcher via ``ExecutionAgent``. The apex engine is retained as a
legacy rollback reference but is no longer the active decision authority.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config import GEMMA_ENABLED
from ..feeds.feed_quality import Candle, Quote
from .interfaces import AgentContext, Direction, Verdict
from .orchestrator import AgentOrchestrator
from .reasoning import LLMReasoningBackend, ReasoningBackend, RuleReasoningBackend
from .risk_gate import RiskGate
from .substrate import BrainSubstrate
from .memory import DecisionLedger
from . import build_agent_system

# The Candidate contract is shared with execution/receipts/journal. We
# import it for shape compatibility so downstream consumers work unchanged
# during the apex -> agentic migration.
from ..apex.engine import Candidate


class AgenticDecisionEngine:
    """Production decision engine built on the agentic multi-agent system.

    Mirrors ``ApexEngine.run_decision_pass`` so it is a drop-in
    replacement. Per closed candle it runs the agentic cycle and converts
    the resulting decision into a ``Candidate`` for downstream execution.
    """

    def __init__(
        self,
        orchestrator: Optional[AgentOrchestrator] = None,
        brain_substrate: Optional[BrainSubstrate] = None,
        risk_gate: Optional[RiskGate] = None,
        reasoning: Optional[ReasoningBackend] = None,
        live: bool = False,
        dispatcher: Any = None,
    ) -> None:
        self.orchestrator = orchestrator or build_agent_system(
            live=live,
            brain_substrate=brain_substrate,
            risk_gate=risk_gate,
            reasoning=reasoning,
        )
        # Expose the pipeline pieces for diagnostics / tests.
        self.substrate = self.orchestrator.signal_agent.substrate
        self.gate = self.orchestrator.risk_agent.gate
        self.ledger = self.orchestrator.ledger
        # Trailing bar history for the brain's feature builder (warm-up).
        # Mirrors the apex engine's ``_history_store``: keyed by
        # (symbol, timeframe) -> list of closed candles (most-recent-last).
        # In production this is populated from the native history export.
        self._history_store: Dict[tuple, List[Candle]] = {}

    # ------------------------------------------------------------------ #
    def set_history(self, symbol: str, timeframe: str,
                    candles: List[Candle]) -> None:
        """Register trailing bar history for the brain's warm-up."""
        self._history_store[(symbol, timeframe)] = list(candles)

    def _history(self, candle: Candle) -> List[Candle]:
        """Return the trailing history for indicator/brain warm-up."""
        return self._history_store.get(
            (candle.symbol, candle.timeframe), [candle])

    # ------------------------------------------------------------------ #
    def run_decision_pass(
        self,
        *,
        connection_id: str,
        broker: str,
        owner_id: str,
        candles: List[Candle],
        quotes: List[Quote],
        account: Dict[str, Any],
        orderflow_trades: Optional[Dict[str, list]] = None,
        tick_size: float = 0.0001,
        equity: Optional[float] = None,
        balance: Optional[float] = None,
    ) -> List[Candidate]:
        """Evaluate all eligible symbols and produce reserved candidates.

        Same contract as ``ApexEngine.run_decision_pass``. Each closed
        candle runs the agentic cycle; only decisions that pass the
        RiskGate become candidates. Fail-closed: any stage failure or gate
        rejection yields no candidate for that symbol.
        """
        quotes_by_sym = {q.symbol: q for q in quotes}
        equity = equity if equity is not None else float(
            account.get("equity", account.get("balance", 0.0)) or 0.0)

        candidates: List[Candidate] = []
        seen_symbols: set = set()
        for c in candles:
            if c.symbol in seen_symbols or not c.closed:
                continue
            seen_symbols.add(c.symbol)
            candidate = self._evaluate_symbol(
                c, quotes_by_sym.get(c.symbol), connection_id, broker,
                owner_id, account, equity, orderflow_trades or {})
            if candidate is not None:
                candidates.append(candidate)

        # Rank by conviction, never alphabetical symbol order (matches
        # the apex contract downstream consumers rely on).
        candidates.sort(key=lambda cd: cd.conviction, reverse=True)
        return candidates

    # ------------------------------------------------------------------ #
    def _evaluate_symbol(
        self,
        c: Candle,
        quote: Optional[Quote],
        connection_id: str,
        broker: str,
        owner_id: str,
        account: Dict[str, Any],
        equity: float,
        orderflow_trades: Dict[str, list],
    ) -> Optional[Candidate]:
        # Build the agentic context from the closed candle + quote.
        orderflow = self._orderflow_summary(c, orderflow_trades)
        portfolio = {
            "equity": equity,
            "connection_id": connection_id,
            "owner_id": owner_id,
            "broker": broker,
        }
        # Build the OHLC series for the brain's feature builder. The
        # unified brain needs a series (not a single bar) to compute
        # returns/ATR and build the full feature vector.
        hist = self._history(c)
        closes = [float(x.close) for x in hist if x.closed]
        highs = [float(x.high) for x in hist if x.closed]
        lows = [float(x.low) for x in hist if x.closed]
        context = AgentContext(
            symbol=c.symbol,
            timeframe=c.timeframe,
            bar_open=float(c.open or 0.0),
            bar_close=float(c.close or 0.0),
            high=float(c.high or 0.0),
            low=float(c.low or 0.0),
            spread=float(getattr(quote, "spread", 0.0) or 0.0)
            if quote else 0.0,
            orderflow=orderflow,
            footprint={},
            portfolio=portfolio,
            extras={"account": account, "quote": _quote_dict(quote)},
            closes=closes, highs=highs, lows=lows,
        )

        decision = self.orchestrator.decide(context)

        # Fail-closed: only a gate-approved, non-hold decision with a
        # real order action becomes a candidate.
        if decision.verdict not in (Verdict.APPROVE, Verdict.SHADOW):
            return None
        if decision.action is None or decision.action.kind != "submit_order":
            return None
        if decision.action.direction is Direction.HOLD:
            return None

        return self._to_candidate(decision, c, connection_id, broker,
                                  owner_id, equity)

    # ------------------------------------------------------------------ #
    def _to_candidate(self, decision, c: Candle, connection_id: str,
                      broker: str, owner_id: str, equity: float) -> Candidate:
        """Convert an agentic decision into the shared ``Candidate``."""
        action = decision.action
        signal = decision.signal
        direction = action.direction.value
        # replay_key: stable per symbol+bar+direction (at-most-once).
        replay_key = f"{c.symbol}:{c.open_time}:{direction}"
        # risk_fraction from conviction-scaled position size.
        risk_fraction = float(signal.position_size) if signal else 0.0
        stop_risk_usd = float(action.payload.get("stop_risk_usd", 0.0))
        if stop_risk_usd <= 0 and action.stop_loss > 0:
            stop_risk_usd = abs(action.price - action.stop_loss) * action.quantity
        return Candidate(
            candidate_id=decision.decision_id,
            connection_id=connection_id,
            owner_id=owner_id,
            broker=broker,
            symbol=c.symbol,
            timeframe=c.timeframe,
            bar_open=str(c.open_time),
            bar_close=str(c.close),
            replay_key=replay_key,
            direction=direction,
            conviction=float(signal.conviction) if signal else 0.0,
            regime=signal.regime if signal else "",
            agent_votes={
                "signal_source": signal.brain_source if signal else "",
                "reasoning_backend": decision.reasoning.get("backend", ""),
                "intent": decision.reasoning.get("signal", ""),
                "confidence": decision.reasoning.get("confidence", 0.0),
                "narrative": decision.reasoning.get("narrative", ""),
                "gate_reason": decision.gate_reason,
                "trace": decision.agent_trace,
            },
            risk_fraction=risk_fraction,
            stop_risk_usd=stop_risk_usd,
            volume=float(action.quantity),
            stop_loss=float(action.stop_loss),
            take_profit=float(action.take_profit),
            price=float(action.price),
            reason_code=decision.gate_reason,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _orderflow_summary(c: Candle,
                           orderflow_trades: Dict[str, list]) -> Dict[str, Any]:
        """Compact order-flow summary from trade ticks (if available)."""
        ticks = orderflow_trades.get(c.symbol)
        if not ticks:
            return {}
        buys = sum(1 for t in ticks if _side(t) == "BUY")
        total = len(ticks)
        if total == 0:
            return {}
        imbalance = (buys / total) - 0.5
        return {
            "delta": float(buys - (total - buys)),
            "imbalance": round(imbalance * 2.0, 4),
            "buy_ratio": round(buys / total, 4),
        }


def _side(tick: Any) -> str:
    if isinstance(tick, dict):
        side = str(tick.get("side", tick.get("action", ""))).upper()
        return "BUY" if side in ("BUY", "B") else "SELL"
    return "BUY"


def _quote_dict(quote: Optional[Quote]) -> Dict[str, Any]:
    if quote is None:
        return {}
    return {
        "bid": float(getattr(quote, "bid", 0.0) or 0.0),
        "ask": float(getattr(quote, "ask", 0.0) or 0.0),
        "spread": float(getattr(quote, "spread", 0.0) or 0.0),
    }
