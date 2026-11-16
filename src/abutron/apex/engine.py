"""APEX decision engine — closed-candle decision and order chain.

Implements the blueprint's 6-step chain:
  1. Capture authenticated, account-bound OHLC + quotes; M5 candle closed
  2. Regime + 3 core signal agents; combine aligned evidence
  3. Rank by conviction (not symbol order)
  4. Validate CRM ON, licensing, owner binding, signal identity, clock,
     spread/cost, native positions, 20% owner ceiling
  5. Reserve unique candidate/candle (at-most-once)
  6. Hand to the native single-writer dispatcher

The engine is signal-only: it never claims broker credentials, EA status
or order ACK. Execution is the native writer's job.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import (DEFAULT_PER_TRADE_RISK_PCT, DEFAULT_TRAILING_ATR_MULT,
                      ENGINE_VERSION, PRIMARY_TIMEFRAME, utc_now)
from ..crypto import make_activation_revision
from ..events import EventLog, make_event
from ..feeds.feed_quality import Candle, Quote
from ..analytics.orderflow import orderflow_engine
from ..analytics.footprint import build_footprint
from .agents.regime import RegimeAgent
from .agents.trend import TrendAgent
from .agents.meanreversion import MeanReversionAgent
from .agents.breakout import BreakoutAgent
from .agents.portfolio import PortfolioAgent
from .agents.risk import RiskAgent
from .gemma import gemma_advisor
from .debate import debate_advisor
from .journal import DecisionJournal
from .sniper import sniper_engine
from .trailing import trailing_stop

# NexusBrain — the advanced, uncertainty-aware decision brain (signal
# authority). The legacy agents below are retained as feature contributors
# and diagnostics, not as the decision authority.
from ..brain import NexusBrain, make_brain, default_ensemble


def _json_default(obj):
    """Serialise numpy scalars / arrays for JSON persistence."""
    item = getattr(obj, "item", None)
    if callable(item):
        return item()
    if hasattr(obj, "tolist"):
        return obj.tolist()
    return str(obj)


@dataclass
class Candidate:
    candidate_id: str
    connection_id: str
    owner_id: str
    broker: str
    symbol: str
    timeframe: str
    bar_open: str
    bar_close: str
    replay_key: str
    direction: str
    conviction: float
    regime: str
    agent_votes: Dict[str, Any]
    risk_fraction: float
    stop_risk_usd: float
    volume: float
    stop_loss: float
    take_profit: float
    price: float
    entry_type: str = ""          # "LIMIT" | "CONFIRM" (sniper entry)
    trigger_price: float = 0.0   # precise sniper entry level
    invalidation: float = 0.0    # tight sniper invalidation (tiny loss)
    entry_quality: float = 0.0   # sniper entry precision 0..1
    sniper_plan: Dict[str, Any] = field(default_factory=dict)
    trail_k: float = 0.0          # trailing-stop ATR multiple
    trail_stop: float = 0.0       # current trailing stop price
    trail_plan: Dict[str, Any] = field(default_factory=dict)
    gemma: Optional[Dict[str, Any]] = None
    reason_code: str = ""
    reserved: bool = False

    def to_row(self) -> Dict[str, Any]:
        import json as _json
        return {
            "candidate_id": self.candidate_id,
            "connection_id": self.connection_id,
            "owner_id": self.owner_id,
            "broker": self.broker,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bar_open": self.bar_open,
            "replay_key": self.replay_key,
            "direction": self.direction,
            "conviction": float(self.conviction),
            "regime": self.regime,
            "agent_votes": _json.dumps(self.agent_votes, default=_json_default),
            "risk_fraction": float(self.risk_fraction),
            "stop_risk_usd": float(self.stop_risk_usd),
            "volume": float(self.volume),
            "stop_loss": float(self.stop_loss),
            "take_profit": float(self.take_profit),
            "price": float(self.price),
            "entry_type": self.entry_type,
            "trigger_price": float(self.trigger_price),
            "invalidation": float(self.invalidation),
            "entry_quality": float(self.entry_quality),
            "sniper_plan": _json.dumps(self.sniper_plan, default=_json_default),
            "trail_k": float(self.trail_k),
            "trail_stop": float(self.trail_stop),
            "trail_plan": _json.dumps(self.trail_plan, default=_json_default),
            "execution_state": "RESERVED" if self.reserved else "PENDING",
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }


class ApexEngine:
    def __init__(self, event_log: EventLog, crm_service=None,
                 brain: "NexusBrain | None" = None):
        self.log = event_log
        self.journal = DecisionJournal(event_log)
        self.crm = crm_service
        # The NexusBrain is the signal authority. Legacy agents are retained
        # as feature contributors + diagnostics.
        from ..config import (BRAIN_MIN_CONFIDENCE, BRAIN_MAX_UNCERTAINTY,
                              BRAIN_MAX_CONVICTION, BRAIN_MC_SAMPLES,
                              BRAIN_ENSEMBLE_SEED, BRAIN_SIZE_CAP)
        if brain is None:
            brain = make_brain(
                ensemble=default_ensemble(seed=BRAIN_ENSEMBLE_SEED),
                min_confidence=BRAIN_MIN_CONFIDENCE,
                max_uncertainty=BRAIN_MAX_UNCERTAINTY,
                max_conviction=BRAIN_MAX_CONVICTION,
                size_cap=BRAIN_SIZE_CAP)
            brain.ensemble.mc_samples = BRAIN_MC_SAMPLES
        self.brain = brain
        self.regime = RegimeAgent()
        self.trend = TrendAgent()
        self.mean_rev = MeanReversionAgent()
        self.breakout = BreakoutAgent()
        self.portfolio = PortfolioAgent()
        self.risk = RiskAgent()

    # ------------------------------------------------------------------
    # Main decision pass
    # ------------------------------------------------------------------
    def run_decision_pass(self, *, connection_id: str, broker: str,
                          owner_id: str, candles: List[Candle],
                          quotes: List[Quote], account: Dict[str, Any],
                          orderflow_trades: Optional[Dict[str, list]] = None,
                          tick_size: float = 0.0001,
                          equity: Optional[float] = None,
                          balance: Optional[float] = None) -> List[Candidate]:
        """Evaluate all eligible symbols and produce reserved candidates."""
        from ..db import engine_db

        candidates: List[Candidate] = []
        quotes_by_sym = {q.symbol: q for q in quotes}
        equity = equity if equity is not None else float(
            account.get("equity", account.get("balance", 0.0)) or 0.0)
        balance = balance if balance is not None else float(
            account.get("balance", 0.0) or 0.0)

        # The legacy/internal APEX engine is not the native-order authority,
        # but must still honour account controls if called by a portal or
        # research worker. Never reserve a candidate for an OFF account.
        if not (account.get("desired_on") and account.get("effective_on")):
            return []

        owner_book = self._owner_book(owner_id, connection_id)

        seen_symbols = set()
        for c in candles:
            if c.symbol in seen_symbols or not c.closed:
                continue
            seen_symbols.add(c.symbol)
            result = self._evaluate_symbol(
                c, quotes_by_sym.get(c.symbol), connection_id, broker,
                owner_id, account, equity, owner_book,
                orderflow_trades or {}, tick_size)
            if result is not None:
                candidates.append(result)

        # Rank by conviction (never alphabetical symbol order)
        ranked_cands = sorted(candidates,
                              key=lambda x: x.conviction, reverse=True)

        reserved: List[Candidate] = []
        for cand in ranked_cands:
            if self._reserve(cand, engine_db):
                reserved.append(cand)
        return reserved

    def _evaluate_symbol(self, c: Candle, quote: Optional[Quote],
                         connection_id: str, broker: str, owner_id: str,
                         account: Dict[str, Any], equity: float,
                         owner_book: Dict[str, Any],
                         orderflow_trades: Dict[str, list],
                         tick_size: float) -> Optional[Candidate]:
        closes = [x.close for x in self._history(c)]
        highs = [x.high for x in self._history(c)]
        lows = [x.low for x in self._history(c)]

        # --- Legacy agents: retained as feature contributors + diagnostics.
        # They are NOT the decision authority anymore; the NexusBrain is.
        regime_op = self.regime.vote({"symbol": c.symbol, "high": highs,
                                      "low": lows, "close": closes})
        context_base = {
            "symbol": c.symbol, "regime": regime_op.regime,
            "high": highs, "low": lows, "close": closes,
        }
        trend_op = self.trend.vote(context_base)
        meanrev_op = self.mean_rev.vote(context_base)
        breakout_op = self.breakout.vote(context_base)
        portfolio_op = self.portfolio.vote(
            {**context_base, "opinions": [trend_op, meanrev_op, breakout_op]})

        # Signed agent scores (BUY=+, SELL=-, HOLD=0) fed to the brain as
        # features and kept for diagnostics.
        def _signed(op) -> float:
            if op.direction == "BUY":
                return op.conviction
            if op.direction == "SELL":
                return -op.conviction
            return 0.0

        agent_scores = {
            "trend": _signed(trend_op), "meanrev": _signed(meanrev_op),
            "breakout": _signed(breakout_op), "carry": 0.0,
        }

        # Order flow + footprint overlay (evidence for the brain)
        of_snap = None
        if orderflow_trades.get(c.symbol):
            of_snap = orderflow_engine.aggregate(
                c.symbol, c.open_time, c.close_time,
                orderflow_trades[c.symbol])
        fp = None
        if of_snap and of_snap.trades:
            fp = build_footprint(c.symbol, of_snap.trades,
                                 tick_size or quote.tick_size)

        # --- The NexusBrain is the signal authority. ---
        proposal = self.brain.decide(
            symbol=c.symbol, closes=closes, highs=highs, lows=lows,
            orderflow=of_snap.features if of_snap else {},
            footprint=fp.features if fp else {},
            agent_scores=agent_scores, regime=regime_op.regime)

        # Multi-agent debate advisory (advisory only, never on the order path)
        advisory_ctx = {
            "symbol": c.symbol, "regime": proposal.regime,
            "opinions": [o.to_dict() for o in
                         (trend_op, meanrev_op, breakout_op)],
            "orderflow": of_snap.features if of_snap else {},
            "footprint": fp.features if fp else {},
        }
        if debate_advisor.is_configured():
            gemma = debate_advisor.advise(advisory_ctx).to_dict()
        else:
            gemma = gemma_advisor.advise(advisory_ctx).to_dict()

        # Aggressive mode: the brain always commits to a direction, so
        # will_trade is always True here. No abstention gate.

        # --- Sniper entry: precise, low-drawdown entry geometry ---------
        # Entry precision is what controls drawdown. Compute a trigger-based
        # entry plan (pullback-to-level or confirmation) with a tight
        # invalidation instead of a naive market-at-close entry.
        from .indicators import atr
        a = atr(highs, lows, closes, 14) or 0.0
        direction = proposal.direction
        # Brain's learned entry bias: after losses the brain prefers
        # CONFIRM entries (never enter too early) and floors entry quality.
        bias = self.brain.get_entry_bias(c.symbol, proposal.regime)
        sniper = sniper_engine.plan(
            symbol=c.symbol, direction=direction,
            closes=closes, highs=highs, lows=lows, atr_val=a,
            of_features=of_snap.features if of_snap else {},
            fp_features=fp.features if fp else {},
            regime=proposal.regime, uncertainty=proposal.uncertainty,
            conviction=proposal.conviction,
            confirm_bias=bias.get("confirm_bias", 0.0),
            quality_floor=bias.get("quality_floor", 0.0))

        # Entry price = the sniper trigger (best-price fill). Stops use the
        # tight invalidation (tiny wrong-entry loss) and the tight TP.
        price = sniper.trigger_price
        sl = sniper.invalidation
        tp = sniper.take_profit

        # Trailing stop leg — the brain computes the Chandelier trail; the
        # EA applies it and moves the broker stop in favour.
        trail = trailing_stop.plan(
            direction=direction, highs=highs, lows=lows, atr_val=a,
            entry_price=price)

        risk_ctx = {
            "candidate": {"direction": direction, "volume": 0.0,
                          "stop_loss": sl, "take_profit": tp,
                          "stop_risk_usd": 0.0},
            "quote": quote, "account": account, "owner_book": owner_book,
            "price": price, "atr": a, "regime": proposal.regime,
        }
        risk_dec = self.risk.evaluate(risk_ctx)
        # Aggressive mode: the risk check is ADVISORY — recorded for
        # transparency but NEVER blocks a trade. (Fail-closed risk gating
        # removed per the new design directive.)
        if not risk_dec.allow:
            self.journal.record(connection_id=connection_id, broker=broker,
                               owner_id=owner_id, symbol=c.symbol,
                               timeframe=c.timeframe, bar_open=c.open_time,
                               regime=proposal.regime,
                               opinions=[o.to_dict() for o in
                                         (regime_op, trend_op, meanrev_op,
                                          breakout_op, portfolio_op)],
                               decision=direction, gemma=gemma,
                               reason=f"RISK_ADVISORY_{risk_dec.reason}",
                               detail=risk_dec.detail,
                               brain=proposal.to_dict())
        # (trade proceeds — risk is advisory, not a gate)

        # The AI brain performs ALL lot-size calculation from client equity
        # + per-trade risk + the sniper stop distance. The EA only executes
        # the computed lot. Sniper entry quality scales the risk (cleaner
        # entry -> larger lot).
        if quote is not None:
            point = float(quote.point or quote.tick_size or 0.0)
            stop_distance_points = abs(sniper.trigger_price -
                                       sniper.invalidation) / point \
                if point > 0 else 0.0
            sizing = self.brain.size_position(
                equity=float(account.get("equity",
                                         account.get("balance", 0.0)) or 0.0),
                risk_pct=float(account.get("per_trade_risk_pct",
                                           DEFAULT_PER_TRADE_RISK_PCT)),
                stop_distance_points=stop_distance_points,
                point_value=float(quote.point_value or 0.0),
                lot_step=float(quote.volume_min or 0.01),
                lot_min=float(quote.volume_min or 0.01),
                lot_max=float(quote.volume_max or 50.0),
                entry_quality=sniper.entry_quality)
        else:
            sizing = {"lots": 0.0, "risk_usd": 0.0, "reason": "NO_QUOTE"}
        volume = sizing.get("lots", 0.0)
        stop_risk_usd = sizing.get("risk_usd", 0.0)

        cand = Candidate(
            candidate_id=f"cand_{uuid.uuid4().hex[:12]}",
            connection_id=connection_id, owner_id=owner_id, broker=broker,
            symbol=c.symbol, timeframe=c.timeframe,
            bar_open=c.open_time, bar_close=c.close_time,
            replay_key=c.replay_key(connection_id, direction),
            direction=direction, conviction=proposal.conviction,
            regime=proposal.regime,
            agent_votes={**{o.agent: o.to_dict() for o in
                            (trend_op, meanrev_op, breakout_op, portfolio_op)},
                         "nexus_brain": proposal.to_dict()},
            risk_fraction=float(account.get("per_trade_risk_pct",
                                            DEFAULT_PER_TRADE_RISK_PCT)),
            stop_risk_usd=stop_risk_usd,
            volume=volume,
            stop_loss=sl, take_profit=tp, price=price,
            entry_type=sniper.entry_type,
            trigger_price=sniper.trigger_price,
            invalidation=sniper.invalidation,
            entry_quality=sniper.entry_quality,
            sniper_plan=sniper.to_dict(),
            trail_k=trail.trail_k,
            trail_stop=trail.trail_stop,
            trail_plan=trail.to_dict(),
            gemma=gemma,
            reason_code=risk_dec.reason,
        )
        self.journal.record(connection_id=connection_id, broker=broker,
                           owner_id=owner_id, symbol=c.symbol,
                           timeframe=c.timeframe, bar_open=c.open_time,
                           regime=proposal.regime,
                           opinions=[o.to_dict() for o in
                                     (regime_op, trend_op, meanrev_op,
                                      breakout_op, portfolio_op)],
                           decision=direction, gemma=gemma,
                           reason=risk_dec.reason,
                           candidate=cand,
                           risk=risk_dec.detail,
                           brain=proposal.to_dict())
        return cand

    def _history(self, candle: Candle) -> List[Candle]:
        """Return the trailing 100-bar history for indicator warm-up.

        In production this comes from the native history export keyed by
        (connection, symbol, timeframe). Here we accept the slice passed
        in via the engine state store.
        """
        return self._history_store.get(
            (candle.symbol, candle.timeframe), [candle])

    _history_store: Dict[tuple, List[Candle]] = {}

    def set_history(self, symbol: str, timeframe: str,
                    candles: List[Candle]) -> None:
        self._history_store[(symbol, timeframe)] = candles

    def _owner_book(self, owner_id: str, connection_id: str) -> Dict[str, Any]:
        from ..db import engine_db
        aggregate = 0.0
        balances = {}
        risk_by_conn: Dict[str, float] = {}
        for acct in engine_db.list_accounts(owner_id=owner_id):
            cid = acct["connection_id"]
            for pos in engine_db.recent_open_positions(cid):
                risk_by_conn[cid] = risk_by_conn.get(cid, 0.0) + \
                    float(pos.get("volume", 0.0) or 0.0) * 10.0  # proxy
            balances[cid] = float(acct.get("equity", 0.0) or 0.0)
            if cid == connection_id:
                aggregate = risk_by_conn.get(cid, 0.0)
        return {
            "aggregate_stop_risk_usd": aggregate,
            "balance_by_conn": balances,
        }

    def _reserve(self, cand: Candidate, engine_db) -> bool:
        """At-most-once reservation on replay_key."""
        if engine_db.add_candidate(cand.to_row()):
            cand.reserved = True
            self.log.append(make_event(
                "candidate.reserved",
                connection_id=cand.connection_id, broker=cand.broker,
                mt5_login=0, owner_id=cand.owner_id,
                symbol=cand.symbol, timeframe=cand.timeframe,
                native_bar_open=cand.bar_open, closed_at=cand.bar_close,
                replay_key=cand.replay_key, apex_version=ENGINE_VERSION,
                regime=cand.regime, conviction=cand.conviction,
                risk_fraction=cand.risk_fraction,
                stop_risk_usd=cand.stop_risk_usd, volume=cand.volume,
                execution_state="RESERVED"))
            return True
        self.log.append(make_event(
            "candidate.duplicate",
            connection_id=cand.connection_id, broker=cand.broker,
            symbol=cand.symbol, replay_key=cand.replay_key,
            execution_state="DUPLICATE",
            reason_code="CANDLE_ALREADY_CONSUMED"))
        return False

    def _journal_reject_all(self, connection_id, broker, owner_id,
                            candles, reason, equity) -> None:
        for c in candles:
            self.journal.record(connection_id=connection_id, broker=broker,
                               owner_id=owner_id, symbol=c.symbol,
                               timeframe=c.timeframe, bar_open=c.open_time,
                               decision="REJECTED", reason=reason)
