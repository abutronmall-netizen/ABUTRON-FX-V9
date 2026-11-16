"""Backtest + forward-test engine.

Drives the full AI pipeline — NexusBrain decision -> sniper entry ->
trailing stop -> brain lot sizing — over historical market data, simulating
execution and computing P&L and performance metrics. The brain is the sole
calculation authority; the engine only simulates the fills the EA would
make.

- **Backtest**: walk the whole series, execute every signal, report metrics.
- **Forward test**: split into train (first 1-f) / test (last f). The brain
  learns from train-window trades, then only trades executed in the test
  window are reported — a realistic walk-forward deployment simulation.

Entry fill logic mirrors the sniper plan:
- LIMIT BUY fills at the trigger when the candle's low reaches it.
- LIMIT SELL fills at the trigger when the candle's high reaches it.
- CONFIRM fills at the candle close (market).
SL / TP / trailing are checked on subsequent candles (conservative: a
candle that hits both SL and TP is treated as SL-first).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

from ..brain import NexusBrain
from ..brain.trainer import prepare_samples, walk_forward_train
from ..brain.signals import (build_signal_bundle, latest_signal_features,
                             online_learn_from_bundle)
from ..brain.risk import RiskPolicy
from ..config import GEMMA_ENABLED, RUNTIME_RULES_PATH
from ..apex.eligibility import SymbolEligibility
from ..apex.sniper import SniperEntryEngine, sniper_engine
from ..apex.trailing import TrailingStop, trailing_stop
from ..apex.runtime_rules import runtime_rule_for, trail_k_for
from ..apex.indicators import atr
from ..feeds.feed_quality import Candle, Quote


@dataclass
class Position:
    symbol: str
    timeframe: str
    direction: str          # BUY | SELL
    entry_price: float
    lots: float
    sl: float               # initial invalidation
    tp: float
    trail_k: float
    trail_atr: float
    trail_stop: float       # current trail stop (tightens in favour)
    entry_type: str
    entry_quality: float
    open_time: str
    highest_high: float
    lowest_low: float
    regime: str = ""
    point: float = 0.0
    pnl_usd: float = 0.0
    closed: bool = False
    close_price: float = 0.0
    close_reason: str = ""
    # feature vector captured at decision time — fed back for online
    # neural learning when the position settles (true self-learning).
    features: list = field(default_factory=list)


@dataclass
class BacktestResult:
    equity_start: float
    equity_end: float
    total_pnl: float
    return_pct: float
    trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    avg_trade_pnl: float
    max_drawdown: float
    max_consecutive_losses: int
    max_open_drawdown: float
    trades_reported: int
    symbol: str = ""
    timeframe: str = ""
    htf: str = ""
    confluence_filtered: int = 0
    entry_filtered: int = 0
    equity_filtered: int = 0
    daily_stopped: int = 0
    days_traded: int = 0
    mode: str = "backtest"
    details: List[Dict] = field(default_factory=list)
    loss_guard_blocked: int = 0
    signal_sources: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {k: v for k, v in self.__dict__.items()}


class BacktestEngine:
    def __init__(self, brain: NexusBrain, *,
                 sniper: Optional[SniperEntryEngine] = None,
                 trailing: Optional[TrailingStop] = None,
                 equity: float = 10000.0,
                 risk_pct: float = 0.05,
                 point: float = 0.0,
                 point_value: float = 1.0,
                 lot_step: float = 0.01,
                 lot_min: float = 0.01,
                 lot_max: float = 50.0,
                 commission_per_lot: float = 0.0,
                 history_len: int = 120,
                 warmup: int = 30,
                 compound: bool = False,
                 mtf_confluence: bool = True,
                 htf_warmup: int = 20,
                 # High-precision entry filters (win-rate optimisation):
                 min_conviction: float = 0.0,
                 require_trend_regime: bool = False,
                 min_atr_pct: float = 0.0,
                 max_atr_pct: float = 9.99,
                 require_momentum: bool = False,
                 risk_policy: Optional[RiskPolicy] = None,
                 eligibility: Optional[SymbolEligibility] = None,
                 trail_k: Optional[float] = None,
                 runtime_rules_path: Optional[str] = None,
                 use_gemma_signals: bool = False,
                 persist_brain_changes: bool = False):
        self.brain = brain
        self.sniper = sniper or sniper_engine
        self.trailing = trailing or trailing_stop
        self.equity = equity
        self.risk_pct = risk_pct
        self.point = point
        self.point_value = point_value
        self.lot_step = lot_step
        self.lot_min = lot_min
        self.lot_max = lot_max
        self.commission_per_lot = commission_per_lot
        self.history_len = history_len
        self.warmup = warmup
        self.compound = compound
        # Multi-timeframe confluence: only enter when the higher-timeframe
        # direction agrees with the lower-timeframe trigger (wait for TFs
        # to agree before trading).
        self.mtf_confluence = mtf_confluence
        self.htf_warmup = htf_warmup
        self.min_conviction = min_conviction
        self.require_trend_regime = require_trend_regime
        self.min_atr_pct = min_atr_pct
        self.max_atr_pct = max_atr_pct
        self.require_momentum = require_momentum
        # Equity-tiered compounding + daily target/loss-cap policy. When
        # set, sizing uses the tier risk_pct/lot_max for the live equity
        # and entries stop for the day on the profit-target / loss-cap.
        self.risk_policy = risk_policy or RiskPolicy()
        # Equity-aware symbol eligibility: the backtest trades only the
        # symbols the equity can carry, exactly like the live engine, so
        # backtest/forward-test and live select trades consistently.
        self.eligibility = eligibility or SymbolEligibility()
        self.train_epochs = 60
        self.train_batch = 32
        self.train_seed = 0
        self.trail_k = trail_k
        self.runtime_rules_path = runtime_rules_path or RUNTIME_RULES_PATH
        self.use_gemma_signals = bool(use_gemma_signals or GEMMA_ENABLED)
        # Backtests learn per bar on an isolated copy by default, so repeated
        # evaluation runs never mutate the shared trained brain. Training
        # scripts opt in to persistence when they intend to refine it.
        self.persist_brain_changes = bool(persist_brain_changes)
        self._signal_bundle = None
        self._signal_index: Dict[str, int] = {}
        self._train_report = None
        self._train_error = ""

    # ------------------------------------------------------------------
    def _point(self, c: "Candle") -> float:
        """Point size for a candle: prefer the instrument's auto-detected
        point, fall back to the engine default (never 0)."""
        p = c.point or self.point
        return p if p > 0 else 1e-9

    def _point_for(self, pos: "Position") -> float:
        p = pos.point or self.point
        return p if p > 0 else 1e-9

    def _entry_allowed(self, prop, closes, highs, lows, a, idx,
                       symbol: str = "") -> tuple:
        """Return (allowed, reason), including optimized runtime rules."""
        from ..apex.geometry import SymbolGeometry, apply_entry_filters
        geom = SymbolGeometry(
            tp_atr_mult=0.0,
            invalidation_atr_frac=0.0,
            require_trend=self.require_trend_regime,
            require_momentum=self.require_momentum,
            min_conviction=self.min_conviction,
            min_atr_pct=self.min_atr_pct,
            max_atr_pct=self.max_atr_pct)
        rule = runtime_rule_for(symbol, self.runtime_rules_path) if symbol else {}
        if rule:
            geom = replace(
                geom,
                require_trend=geom.require_trend or bool(rule.get("require_trend", False)),
                require_momentum=geom.require_momentum or bool(rule.get("require_momentum", False)),
                min_conviction=max(geom.min_conviction,
                                   float(rule.get("min_conviction", 0.0) or 0.0)),
                min_atr_pct=max(geom.min_atr_pct,
                                float(rule.get("min_atr_pct", 0.0) or 0.0)),
                max_atr_pct=(float(rule.get("max_atr_pct", geom.max_atr_pct))
                             if rule.get("max_atr_pct") else geom.max_atr_pct))
        return apply_entry_filters(prop, closes, highs, lows, a, geom, idx)

    @staticmethod
    def _to_epoch(iso: str) -> float:
        import time as _time
        try:
            return _time.mktime(_time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _trade_detail(pos: "Position", pnl: float, c: "Candle",
                      reason: str) -> Dict[str, Any]:
        """Export one paper trade with the exact brain features at entry."""
        return {
            "source": "paper_backtest",
            "symbol": pos.symbol, "timeframe": pos.timeframe,
            "direction": pos.direction, "entry_type": pos.entry_type,
            "entry_quality": round(float(pos.entry_quality), 6),
            "regime": pos.regime,
            "entry": float(pos.entry_price),
            "exit": float(pos.close_price or getattr(c, "close", 0.0)),
            "pnl": round(float(pnl), 6), "reason": reason,
            "opened_at": pos.open_time, "closed_at": c.open_time,
            "lots": float(pos.lots), "point": float(pos.point),
            "features": [float(v) for v in pos.features],
        }

    @staticmethod
    def _directional_return(pos: "Position", exit_price: float) -> float:
        """Positive when the market moved UP (the brain's class-1 label)."""
        return (float(exit_price) - float(pos.entry_price)) \
            if pos.direction == "BUY" else \
            (float(pos.entry_price) - float(exit_price))

    def _train_on(self, train_candles: List[Candle]) -> None:
        """Train the brain's ensemble on a window of candles (walk-forward)."""
        try:
            closes = [c.close for c in train_candles]
            highs = [c.high for c in train_candles]
            lows = [c.low for c in train_candles]
            bundle = build_signal_bundle(
                train_candles,
                symbol=train_candles[0].symbol if train_candles else "",
                use_gemma=self.use_gemma_signals)
            samples = prepare_samples(
                closes, highs, lows, horizon=1,
                market_features=bundle.market_features)
            if len(samples) < 50:
                return
            report = walk_forward_train(
                self.brain.ensemble, samples,
                epochs=self.train_epochs, batch_size=self.train_batch,
                val_fraction=0.2, seed=self.train_seed)
            # surface accuracy on the train window
            self._train_report = report
        except Exception as exc:  # training must never break the run
            self._train_report = None
            self._train_error = str(exc)

    def run(self, candles: List[Candle], quotes: Optional[List[Quote]] = None,
            *, symbol: str = "", timeframe: str = "",
            htf_candles: Optional[List[Candle]] = None,
            forward_test: bool = False, forward_frac: float = 0.2
            ) -> BacktestResult:
        """Run an isolated evaluation/training pass.

        Per-bar online learning still happens on every closed candle, but on
        a deep copy of the brain unless ``persist_brain_changes`` is set.
        This makes backtests reproducible and prevents one optimizer combo
        from contaminating the next.
        """
        original_brain = self.brain
        original_sniper = self.sniper
        original_trail_k = self.trail_k
        if not self.persist_brain_changes:
            self.brain = copy.deepcopy(original_brain)
            self.sniper = copy.deepcopy(original_sniper)
        try:
            return self._run_impl(
                candles, quotes, symbol=symbol, timeframe=timeframe,
                htf_candles=htf_candles, forward_test=forward_test,
                forward_frac=forward_frac)
        finally:
            if not self.persist_brain_changes:
                self.brain = original_brain
                self.sniper = original_sniper
                self.trail_k = original_trail_k

    def _run_impl(self, candles: List[Candle],
                  quotes: Optional[List[Quote]] = None,
                  *, symbol: str = "", timeframe: str = "",
                  htf_candles: Optional[List[Candle]] = None,
                  forward_test: bool = False, forward_frac: float = 0.2
                  ) -> BacktestResult:
        if not candles:
            return BacktestResult(self.equity, self.equity, 0.0, 0.0, 0, 0,
                                  0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0,
                                  0, symbol=symbol, timeframe=timeframe,
                                  mode="empty")
        candles = sorted(candles, key=lambda c: c.open_time)
        htf_candles = sorted(htf_candles, key=lambda c: c.open_time) \
            if htf_candles else []
        run_symbol = symbol or (candles[0].symbol if candles else "")
        # One causal signal bundle for the full series. Row i uses only bars
        # 0..i, so using the full array here never leaks future information.
        self._signal_bundle = build_signal_bundle(
            candles, symbol=run_symbol, use_gemma=self.use_gemma_signals)
        self._signal_index = {t: i for i, t in
                              enumerate(self._signal_bundle.bar_times)}
        # Apply optimized sniper/trailing rules when a rules file is present.
        rule = runtime_rule_for(run_symbol, self.runtime_rules_path)
        if rule:
            self.sniper = SniperEntryEngine(
                tp_atr_mult=float(rule.get("tp_atr_mult",
                                           self.sniper.tp_atr_mult)),
                invalidation_atr_frac=float(
                    rule.get("invalidation_atr_frac",
                             self.sniper.invalidation_atr_frac)),
                pullback_atr_max=float(rule.get("pullback_atr_max",
                                                self.sniper.pullback_atr_max)))
            if self.trail_k is None:
                self.trail_k = trail_k_for(run_symbol,
                                           path=self.runtime_rules_path)
        quotes_by_sym = {}
        if quotes:
            for q in quotes:
                if symbol and q.symbol != symbol:
                    continue
                quotes_by_sym.setdefault(q.symbol, []).append(q)

        # forward-test: train the brain on the first (1-f) window, then
        # evaluate on the held-out last f window (clean walk-forward).
        report_from = 0
        mode = "backtest"
        run_candles = candles
        if forward_test and forward_frac > 0:
            split = max(1, int(len(candles) * (1.0 - forward_frac)))
            train_candles = candles[:split]
            run_candles = candles[split:]
            self._train_on(train_candles)
            report_from = 0
            mode = "forward_test"

        equity = self.equity
        max_equity = equity
        max_dd = 0.0
        max_consec_losses = 0
        consec_losses = 0
        wins = losses = 0
        total_win = total_loss = 0.0
        reported_trades = 0
        reported_wins = reported_losses = 0
        reported_pnl = 0.0
        details: List[Dict] = []
        # daily profit-stop / loss-cap tracking (equity-tiered policy)
        current_day = None
        daily_pnl = 0.0
        daily_stopped = 0
        days_traded = 0

        history: List[Candle] = []
        position: Optional[Position] = None
        idx = 0
        last_a: Optional[float] = None
        # multi-timeframe confluence state
        htf_hist: List[Candle] = []
        htf_ptr: int = 0
        htf_dir: str = ""
        confluence_filtered = 0
        entry_filtered = 0
        equity_filtered = 0
        loss_guard_blocked = 0

        for c in run_candles:
            idx += 1
            history.append(c)
            # roll the daily PnL over to a new trading day
            day_key = (c.open_time or "")[:10]
            if day_key and day_key != current_day:
                current_day = day_key
                daily_pnl = 0.0
                days_traded += 1
            if len(history) > self.history_len:
                history = history[-self.history_len:]

            closes = [x.close for x in history]
            highs = [x.high for x in history]
            lows = [x.low for x in history]
            _a = atr(highs, lows, closes, 14)
            if _a is None:
                _a = last_a
            else:
                last_a = _a
            a = _a if _a else (self._point(c) * 10)

            # --- continuous per-bar online neural learning --------------
            # Learn from the move that just completed: the features as of
            # the PREVIOUS bar predict this bar's move. Causal (no
            # look-ahead) and runs on every closed bar, so the brain
            # self-trains continuously, not just on settled trades.
            s_idx = self._signal_index.get(c.open_time)
            if len(closes) >= 30 and s_idx is not None and s_idx > 0:
                _prev_close = closes[-2]
                online_learn_from_bundle(
                    self.brain, symbol=c.symbol,
                    bundle=self._signal_bundle, index=s_idx - 1,
                    realised_return=closes[-1] - _prev_close)

            # --- advance the higher-timeframe series to bars closed before
            #     this LTF bar opens, and derive the HTF direction.
            c_open_epoch = self._to_epoch(c.open_time)
            while htf_ptr < len(htf_candles):
                hb = htf_candles[htf_ptr]
                if self._to_epoch(hb.close_time) <= c_open_epoch:
                    htf_hist.append(hb)
                    if len(htf_hist) > self.history_len:
                        htf_hist = htf_hist[-self.history_len:]
                    htf_ptr += 1
                else:
                    break
            if len(htf_hist) >= self.htf_warmup:
                _hcloses = [x.close for x in htf_hist]
                _hhighs = [x.high for x in htf_hist]
                _hlows = [x.low for x in htf_hist]
                _hsig = latest_signal_features(
                    htf_hist, symbol=c.symbol,
                    use_gemma=self.use_gemma_signals)
                _hprop = self.brain.decide(
                    symbol=c.symbol, closes=_hcloses, highs=_hhighs,
                    lows=_hlows, orderflow=_hsig.get("orderflow", {}),
                    footprint=_hsig.get("footprint", {}),
                    agent_scores=_hsig.get("agent_scores", {}),
                    regime=_hsig.get("regime", ""))
                htf_dir = _hprop.direction
            else:
                htf_dir = ""

            # --- manage an open position against this candle -------------
            if position is not None:
                position.highest_high = max(position.highest_high, c.high)
                position.lowest_low = min(position.lowest_low, c.low)
                # update trail (Chandelier) from the position's own history
                trail = self.trailing.plan(
                    direction=position.direction,
                    highs=[position.highest_high],
                    lows=[position.lowest_low],
                    atr_val=a, entry_price=position.entry_price,
                    trail_k=position.trail_k)
                # tighten SL to the trail only when it improves it
                if position.direction == "BUY":
                    if trail.trail_stop > position.sl:
                        position.sl = trail.trail_stop
                else:
                    if trail.trail_stop < position.sl:
                        position.sl = trail.trail_stop

                closed = self._check_exit(position, c)
                if closed is not None:
                    pnl = self._settle(position, closed[0], closed[1], equity)
                    equity += pnl
                    daily_pnl += pnl
                    self.brain.learn_from_trade(
                        symbol=position.symbol, regime=position.regime,
                        direction=position.direction,
                        entry_type=position.entry_type,
                        entry_quality=position.entry_quality, pnl=pnl,
                        features=position.features)
                    # True online neural learning: nudge the brain's own
                    # weights toward the realised move using the features
                    # captured at entry (self-learning from each trade).
                    if position.features:
                        self.brain.online_learn_features(
                            position.features,
                            self._directional_return(position, closed[0]),
                            symbol=position.symbol)
                    if pnl > 0:
                        wins += 1; total_win += pnl; consec_losses = 0
                    else:
                        losses += 1; total_loss += -pnl
                        consec_losses += 1
                        max_consec_losses = max(max_consec_losses,
                                                consec_losses)
                    if idx > report_from:
                        reported_trades += 1
                        reported_pnl += pnl
                        if pnl > 0:
                            reported_wins += 1
                        else:
                            reported_losses += 1
                    if closed[2]:
                        details.append(self._trade_detail(
                            position, pnl, c, closed[1]))
                    position = None

            max_equity = max(max_equity, equity)
            max_dd = max(max_dd, (max_equity - equity) / max_equity)

            # --- new entry on this closed candle -------------------------
            # require a warmup of history (for ATR + features) before the
            # brain/sniper are trusted to generate entries
            warmed = idx > self.warmup and last_a is not None
            if warmed and ((position is None and idx > report_from) or
                           (position is None and not forward_test)):
                # Equity-aware eligibility: trade only the symbols the
                # equity can carry (min-lot risk <= per-trade risk budget).
                # Uses the actual point-in-time ATR, so it matches the live
                # engine and size_position's MIN_LOT_RISK_EXCEEDS_CAP guard.
                _atr_pts = (a / self._point(c)) if a else None
                _elig = self.eligibility.is_tradable(
                    c.symbol, equity, atr_points=_atr_pts,
                    risk_pct=self.risk_policy.risk_pct_for(equity),
                    point=self._point(c))
                if not _elig.eligible:
                    equity_filtered += 1
                else:
                    sig = (self._signal_bundle.at(s_idx)
                           if s_idx is not None else
                           latest_signal_features(
                               history, symbol=c.symbol,
                               use_gemma=self.use_gemma_signals))
                    prop = self.brain.decide(
                        symbol=c.symbol, closes=closes, highs=highs, lows=lows,
                        orderflow=sig.get("orderflow", {}),
                        footprint=sig.get("footprint", {}),
                        agent_scores=sig.get("agent_scores", {}),
                        regime=sig.get("regime", ""))
                    if prop.direction not in ("BUY", "SELL"):
                        if any("LOSS_PATTERN_REPEAT" in b
                               for b in (prop.blockers or [])):
                            loss_guard_blocked += 1
                    else:
                        # Daily profit-stop / loss-cap: once the daily target is
                        # reached or the loss cap hit, stop trading for the day.
                        # Never raises risk to chase the target (tier risk is fixed).
                        _dstop, _dreason = self.risk_policy.allow_entry(equity, daily_pnl)
                        if not _dstop:
                            daily_stopped += 1
                        else:
                            # High-precision entry filter (win-rate optimisation):
                            # drop low-probability setups before the confluence gate.
                            _ok, _reason = self._entry_allowed(
                                prop, closes, highs, lows, a, idx,
                                symbol=c.symbol)
                            if not _ok:
                                entry_filtered += 1
                            elif self.mtf_confluence and htf_candles and (
                                    not htf_dir or htf_dir != prop.direction):
                                # Multi-timeframe confluence: wait until the HTF
                                # agrees with the LTF trigger. Only applies when a
                                # higher-timeframe series was supplied; otherwise the
                                # platform trades every trigger as before.
                                confluence_filtered += 1
                            else:
                                bias = self.brain.get_entry_bias(c.symbol, "")
                                snip = self.sniper.plan(
                                    symbol=c.symbol, direction=prop.direction,
                                    closes=closes, highs=highs, lows=lows,
                                    atr_val=a,
                                    of_features=sig.get("orderflow", {}),
                                    fp_features=sig.get("footprint", {}),
                                    regime=prop.regime,
                                    uncertainty=prop.uncertainty,
                                    conviction=prop.conviction,
                                    confirm_bias=bias.get("confirm_bias", 0.0),
                                    quality_floor=bias.get("quality_floor", 0.0))
                                fill = self._entry_fill(snip, c, prop.direction)
                                if fill is not None:
                                    sizing_equity = equity if self.compound \
                                        else self.equity
                                    position = self._open_position(
                                        c, snip, fill, prop, a, sizing_equity,
                                        max_equity)

        # --- close any position left open at end of data -------------------
        if position is not None:
            last = candles[-1]
            pnl = self._settle(position, last.close, "END_OF_DATA", equity)
            equity += pnl
            daily_pnl += pnl
            self.brain.learn_from_trade(
                symbol=position.symbol, regime=position.regime,
                direction=position.direction,
                entry_type=position.entry_type,
                entry_quality=position.entry_quality, pnl=pnl,
                features=position.features)
            if position.features:
                self.brain.online_learn_features(
                    position.features,
                    self._directional_return(position, last.close),
                    symbol=position.symbol)
            details.append(self._trade_detail(
                position, pnl, last, "END_OF_DATA"))
            if last and idx > report_from:
                reported_trades += 1
                reported_pnl += pnl

        trades = wins + losses
        avg_win = (total_win / wins) if wins else 0.0
        avg_loss = (total_loss / losses) if losses else 0.0
        pf = (total_win / total_loss) if total_loss > 0 else (
            float("inf") if total_win > 0 else 0.0)
        avg_trade = (reported_pnl / reported_trades) if reported_trades else 0.0

        return BacktestResult(
            equity_start=self.equity, equity_end=equity,
            total_pnl=equity - self.equity,
            return_pct=((equity - self.equity) / self.equity * 100.0)
            if self.equity else 0.0,
            trades=trades, wins=wins, losses=losses,
            win_rate=(wins / trades * 100.0) if trades else 0.0,
            profit_factor=pf, avg_win=avg_win, avg_loss=avg_loss,
            avg_trade_pnl=avg_trade, max_drawdown=max_dd * 100.0,
            max_consecutive_losses=max_consec_losses,
            max_open_drawdown=0.0, trades_reported=reported_trades,
            symbol=symbol or (candles[0].symbol if candles else ""),
            timeframe=timeframe or (candles[0].timeframe if candles else ""),
            htf=(htf_candles[0].timeframe if htf_candles else ""),
            confluence_filtered=confluence_filtered,
            entry_filtered=entry_filtered,
            equity_filtered=equity_filtered,
            daily_stopped=daily_stopped,
            days_traded=days_traded,
            mode=mode, details=details,
            loss_guard_blocked=loss_guard_blocked,
            signal_sources=self._signal_bundle.sources)

    # ------------------------------------------------------------------
    def _entry_fill(self, snip, c: Candle, direction: str):
        """Determine the fill price for a sniper entry on candle c.

        LIMIT fills at the trigger when price reaches it; CONFIRM fills at
        the close. Returns (fill_price, filled) or None if not filled.
        """
        if snip.entry_type == "LIMIT":
            return self._limit_fill(snip, c, direction)
        return c.close, True

    def _limit_fill(self, snip, c: Candle, direction: str):
        # BUY limit fills when low <= trigger; SELL when high >= trigger.
        is_buy = direction == "BUY"
        if is_buy:
            if c.low <= snip.trigger_price:
                return snip.trigger_price, True
        else:
            if c.high >= snip.trigger_price:
                return snip.trigger_price, True
        return None

    def _open_position(self, c: Candle, snip, fill, prop, a, equity,
                       max_equity) -> Optional[Position]:
        is_buy = prop.direction == "BUY"
        stop_dist_points = abs(snip.trigger_price - snip.invalidation) / self._point(c)
        # Extreme-low-drawdown sizing: DD-capped per-trade risk with the
        # runtime drawdown taper (risk scaled, never hard-stopped).
        risk_pct = self.risk_policy.risk_pct_for_runtime(equity, max_equity)
        sizing = self.brain.size_position(
            equity=equity, risk_pct=risk_pct,
            stop_distance_points=stop_dist_points,
            point_value=self.point_value, lot_step=self.lot_step,
            lot_min=self.lot_min, lot_max=self.risk_policy.lot_max_for(equity),
            entry_quality=snip.entry_quality)
        # If sizing refused the trade (e.g. MIN_LOT_RISK_EXCEEDS_CAP when
        # the account is too small for the instrument's minimum lot), skip
        # it — do NOT force the minimum lot and over-risk (that compounds
        # into a >100% drawdown on tiny equity).
        if sizing.get("lots", 0.0) <= 0.0:
            return None
        lots = sizing["lots"]
        trail_k = (self.trail_k if self.trail_k is not None
                   else trail_k_for(c.symbol, path=self.runtime_rules_path,
                                    default=TrailingStop.DEFAULT_K))
        trail = self.trailing.plan(
            direction=prop.direction, highs=[c.high], lows=[c.low],
            atr_val=a, entry_price=fill[0], trail_k=trail_k,
            symbol=c.symbol)
        return Position(
            symbol=c.symbol, timeframe=c.timeframe,
            direction=prop.direction, entry_price=fill[0], lots=lots,
            sl=snip.invalidation, tp=snip.take_profit,
            trail_k=trail.trail_k, trail_atr=trail.trail_atr,
            trail_stop=snip.invalidation,  # starts at invalidation
            entry_type=snip.entry_type, entry_quality=snip.entry_quality,
            regime=prop.regime, open_time=c.open_time,
            highest_high=c.high, lowest_low=c.low,
            point=self._point(c),
            features=list(prop.features))

    def _check_exit(self, pos: Position, c: Candle):
        """Return (exit_price, reason, should_record) or None."""
        if pos.direction == "BUY":
            if c.low <= pos.sl:
                return pos.sl, "SL", True
            if c.high >= pos.tp:
                return pos.tp, "TP", True
        else:
            if c.high >= pos.sl:
                return pos.sl, "SL", True
            if c.low <= pos.tp:
                return pos.tp, "TP", True
        return None

    def _settle(self, pos: Position, exit_price: float, reason: str,
                equity: float) -> float:
        """Compute realised PnL for a closed position (USD)."""
        move_points = (exit_price - pos.entry_price) / self._point_for(pos)
        if pos.direction == "SELL":
            move_points = -move_points
        pnl = pos.lots * move_points * self.point_value
        pnl -= pos.lots * self.commission_per_lot  # round-turn commission
        pos.pnl_usd = pnl
        pos.close_price = exit_price
        pos.close_reason = reason
        pos.closed = True
        return pnl
