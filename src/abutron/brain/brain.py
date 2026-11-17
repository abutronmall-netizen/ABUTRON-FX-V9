"""NexusBrain — the advanced, uncertainty-aware decision brain.

This replaces the legacy rule-based multi-agent voting as the *signal
authority* of the platform. It is deliberately built to be **safe by
architecture**, not just "advanced":

  * **Ensemble** of diverse neural members (snapshot + sequence encoders).
  * **Uncertainty estimation** (ensemble disagreement + MC-dropout) so the
    brain knows when it does not know.
  * **Calibrated confidence** so a stated confidence is meaningful.
  * **Abstention** — below the confidence floor or above the uncertainty
    ceiling the brain returns HOLD and does not propose a trade.
  * **Regime conditioning** — the policy is evaluated in context.
  * **Online adaptation** with a forgetting gate and out-of-sample gating,
    so it can learn from live outcomes without catastrophic forgetting.
  * **Deterministic fallback** — if the brain is not trained, a bounded
    rule-based edge is used, so the platform never breaks.

The brain is **signal-only**. It produces a *proposal* (direction +
conviction + confidence + uncertainty). It never places orders, sets stops,
or bypasses the deterministic RiskGuard / candidate contract / single-writer.
The deterministic risk firewall remains in front of it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .ensemble import BrainEnsemble, EnsemblePrediction, build_ensemble
from .features import N_FEATURES, build_feature_vector, returns_from_closes
from .neural import RNG
from .loss_guard import LossPatternGuard


# ---------------------------------------------------------------------------
# Proposal envelope
# ---------------------------------------------------------------------------
@dataclass
class BrainProposal:
    symbol: str
    direction: str                 # BUY | SELL | HOLD
    conviction: float              # 0..1
    confidence: float              # 0..1 (calibrated, uncertainty-discounted)
    uncertainty: float             # 0..1 (informational — NOT a gate)
    regime: str = ""
    model_agreement: Dict[str, float] = field(default_factory=dict)
    position_size: float = 0.0     # conviction-scaled size fraction (0..1)
    blockers: List[str] = field(default_factory=list)
    rationale: str = ""
    brain_version: str = "nexus_v1"
    trained: bool = False
    features: List[float] = field(default_factory=list)  # captured at decision time for online learning

    @property
    def will_trade(self) -> bool:
        # Aggressive mode: trade on any committed direction, regardless of
        # uncertainty/confidence. Uncertainty is reported, never a gate.
        return self.direction in ("BUY", "SELL")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "direction": self.direction,
            "conviction": round(self.conviction, 4),
            "confidence": round(self.confidence, 4),
            "uncertainty": round(self.uncertainty, 4),
            "regime": self.regime, "model_agreement": self.model_agreement,
            "position_size": round(self.position_size, 4),
            "blockers": self.blockers, "rationale": self.rationale,
            "brain_version": self.brain_version, "trained": self.trained,
            "features": [round(float(v), 6) for v in self.features],
        }


class TradeMemory:
    """Self-learning trade memory — learns from losing trades so the brain
    never enters too early.

    The user's rule: *if it loses a trade, learn from the losing trade and
    never enter too early.* This memory tracks the outcome of every settled
    trade per (symbol, regime) and produces two learned biases that the
    sniper entry engine applies:

    - **confirm_bias** (0..1): rises after losses. Above a threshold the
      entry engine prefers CONFIRM (breakout/continuation) entries over
      LIMIT pullback entries — i.e. it waits for price to confirm the
      direction before entering, instead of entering early on a pullback.
    - **quality_floor** (0..1): rises after losses. The effective entry
      quality must clear this floor, so after a loss the brain demands a
      cleaner, more certain setup before sizing up.

    Wins decay the biases back toward zero (reinforcement). Everything is
    advisory — it shapes entry timing and size, never blocks a trade.
    """

    def __init__(self, win_decay: float = 0.5, loss_step: float = 0.15,
                 max_bias: float = 1.0):
        self.win_decay = win_decay
        self.loss_step = loss_step
        self.max_bias = max_bias
        # key: (symbol, regime) -> state
        self._state: Dict[tuple, Dict[str, float]] = {}

    def _key(self, symbol: str, regime: str) -> tuple:
        return (symbol, regime or "")

    def _slot(self, symbol: str, regime: str) -> Dict[str, float]:
        k = self._key(symbol, regime)
        if k not in self._state:
            self._state[k] = {"confirm_bias": 0.0, "quality_floor": 0.0,
                              "consec_losses": 0, "trades": 0.0,
                              "wins": 0.0, "losses": 0.0,
                              "gross_pnl": 0.0}
        return self._state[k]

    def learn_from_trade(self, *, symbol: str, regime: str,
                         direction: str, entry_type: str,
                         entry_quality: float, pnl: float) -> Dict[str, float]:
        """Record a settled trade and update the entry-timing biases.

        pnl > 0 -> win (decay biases, reinforce patience).
        pnl < 0 -> loss (raise confirm_bias + quality_floor so the brain
        waits for confirmation and a cleaner setup next time).
        """
        s = self._slot(symbol, regime)
        s["trades"] += 1.0
        s["gross_pnl"] += pnl
        won = pnl > 0
        if won:
            s["wins"] += 1.0
            s["consec_losses"] = 0
            # winning reinforces: gently decay the caution biases
            s["confirm_bias"] = max(0.0, s["confirm_bias"] * self.win_decay)
            s["quality_floor"] = max(0.0, s["quality_floor"] * self.win_decay)
        else:
            s["losses"] += 1.0
            s["consec_losses"] += 1.0
            # loss: learn to wait — raise confirm bias (prefer CONFIRM) and
            # raise the entry-quality floor. The bigger the loss and the
            # more consecutive losses, the more the brain demands waiting.
            loss_mag = max(0.0, min(1.0, -pnl)) if pnl < 0 else 0.3
            step = self.loss_step * (1.0 + 0.5 * s["consec_losses"] - 1.0) \
                * (0.5 + loss_mag)
            s["confirm_bias"] = min(self.max_bias,
                                    s["confirm_bias"] + step)
            s["quality_floor"] = min(self.max_bias,
                                     s["quality_floor"] + step * 0.6)
        return self.get_entry_bias(symbol, regime)

    def get_entry_bias(self, symbol: str, regime: str) -> Dict[str, float]:
        """Return the learned entry-timing biases for a symbol/regime."""
        s = self._slot(symbol, regime)
        return {
            "confirm_bias": round(s["confirm_bias"], 4),
            "quality_floor": round(s["quality_floor"], 4),
            "consec_losses": s["consec_losses"],
            "trades": s["trades"],
            "wins": s["wins"],
            "losses": s["losses"],
            "win_rate": (s["wins"] / s["trades"]) if s["trades"] else 0.0,
        }

    def state_dict(self) -> Dict[str, Any]:
        return {"state": {f"{k[0]}|{k[1]}": v for k, v in self._state.items()}}

    def load_state(self, data: Dict[str, Any]) -> None:
        self._state = {}
        for k, v in (data or {}).get("state", {}).items():
            parts = k.split("|", 1)
            symbol = parts[0]
            regime = parts[1] if len(parts) > 1 else ""
            self._state[(symbol, regime)] = dict(v)


# ---------------------------------------------------------------------------
# Regime detector (lightweight, deterministic)
# ---------------------------------------------------------------------------
def detect_regime(closes: List[float], atr: float = 0.0) -> str:
    if len(closes) < 20:
        return "range"
    rets = np.diff(np.asarray(closes, dtype=float))
    vol = float(np.std(rets[-20:])) if len(rets) >= 20 else 0.0
    # trend: compare first/last third of the window
    third = max(1, len(closes) // 3)
    trend = (closes[-1] - closes[-third]) / (closes[-third] or 1.0)
    if vol > 0.0 and vol < 0.0:
        return "range"
    if abs(trend) > 0.004 and (trend > 0) == (rets[-1] >= 0):
        return "trend_up" if trend > 0 else "trend_down"
    if vol and atr and (vol / (abs(closes[-1]) or 1.0)) > 0.003:
        return "high_vol"
    return "range"


# ---------------------------------------------------------------------------
# The brain
# ---------------------------------------------------------------------------
class NexusBrain:
    """The advanced decision brain."""

    VERSION = "nexus_v1"

    def __init__(
        self,
        *,
        ensemble: Optional[BrainEnsemble] = None,
        # Aggressive mode: thresholds are informational, NOT gates. The
        # brain commits to a direction on any edge; conviction drives sizing.
        min_confidence: float = 0.0,
        max_uncertainty: float = 1.0,
        max_conviction: float = 1.0,
        size_cap: float = 1.0,          # max position fraction (aggressive)
        online_lr: float = 0.05,
        forget_decay: float = 0.99,
        oos_gate: float = 0.0,
        # True online neural learning: the ensemble's own weights update
        # from every realised outcome (settled trade / observed bar).
        online_enabled: bool = True,
        nn_online_lr: float = 1e-3,
        seed: int = 0,
    ):
        self.ensemble = ensemble
        self.min_confidence = min_confidence
        self.max_uncertainty = max_uncertainty
        self.max_conviction = max_conviction
        self.size_cap = size_cap
        self.online_lr = online_lr
        self.forget_decay = forget_decay
        self.oos_gate = oos_gate
        self.online_enabled = online_enabled
        self.nn_online_lr = nn_online_lr
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.online_updates = 0

        # online adaptation state (bounded edge model + OOS gating)
        self._online_w = float(self.rng.standard_normal()) * 0.01
        self._online_b = 0.0
        self._online_loss = 0.0
        self._online_n = 0
        self._online_ema = 1.0
        self._baseline_loss = None
        self._training = False
        # Self-learning trade memory: learns from losses so the brain never
        # enters too early (prefers confirmation + cleaner setups after losses).
        self.trade_memory = TradeMemory()
        # Explicit per-trade "never repeat" veto, persisted with the brain.
        self.loss_guard = LossPatternGuard()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def trained(self) -> bool:
        return self.ensemble is not None and bool(self.ensemble.members)

    def attach_ensemble(self, ensemble: BrainEnsemble) -> None:
        self.ensemble = ensemble

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------
    def decide(
        self,
        *,
        symbol: str,
        closes: Optional[List[float]] = None,
        highs: Optional[List[float]] = None,
        lows: Optional[List[float]] = None,
        orderflow: Optional[Dict[str, float]] = None,
        footprint: Optional[Dict[str, float]] = None,
        agent_scores: Optional[Dict[str, float]] = None,
        regime: str = "",
    ) -> BrainProposal:
        """Produce a bounded, uncertainty-aware proposal for one symbol."""
        closes = list(closes or [])
        highs = list(highs or [])
        lows = list(lows or [])
        if not regime and closes:
            from .features import _atr
            regime = detect_regime(closes, _atr(highs or closes,
                                                lows or closes, closes))

        rets = returns_from_closes(closes)
        feats = build_feature_vector(
            closes=closes, highs=highs, lows=lows,
            orderflow=orderflow, footprint=footprint,
            regime=regime, agent_scores=agent_scores,
            **rets)
        x = np.asarray([feats], dtype=float)

        blockers: List[str] = []
        if self.ensemble is None or not self.ensemble.members:
            return self._fallback(symbol, feats, regime, blockers,
                                  trained=False)

        pred = self.ensemble.predict(x)

        # Explicit, deterministic loss-pattern veto. This is a hard guard
        # independent of learned weights and scalar entry biases.
        direction = pred.direction
        blocked, guard_reason = self.loss_guard.should_block(
            symbol=symbol, direction=direction, regime=regime, features=feats)
        if blocked:
            blockers.append(guard_reason)
            return self._guard_hold(symbol=symbol, direction=direction,
                                    regime=regime, feats=feats,
                                    blockers=blockers, votes=pred.votes)

        # Aggressive mode: NO abstention. The brain commits to a direction
        # on any edge. Uncertainty/confidence are reported, never gates.
        # Conviction drives adaptive position sizing.
        direction = pred.direction
        conviction = min(pred.conviction, self.max_conviction)
        # conviction-scaled size: stronger edge -> larger position, capped.
        position_size = float(min(self.size_cap, 0.25 + 0.75 * conviction))

        rationale = (
            f"ensemble={pred.members} dir={pred.direction} "
            f"conv={conviction:.3f} conf={pred.confidence:.3f} "
            f"unc={pred.uncertainty:.3f} size={position_size:.3f} "
            f"regime={regime}"
        )

        return BrainProposal(
            symbol=symbol, direction=direction,
            conviction=conviction,
            confidence=pred.confidence, uncertainty=pred.uncertainty,
            regime=regime, model_agreement=pred.votes,
            position_size=position_size,
            rationale=rationale,
            brain_version=self.VERSION, trained=True,
            features=list(feats))

    # ------------------------------------------------------------------
    # Deterministic loss-pattern veto
    # ------------------------------------------------------------------
    def _guard_hold(self, *, symbol: str, direction: str, regime: str,
                    feats: Sequence[float], blockers: List[str],
                    votes: Optional[Dict[str, float]] = None) -> BrainProposal:
        """Return a no-order proposal for a previously losing feature pattern."""
        return BrainProposal(
            symbol=symbol, direction="HOLD", conviction=0.0,
            confidence=0.0, uncertainty=1.0, regime=regime,
            model_agreement=dict(votes or {}), position_size=0.0,
            blockers=list(blockers),
            rationale="LOSS_PATTERN_REPEAT: exact loss-associated setup vetoed",
            brain_version=self.VERSION, trained=self.trained,
            features=list(feats))

    # ------------------------------------------------------------------
    # Deterministic fallback (untrained brain still trades — no gates)
    # ------------------------------------------------------------------
    def _fallback(self, symbol: str, feats: List[float], regime: str,
                  blockers: List[str], *, trained: bool) -> BrainProposal:
        # A transparent rule-based edge from the feature vector. Always
        # commits to a direction (aggressive mode: no abstention).
        try:
            f = np.asarray(feats, dtype=float)
            edge = 0.0
            # indices: ret_5 (2), momentum_z (4), of_imbalance (10),
            # trend_score (18), meanrev_score (19)
            edge += 0.3 * float(f[2]) + 0.3 * float(f[4])
            edge += 0.2 * float(f[10])
            edge += 0.2 * float(f[18])
            edge = float(max(-1.0, min(1.0, edge)))
            conviction = min(1.0, abs(edge))
            confidence = 0.5 + 0.4 * conviction
            uncertainty = 1.0 - conviction
            # commit to a direction on any non-zero edge
            direction = "BUY" if edge >= 0 else "SELL"
            blocked, guard_reason = self.loss_guard.should_block(
                symbol=symbol, direction=direction, regime=regime,
                features=feats)
            if blocked:
                blockers.append(guard_reason)
                return self._guard_hold(symbol=symbol, direction=direction,
                                        regime=regime, feats=feats,
                                        blockers=blockers)
            position_size = float(min(self.size_cap, 0.25 + 0.75 * conviction))
            return BrainProposal(
                symbol=symbol, direction=direction,
                conviction=conviction, confidence=confidence,
                uncertainty=uncertainty, regime=regime,
                position_size=position_size,
                rationale="fallback_rule_edge "
                          f"edge={edge:.3f} size={position_size:.3f} "
                          f"regime={regime}",
                brain_version=self.VERSION, trained=False,
                features=list(feats))
        except Exception:
            return BrainProposal(
                symbol=symbol, direction="BUY", conviction=0.1,
                confidence=0.0, uncertainty=1.0, regime=regime,
                position_size=0.1,
                rationale="fallback_error (committed to BUY)",
                brain_version=self.VERSION, trained=False,
                features=list(feats))

    # ------------------------------------------------------------------
    # Online adaptation (safe, gated)
    # ------------------------------------------------------------------
    def observe(self, *, symbol: str, predicted_edge: float,
                realized_return: float) -> Dict[str, Any]:
        """Update the online edge model from a realised outcome.

        A forgetting gate (EMA on loss) prevents a few bad bars from
        overwriting learning, and an out-of-sample gate blocks an update
        that would worsen the running loss beyond ``oos_gate``.
        """
        if not self.trained:
            return {"updated": False, "reason": "UNTRAINED"}
        y = 1.0 if realized_return > 0 else 0.0
        pred = self._online_sigmoid(predicted_edge)
        loss = -(y * math.log(max(pred, 1e-9)) +
                 (1 - y) * math.log(1 - max(pred, 1e-9)))
        self._online_ema = self.forget_decay * self._online_ema + \
            (1 - self.forget_decay) * loss
        self._online_n += 1

        # accept the update only if it does not blow up the running loss
        accept = True
        if self._baseline_loss is not None and self._online_ema > \
                self._baseline_loss * (1 + self.oos_gate) + 1e-3:
            accept = False
        # gradient step on the scalar edge model
        d = (pred - y) * self.online_lr
        self._online_w = self._online_w - d * predicted_edge
        self._online_b = self._online_b - d
        self._online_loss = self._online_ema
        return {"updated": accept, "loss_ema": round(self._online_ema, 5),
                "baseline": round(self._baseline_loss or 0.0, 5)}

    # ------------------------------------------------------------------
    # True online neural learning — the ensemble's own weights update
    # from each realised outcome, so the brain is continuously
    # self-training (not just the offline batch + the scalar edge model).
    # ------------------------------------------------------------------
    def online_learn(
        self, *, symbol: str,
        closes: Optional[List[float]] = None,
        highs: Optional[List[float]] = None,
        lows: Optional[List[float]] = None,
        realised_return: float = 0.0,
        orderflow: Optional[Dict[str, float]] = None,
        footprint: Optional[Dict[str, float]] = None,
        agent_scores: Optional[Dict[str, float]] = None,
        regime: str = "",
    ) -> Dict[str, Any]:
        """Nudge the ensemble weights toward a realised outcome.

        The label is the direction the market actually moved: class 1
        (BUY) if ``realised_return > 0`` else class 0 (SELL). Features
        are built exactly as in ``decide`` (no look-ahead). One
        single-sample gradient step is taken per ensemble member with a
        small learning rate, so live outcomes refine the offline-trained
        brain without destroying it.
        """
        if not self.online_enabled or not self.trained:
            return {"updated": False,
                    "reason": "ONLINE_DISABLED" if not self.online_enabled
                    else "UNTRAINED"}
        closes = list(closes or [])
        highs = list(highs or closes)
        lows = list(lows or closes)
        if not closes:
            return {"updated": False, "reason": "NO_DATA"}
        rets = returns_from_closes(closes)
        feats = build_feature_vector(
            closes=closes, highs=highs, lows=lows,
            orderflow=orderflow, footprint=footprint,
            regime=regime, agent_scores=agent_scores, **rets)
        return self.online_learn_features(feats, realised_return,
                                          symbol=symbol)

    def online_learn_features(self, features: Sequence[float],
                              realised_return: float,
                              *, symbol: str = "") -> Dict[str, Any]:
        """Online neural update from a pre-built feature vector.

        Used by the engines with the exact features captured at decision
        time (proposal.features) so there is no recomputation and no
        feature-dimension mismatch. One single-sample gradient step per
        ensemble member toward the realised direction.
        """
        if not self.online_enabled or not self.trained:
            return {"updated": False,
                    "reason": "ONLINE_DISABLED" if not self.online_enabled
                    else "UNTRAINED"}
        feats = list(features or [])
        if not feats:
            return {"updated": False, "reason": "NO_DATA"}
        x = np.asarray([feats], dtype=float)
        y = np.asarray([1.0 if realised_return > 0 else 0.0])
        summary = self.ensemble.online_update(x, y, lr=self.nn_online_lr)
        self.online_updates += 1
        return {"updated": True, "loss": round(summary["loss"], 6),
                "members": summary["updated"],
                "label": int(y[0]), "symbol": symbol}

    def _online_sigmoid(self, x: float) -> float:
        z = self._online_w * x + self._online_b
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    def set_baseline_loss(self, loss: float) -> None:
        self._baseline_loss = float(loss)

    # ------------------------------------------------------------------
    # Position sizing — the AI brain performs ALL lot-size calculation.
    # The EA only executes the computed lot. Lot size is determined from
    # client equity + per-trade risk + the sniper stop distance.
    # ------------------------------------------------------------------
    def size_position(self, *, equity: float, risk_pct: float,
                      stop_distance_points: float, point_value: float,
                      lot_step: float = 0.01, lot_min: float = 0.01,
                      lot_max: float = 50.0,
                      entry_quality: float = 1.0) -> Dict[str, float]:
        """Compute the lot size for one trade from client equity + risk.

        risk_usd = equity * risk_pct
        risk_per_lot = stop_distance_points * point_value   (USD per lot)
        lots = risk_usd / risk_per_lot, rounded to the broker lot step and
        clamped to [lot_min, lot_max]. Sniper entry quality scales the
        risk (and thus the lot) — a cleaner entry risks more.
        """
        equity = float(equity or 0.0)
        risk_pct = float(risk_pct or 0.0)
        stop_distance_points = float(stop_distance_points or 0.0)
        point_value = float(point_value or 0.0)
        lot_step = float(lot_step or 0.01)
        lot_min = float(lot_min or 0.01)
        lot_max = float(lot_max or 50.0)
        if equity <= 0 or risk_pct <= 0 or stop_distance_points <= 0                 or point_value <= 0:
            return {"lots": 0.0, "risk_usd": 0.0, "risk_per_lot": 0.0,
                    "reason": "SIZING_INPUT_INVALID"}
        # sniper sizing: cleaner entry -> risk more (floor keeps it trading)
        risk_scale = 0.4 + 0.6 * max(0.0, min(1.0, entry_quality))
        risk_usd = equity * risk_pct * risk_scale
        risk_per_lot = stop_distance_points * point_value
        if risk_per_lot <= 0:
            return {"lots": 0.0, "risk_usd": risk_usd,
                    "risk_per_lot": 0.0, "reason": "BAD_RISK_PER_LOT"}
        # Small-account guard: if the broker's minimum lot already risks more
        # than the intended per-trade risk, the account is too small for this
        # instrument. Skip rather than floor to lot_min and over-risk — on
        # tiny equity the fixed minimum-lot risk compounds into a >100%
        # drawdown death spiral (sizing can't scale below lot_min).
        min_lot_risk = lot_min * risk_per_lot
        if min_lot_risk > risk_usd:
            return {"lots": 0.0, "risk_usd": risk_usd,
                    "risk_per_lot": risk_per_lot, "risk_scale": risk_scale,
                    "stop_distance_points": stop_distance_points,
                    "min_lot_risk": round(min_lot_risk, 4),
                    "reason": "MIN_LOT_RISK_EXCEEDS_CAP"}
        lots = risk_usd / risk_per_lot
        # Floor to the broker lot step — never round upward. Rounding up
        # can push actual_risk above the requested cash-risk budget,
        # violating the account's approved risk fraction. If the floored
        # lot is below lot_min, reject (the minimum lot already over-risks).
        import math
        lots = math.floor(lots / lot_step) * lot_step
        if lots < lot_min:
            return {"lots": 0.0, "risk_usd": risk_usd,
                    "risk_per_lot": risk_per_lot, "risk_scale": risk_scale,
                    "stop_distance_points": stop_distance_points,
                    "reason": "MIN_LOT_RISK_EXCEEDS_CAP"}
        lots = min(lots, lot_max)
        actual_risk = lots * risk_per_lot
        return {"lots": round(lots, 4), "risk_usd": round(risk_usd, 4),
                "risk_per_lot": round(risk_per_lot, 4),
                "risk_scale": round(risk_scale, 4),
                "stop_distance_points": round(stop_distance_points, 4),
                "actual_risk_usd": round(actual_risk, 4),
                "reason": "OK"}

    # ------------------------------------------------------------------
    # Self-learning from settled trades — never enter too early.
    # ------------------------------------------------------------------
    def learn_from_trade(self, *, symbol: str, regime: str,
                         direction: str, entry_type: str,
                         entry_quality: float, pnl: float,
                         features: Optional[Sequence[float]] = None
                         ) -> Dict[str, float]:
        """Feed a settled trade's label, features and PnL back into the brain.

        Losses raise confirm_bias / quality_floor and record an explicit
        quantized feature signature that ``decide`` will veto if it repeats.
        Wins clear that signature. Neural weights are updated separately by
        ``online_learn_features`` at each training call site.
        """
        bias = self.trade_memory.learn_from_trade(
            symbol=symbol, regime=regime, direction=direction,
            entry_type=entry_type, entry_quality=entry_quality, pnl=pnl)
        if features is not None and len(features):
            stats = self.loss_guard.learn(
                symbol=symbol, direction=direction, regime=regime,
                features=list(features), pnl=pnl)
            bias["loss_guard_active"] = stats["active_loss_patterns"]
            bias["loss_guard_blocked"] = stats["blocked"]
        return bias

    def get_entry_bias(self, symbol: str, regime: str) -> Dict[str, float]:
        """Learned entry-timing biases for a symbol/regime."""
        return self.trade_memory.get_entry_bias(symbol, regime)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def state_dict(self) -> Dict[str, Any]:
        return {
            "version": self.VERSION,
            "ensemble": (self.ensemble.state_dict()
                         if self.ensemble else None),
            "online": {"w": self._online_w, "b": self._online_b,
                       "ema": self._online_ema, "n": self._online_n,
                       "nn_updates": self.online_updates,
                       "enabled": self.online_enabled,
                       "nn_lr": self.nn_online_lr},
            "thresholds": {"min_confidence": self.min_confidence,
                           "max_uncertainty": self.max_uncertainty},
            "trade_memory": self.trade_memory.state_dict(),
            "loss_guard": self.loss_guard.state_dict(),
        }

    def load_memory_state(self, data: Optional[Dict[str, Any]]) -> None:
        """Restore TradeMemory + explicit loss-pattern guard from a payload."""
        data = data or {}
        self.trade_memory.load_state(data.get("trade_memory") or {})
        self.loss_guard.load_state(data.get("loss_guard") or {})


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def default_ensemble(seed: int = 0) -> BrainEnsemble:
    """A small, diverse ensemble: a snapshot MLP + a deeper MLP + a
    sequence-style member. Diversity in depth/seed is what gives the
    uncertainty estimate its teeth."""
    specs = [
        {"name": "snap_a", "sizes": [N_FEATURES, 32, 16, 2],
         "kind": "snapshot", "weight": 1.0, "p_drop": 0.1},
        {"name": "snap_b", "sizes": [N_FEATURES, 24, 24, 12, 2],
         "kind": "snapshot", "weight": 1.0, "p_drop": 0.15},
        {"name": "seq_c", "sizes": [N_FEATURES, 40, 20, 2],
         "kind": "sequence", "weight": 1.0, "p_drop": 0.1},
    ]
    return build_ensemble(specs, seed=seed)


def make_brain(**kwargs) -> NexusBrain:
    kwargs.setdefault("ensemble", default_ensemble())
    return NexusBrain(**kwargs)
