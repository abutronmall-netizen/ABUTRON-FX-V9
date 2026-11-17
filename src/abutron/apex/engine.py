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
                      DEPTH_SNAPSHOTS_PATH, ENGINE_VERSION, GEMMA_ENABLED,
                      PRIMARY_TIMEFRAME, RUNTIME_RULES_PATH,
                      TICK_TRADES_PATH, utc_now)
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
from .eligibility import SymbolEligibility, load_symbol_brain, default_brain_root
from .ranker import DailyProfitRanker

# NexusBrain — the advanced, uncertainty-aware decision brain (signal
# authority). The legacy agents below are retained as feature contributors
# and diagnostics, not as the decision authority.
from ..brain import NexusBrain, make_brain, default_ensemble
from ..brain.risk import RiskPolicy
from ..brain.unified import load_unified, save_unified, unified_brain_dir
from ..brain.trainer import save_brain
from ..brain.signals import (build_signal_bundle, load_depth_signals,
                             load_tick_trades, online_learn_from_bundle)


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
                 brain: "NexusBrain | None" = None,
                 risk_policy: Optional[RiskPolicy] = None,
                 eligibility: Optional["SymbolEligibility"] = None,
                 brain_root: Optional[str] = None,
                 ranker: Optional["DailyProfitRanker"] = None,
                 max_symbols_per_day: Optional[int] = 5,
                 online_save_interval: int = 50):
        self.log = event_log
        # Equity-tiered compounding + daily target/loss-cap policy. Drives
        # tier-based risk_pct + lot_max and the daily profit-stop / loss-cap.
        self.risk_policy = risk_policy or RiskPolicy()
        # Equity-aware symbol eligibility: trade only the symbols the
        # client's equity can carry (min-lot risk <= per-trade risk budget).
        # Built-in default; an explicit instance may be injected.
        self.eligibility = eligibility or SymbolEligibility()
        self.journal = DecisionJournal(event_log)
        self.crm = crm_service
        # The NexusBrain is the signal authority. Legacy agents are retained
        # as feature contributors + diagnostics.
        from ..config import (BRAIN_MIN_CONFIDENCE, BRAIN_MAX_UNCERTAINTY,
                              BRAIN_MAX_CONVICTION, BRAIN_MC_SAMPLES,
                              BRAIN_ENSEMBLE_SEED, BRAIN_SIZE_CAP)
        self._brain_cfg = {
            "min_confidence": BRAIN_MIN_CONFIDENCE,
            "max_uncertainty": BRAIN_MAX_UNCERTAINTY,
            "max_conviction": BRAIN_MAX_CONVICTION,
            "size_cap": BRAIN_SIZE_CAP,
        }
        self._brain_mc_samples = BRAIN_MC_SAMPLES
        self._brain_seed = BRAIN_ENSEMBLE_SEED
        # Trained-brain root for per-symbol brains (data/brains/<slug>_m5).
        self._brain_root = brain_root or default_brain_root()
        # Per-symbol trained-brain cache. The brain used for a symbol is the
        # one trained + validated on that symbol (loaded from data/brains);
        # unknown symbols fall back to the default ensemble brain.
        self._brain_cache: Dict[str, NexusBrain] = {}
        # --- Unified brain: all per-symbol brains working as ONE brain. ---
        # If a unified (symbol-conditioned) brain is trained, it is the
        # signal authority for EVERY symbol; per-symbol specialists are the
        # fallback when no unified brain is present.
        self._unified_brain = load_unified(
            unified_brain_dir(self._brain_root))
        # --- Daily profit ranker: trade the most profitable eligible symbols. ---
        self.ranker = (ranker if ranker is not None
                       else DailyProfitRanker(self.eligibility))
        # Cap on simultaneous symbols traded per day (most-profitable first).
        # None = trade every profitable eligible symbol.
        self.max_symbols_per_day = max_symbols_per_day
        # Throttle for persisting online-learned brain weights.
        self.online_save_interval = online_save_interval
        # Optional true tick/depth evidence; otherwise causal OHLCV proxies.
        self._signal_tick_trades = (load_tick_trades(TICK_TRADES_PATH)
                                    if TICK_TRADES_PATH else {})
        self._signal_depth = (load_depth_signals(DEPTH_SNAPSHOTS_PATH)
                              if DEPTH_SNAPSHOTS_PATH else {})
        self._runtime_rules_path = RUNTIME_RULES_PATH
        # High-water mark for the extreme-low-drawdown runtime risk taper.
        self._high_water: float = 0.0
        if brain is None:
            brain = make_brain(
                ensemble=default_ensemble(seed=BRAIN_ENSEMBLE_SEED),
                **self._brain_cfg)
            brain.ensemble.mc_samples = BRAIN_MC_SAMPLES
        self.brain = brain
        self.regime = RegimeAgent()
        self.trend = TrendAgent()
        self.mean_rev = MeanReversionAgent()
        self.breakout = BreakoutAgent()
        self.portfolio = PortfolioAgent()
        self.risk = RiskAgent()

    # ------------------------------------------------------------------
    # Per-symbol trained brain
    # ------------------------------------------------------------------
    def _brain_for(self, symbol: str) -> NexusBrain:
        """Return the signal brain for ``symbol``.

        When a **unified** brain is trained, it is the single brain for
        EVERY symbol ("all brains work as one brain") — the symbol-
        conditioned ensemble handles all instruments. Otherwise the
        per-symbol trained brain is loaded from ``data/brains/<slug>_m5``
        and cached; unknown symbols fall back to the default ensemble.
        """
        if self._unified_brain is not None:
            return self._unified_brain
        if symbol in self._brain_cache:
            return self._brain_cache[symbol]
        brain = load_symbol_brain(
            symbol, brain_root=self._brain_root,
            mc_samples=self._brain_mc_samples, brain_kwargs=self._brain_cfg)
        if brain is None:
            brain = self.brain
        self._brain_cache[symbol] = brain
        return brain

    def _persist_brain_online(self, brain, symbol: str = "") -> None:
        """Save a brain's online-learned weights to disk (best-effort)."""
        try:
            if brain is self._unified_brain:
                save_unified(unified_brain_dir(self._brain_root), brain)
            elif symbol and getattr(brain, "ensemble", None) is not None:
                from .eligibility import brain_dir_for
                save_brain(brain_dir_for(symbol, self._brain_root),
                           brain.ensemble)
        except Exception:
            pass

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

        # Track the high-water mark for the extreme-low-drawdown risk taper.
        if equity > self._high_water:
            self._high_water = equity

        # Aggressive mode: NO effective-ON gate. Trading proceeds on every
        # closed candle regardless of the CRM switch state. (Fail-closed
        # EFFECTIVE_OFF gating has been removed per the new design directive.)

        owner_book = self._owner_book(owner_id, connection_id)

        # --- Daily profit selection: trade the most profitable eligible
        # symbols. The ranker returns eligible + profitable symbols ranked
        # by expected daily profit; we trade the top-N (most profit first),
        # which also caps simultaneous exposure for low drawdown. ---
        trade_set = None
        if self.ranker is not None:
            avail = list(dict.fromkeys(
                c.symbol for c in candles if c.closed))
            base_risk = self.risk_policy.risk_pct_for(equity)
            trade_set = set(self.ranker.select(
                equity, symbols=avail, risk_pct=base_risk,
                top_n=self.max_symbols_per_day))

        seen_symbols = set()
        for c in candles:
            if c.symbol in seen_symbols or not c.closed:
                continue
            if trade_set is not None and c.symbol not in trade_set:
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
        history = self._history(c)
        closes = [x.close for x in history]
        highs = [x.high for x in history]
        lows = [x.low for x in history]
        signal_bundle = build_signal_bundle(
            history, symbol=c.symbol,
            tick_trades=self._signal_tick_trades,
            depth=self._signal_depth, use_gemma=GEMMA_ENABLED)

        # --- Continuous per-bar online neural learning (true self-learning)
        # Learn from the move that just completed: features as of the
        # PREVIOUS bar predict this bar's move. Causal, runs on every
        # closed candle so the brain self-trains continuously in live
        # trading (not just offline retraining). No-op when online
        # learning is disabled or the brain is untrained.
        if len(closes) >= 30:
            _prev_close = closes[-2]
            _brain = self._brain_for(c.symbol)
            online_learn_from_bundle(
                _brain, symbol=c.symbol, bundle=signal_bundle,
                index=len(signal_bundle.closes) - 2,
                realised_return=closes[-1] - _prev_close)
            # Persist online-learned weights every N updates so the
            # self-training survives restarts (throttled, not per-bar).
            if _brain.online_updates and \
                    _brain.online_updates % self.online_save_interval == 0:
                self._persist_brain_online(_brain, c.symbol)

        # --- Equity-aware symbol eligibility gate ------------------------
        # Look at the client equity and trade ONLY the symbols that cater
        # for it: if the instrument's minimum lot already risks more than
        # the per-trade risk budget, the account is too small for it. Skip
        # before spending brain/sniper compute (size_position is the
        # authoritative final guard with the actual entry quality).
        from .indicators import atr as _atr
        _a = _atr(highs, lows, closes, 14) or 0.0
        _point = float(c.point or (quote.point if quote else 0.0) or 0.0)
        _atr_points = (_a / _point) if _point > 0 else 0.0
        _eq = float(account.get("equity",
                                account.get("balance", 0.0)) or 0.0)
        _risk_pct = (float(account["per_trade_risk_pct"])
                     if "per_trade_risk_pct" in account
                     else self.risk_policy.risk_pct_for(_eq))
        _elig = self.eligibility.is_tradable(
            c.symbol, _eq, atr_points=_atr_points or None,
            risk_pct=_risk_pct,
            point=_point or None,
            point_value=(float(quote.point_value) if quote and quote.point_value
                         else None),
            lot_min=(float(quote.volume_min) if quote and quote.volume_min
                     else None))
        if not _elig.eligible:
            self.journal.record(
                connection_id=connection_id, broker=broker,
                owner_id=owner_id, symbol=c.symbol,
                timeframe=c.timeframe, bar_open=c.open_time,
                regime="",
                opinions=[], decision="REJECTED",
                reason=f"SYMBOL_NOT_ELIGIBLE_EQUITY_{_elig.reason}",
                detail={"equity": _eq, "risk_pct": _risk_pct,
                        "min_equity": round(_elig.min_equity, 4),
                        "risk_per_lot": round(_elig.risk_per_lot, 4),
                        "stop_distance_points": round(
                            _elig.stop_distance_points, 4),
                        "atr_points": round(_elig.atr_points, 4)})
            return None

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

        # --- The NexusBrain is the signal authority. The brain used is the
        # one trained + validated on THIS symbol (loaded from data/brains),
        # falling back to the default ensemble for untrained symbols. ---
        # Causal feature bundle fills all 23 market slots. Real order-flow /
        # footprint snapshots and live agent votes override the proxies when
        # supplied; absent evidence is explicitly labelled as a proxy.
        sig = signal_bundle.at(-1)
        of_features = (dict(of_snap.features) if of_snap else
                       sig.get("orderflow", {}))
        fp_features = (dict(fp.features) if fp else
                       sig.get("footprint", {}))
        fp_features.setdefault("poc_vol_pct",
                               fp_features.get("poc_volume_pct", 0.0))
        scores = dict(sig.get("agent_scores", {}))
        scores.update({k: v for k, v in agent_scores.items() if v})
        decision_brain = self._brain_for(c.symbol)
        proposal = decision_brain.decide(
            symbol=c.symbol, closes=closes, highs=highs, lows=lows,
            orderflow=of_features, footprint=fp_features,
            agent_scores=scores, regime=regime_op.regime)
        # A persisted loss-pattern veto is a HOLD; do not size or open a trade.
        if proposal.direction == "HOLD" and any(
                "LOSS_PATTERN_REPEAT" in b for b in (proposal.blockers or [])):
            self.journal.record(
                connection_id=connection_id, broker=broker,
                owner_id=owner_id, symbol=c.symbol,
                timeframe=c.timeframe, bar_open=c.open_time,
                regime=proposal.regime, opinions=[], decision="HOLD",
                reason="LOSS_PATTERN_REPEAT",
                detail={"blockers": proposal.blockers,
                        "signal_sources": sig.get("sources", {})},
                brain=proposal.to_dict())
            return None

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
        from .geometry import geometry_for, apply_entry_filters
        from .sniper import SniperEntryEngine
        a = _a
        direction = proposal.direction

        # Per-symbol win-rate geometry (TP/SL ATR multiples + entry
        # filters) validated out-of-sample by the optimizer. The live
        # engine trades with the exact geometry the backtest measured.
        geom = geometry_for(c.symbol, rules_path=self._runtime_rules_path)

        # High-precision entry filters (trade selection for win rate, NOT
        # a risk gate). A rejected setup is journaled and skipped; valid
        # setups always proceed. Computed on the point-in-time history.
        filt_ok, filt_reason = apply_entry_filters(
            proposal, closes, highs, lows, a, geom, idx=-1)
        if not filt_ok:
            self.journal.record(
                connection_id=connection_id, broker=broker,
                owner_id=owner_id, symbol=c.symbol,
                timeframe=c.timeframe, bar_open=c.open_time,
                regime=proposal.regime,
                opinions=[o.to_dict() for o in
                          (regime_op, trend_op, meanrev_op, breakout_op,
                           portfolio_op)],
                decision=direction, gemma=gemma,
                reason=f"ENTRY_FILTERED_{filt_reason}",
                detail={"geometry": {
                    "tp_atr": geom.tp_atr_mult,
                    "sl_atr": geom.invalidation_atr_frac,
                    "require_trend": geom.require_trend,
                    "require_momentum": geom.require_momentum}},
                brain=proposal.to_dict())
            return None

        # Brain's learned entry bias: after losses the brain prefers
        # CONFIRM entries (never enter too early) and floors entry quality.
        bias = decision_brain.get_entry_bias(c.symbol, proposal.regime)
        # Per-symbol sniper engine carrying the validated TP/SL geometry.
        sniper_engine_sym = SniperEntryEngine(
            tp_atr_mult=geom.tp_atr_mult,
            invalidation_atr_frac=geom.invalidation_atr_frac,
            pullback_atr_max=geom.pullback_atr_max)
        sniper = sniper_engine_sym.plan(
            symbol=c.symbol, direction=direction,
            closes=closes, highs=highs, lows=lows, atr_val=a,
            of_features=of_features, fp_features=fp_features,
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
            entry_price=price, symbol=c.symbol)

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
            eq = _eq
            # Extreme-low-drawdown sizing: the per-trade risk is capped by
            # the DD target (dd_target / max_consec_losses) and tapered by
            # the runtime drawdown scale. An explicit account override is
            # still capped by the DD target so the drawdown limit always
            # holds. (Risk is scaled, never hard-stopped — no fail-closed.)
            if "per_trade_risk_pct" in account:
                base_rp = min(float(account["per_trade_risk_pct"]),
                              self.risk_policy.dd_risk_pct())
            else:
                base_rp = _risk_pct
            risk_pct = base_rp * self.risk_policy.dd_risk_scale(
                eq, self._high_water)
            lot_max = min(self.risk_policy.lot_max_for(eq),
                          float(quote.volume_max or 50.0))
            sizing = self.brain.size_position(
                equity=eq,
                risk_pct=risk_pct,
                stop_distance_points=stop_distance_points,
                point_value=float(quote.point_value or 0.0),
                lot_step=float(quote.volume_min or 0.01),
                lot_min=float(quote.volume_min or 0.01),
                lot_max=lot_max,
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
                         "nexus_brain": proposal.to_dict(),
                         "signal_sources": sig.get("sources", {})},
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
        """Owner-wide risk exposure using real contract specs.

        Uses RiskEngine.aggregate_owner_risk() which reads actual
        point/point_value from SYMBOL_REGISTRY (with conservative
        fallback for unknown symbols) instead of a fixed * 10.0 proxy.
        """
        from ..db import engine_db
        from ..risk.risk_engine import RiskEngine
        balances = {}
        for acct in engine_db.list_accounts(owner_id=owner_id):
            cid = acct["connection_id"]
            balances[cid] = float(acct.get("equity", 0.0) or 0.0)
        # Use the RiskEngine for correct contract-based exposure calc
        risk_engine = RiskEngine(self.log)
        exposure = risk_engine.aggregate_owner_risk(owner_id)
        return {
            # NULL is not zero: unknown native stop risk must not create
            # artificial headroom for APEX candidate reservations.
            "aggregate_stop_risk_usd": exposure["aggregate_stop_risk_usd"],
            "owner_risk_certified": exposure.get("owner_risk_certified") is True,
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
