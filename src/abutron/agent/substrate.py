"""Brain signal substrate — exposes the V9 brains to the agents.

The ``BrainSubstrate`` is the *signal* layer of the agentic system. It
wraps the existing V9 brain stack (``NexusBrain`` / ``UnifiedBrain``)
and exposes a single ``predict`` call that returns a ``Signal``.

It is deliberately resilient: if no trained brain checkpoint is present
(offline, CI, sandbox), it produces a transparent *rule-based* signal
derived from the closed-candle context and marks it ``brain_source =
"rule_fallback"``. It never fabricates a trained-brain signal it does
not have — the provenance is always explicit so the decision ledger can
audit whether the signal came from a real model or a deterministic rule.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config import BRAIN_MODEL_DIR, BRAIN_MC_SAMPLES
from .interfaces import AgentContext, Direction, Signal


class BrainSubstrate:
    """Loads and serves V9 brain predictions as ``Signal`` objects."""

    def __init__(
        self,
        brain: Any = None,
        brain_dir: Optional[str] = None,
        mc_samples: int = BRAIN_MC_SAMPLES,
    ) -> None:
        # An explicitly injected brain (used in tests / preloaded runtimes).
        self._brain = brain
        self._brain_dir = brain_dir or BRAIN_MODEL_DIR
        self._mc_samples = mc_samples
        self._loaded = brain is not None
        self._load_error: str = ""

    # ------------------------------------------------------------------ #
    # Availability
    # ------------------------------------------------------------------ #
    @property
    def brain(self) -> Any:
        return self._brain

    @property
    def load_error(self) -> str:
        return self._load_error

    def is_available(self) -> bool:
        """True if a real (non-fallback) brain is loaded."""
        if self._loaded and self._brain is not None:
            return True
        if not self._loaded and not self._load_error:
            self._try_load()
        return self._loaded and self._brain is not None

    # ------------------------------------------------------------------ #
    # Lazy loading
    # ------------------------------------------------------------------ #
    def _try_load(self) -> None:
        """Attempt to load a UnifiedBrain from the configured brain root.

        Any failure (missing checkpoint, missing numeric deps) is captured
        in ``_load_error`` and the substrate silently serves rule signals
        instead — the agentic system must never be blocked by a missing
        brain file.
        """
        try:
            from ..brain.unified import load_unified
            brain = load_unified(self._brain_dir, mc_samples=self._mc_samples)
            if brain is not None:
                self._brain = brain
                self._loaded = True
                return
            self._load_error = "BRAIN_EMPTY"
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"BRAIN_LOAD:{type(exc).__name__}"
            self._loaded = False

    # ------------------------------------------------------------------ #
    # Prediction
    # ------------------------------------------------------------------ #
    def predict(self, context: AgentContext,
                features: Optional[List[float]] = None) -> Signal:
        """Return a ``Signal`` for the given context.

        Uses the real brain when available and a feature vector can be
        constructed; otherwise returns a deterministic rule signal from the
        bar structure. Provenance (``brain_source``) is always explicit.
        """
        if self.is_available() and features:
            try:
                return self._predict_with_brain(context, features)
            except Exception as exc:  # noqa: BLE001
                # Brain path failed — degrade to rule signal, record reason.
                rule = self._rule_signal(context)
                rule.reasons.append(f"brain_predict_error:{type(exc).__name__}")
                return rule
        if self.is_available():
            # Brain present but no feature vector supplied — try to build
            # one from the context's orderflow/footprint if available.
            built = self._features_from_context(context)
            if built:
                try:
                    return self._predict_with_brain(context, built)
                except Exception as exc:  # noqa: BLE001
                    rule = self._rule_signal(context)
                    rule.reasons.append(
                        f"brain_predict_error:{type(exc).__name__}")
                    return rule
        return self._rule_signal(context)

    # ------------------------------------------------------------------ #
    # Real-brain path
    # ------------------------------------------------------------------ #
    def _predict_with_brain(self, context: AgentContext,
                            features: List[float]) -> Signal:
        brain = self._brain
        # Build the OHLC series the brain's feature builder needs. A
        # single bar yields zero return features, so we derive a minimal
        # series from the bar structure (and any explicit series carried
        # on the context) to give the brain real input.
        closes = list(context.closes) or _series_from_bar(context)
        highs = list(context.highs) or _series_from_bar(context, "high")
        lows = list(context.lows) or _series_from_bar(context, "low")
        # UnifiedBrain has a high-level ``decide``; NexusBrain has
        # ``predict``. Prefer ``decide`` when present.
        if hasattr(brain, "decide"):
            proposal = brain.decide(
                symbol=context.symbol,
                closes=closes, highs=highs, lows=lows,
                orderflow=context.orderflow or None,
                footprint=context.footprint or None,
            )
        else:
            import numpy as np
            x = np.asarray([features], dtype=float)
            proposal = brain.predict(symbol=context.symbol, features=features,
                                     regime="", x=x) if hasattr(
                brain, "predict") else None
        if proposal is None:
            return self._rule_signal(context)
        return self._signal_from_proposal(proposal, context, features)

    def _signal_from_proposal(self, proposal: Any, context: AgentContext,
                              features: List[float]) -> Signal:
        direction = Direction.from_str(str(getattr(proposal, "direction",
                                                    "HOLD")))
        return Signal(
            symbol=context.symbol,
            direction=direction,
            conviction=float(getattr(proposal, "conviction", 0.0)),
            confidence=float(getattr(proposal, "confidence", 0.0)),
            uncertainty=float(getattr(proposal, "uncertainty", 0.0)),
            position_size=float(getattr(proposal, "position_size", 0.0)),
            regime=str(getattr(proposal, "regime", "")),
            features=list(features),
            brain_source="nexus_unified",
            reasons=["brain_proposal"],
        )

    def _features_from_context(self, context: AgentContext) -> List[float]:
        """Best-effort feature vector from the context.

        The full V9 ``build_signal_bundle`` requires a series of closed
        candles. Here we derive a compact, deterministic feature vector
        from the bar structure + orderflow/footprint so a loaded brain
        can still be exercised. Returns ``[]`` if nothing is derivable.
        """
        if context.bar_close <= 0:
            return []
        of = context.orderflow or {}
        fp = context.footprint or {}
        vec: List[float] = [
            (context.bar_close - context.bar_open) / context.bar_close
            if context.bar_open else 0.0,
            (context.high - context.low) / context.bar_close
            if context.bar_close else 0.0,
            (context.bar_close - context.low) / (context.high - context.low)
            if (context.high - context.low) else 0.5,
            float(of.get("imbalance", 0.0)),
            float(of.get("buy_ratio", 0.5)) - 0.5,
            float(fp.get("va_imbalance", 0.0)),
        ]
        return vec

    # ------------------------------------------------------------------ #
    # Deterministic fallback signal
    # ------------------------------------------------------------------ #
    def _rule_signal(self, context: AgentContext) -> Signal:
        """A transparent signal derived from bar structure.

        Direction from close vs open (plus high/low position), with low,
        honest conviction. This is a *placeholder* signal for when no
        trained brain is present — never presented as a model output.
        """
        o, h, l, c = (context.bar_open, context.high, context.low,
                      context.bar_close)
        reasons: List[str] = [f"rule_fallback:{self._load_error or 'no_brain'}"]
        direction = Direction.HOLD
        conviction = 0.0
        confidence = 0.0
        uncertainty = 0.9  # flag: rule signal is high-uncertainty
        if c > o and c > (h + l) / 2.0:
            direction = Direction.BUY
            conviction = 0.30
        elif c < o and c < (h + l) / 2.0:
            direction = Direction.SELL
            conviction = 0.30
        else:
            conviction = 0.15
        confidence = min(0.4, conviction)
        # position_size follows the V9 conviction-scaling convention.
        position_size = float(min(1.0, 0.25 + 0.75 * conviction))
        return Signal(
            symbol=context.symbol,
            direction=direction,
            conviction=conviction,
            confidence=confidence,
            uncertainty=uncertainty,
            position_size=position_size,
            regime="rule",
            features=[],
            brain_source="rule_fallback",
            reasons=reasons,
        )


def _series_from_bar(context: AgentContext,
                     key: str = "close") -> List[float]:
    """Derive a minimal OHLC series from a single bar for the brain.

    The unified feature builder needs a series to compute returns and
    ATR. A single closed candle yields zero return features, which
    starves the brain. We synthesise a short deterministic series from
    the bar's OHLC so the brain still receives real (non-zero) input
    when only one bar is available in the agentic context.
    """
    o, h, l, c = (context.bar_open, context.high, context.low,
                  context.bar_close)
    if c <= 0 and o <= 0:
        return []
    # 2-bar series: a synthetic prior bar + the current bar. The prior
    # bar is derived from the current bar so returns are non-zero and
    # deterministic.
    if key == "close":
        prior = o if o else c * 0.99
        return [prior, c]
    if key == "high":
        prior = max(o, c) if (o or c) else h
        return [prior, h]
    if key == "low":
        prior = min(o, c) if (o or c) else l
        return [prior, l]
    return [o, c]
