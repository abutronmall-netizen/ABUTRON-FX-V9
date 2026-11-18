"""Pluggable reasoning backend for the agentic system.

The hybrid multi-agent system needs a *reasoning* layer that interprets
the brain signal and market context and decides intent — but it must not
hard-depend on a live LLM gateway. Two backends implement the same
contract:

* ``LLMReasoningBackend``  — wraps the existing V9 LLM advisors
  (``abutron.apex.gemma`` single advisor + ``abutron.apex.debate``
  multi-agent debate). Advisory-only, degrades to ``NO_ADVISORY`` on any
  failure. Used when a Gemma/OpenAI-compatible gateway is configured.
* ``RuleReasoningBackend`` — a deterministic, fully self-contained
  reasoner. It synthesises a direction and narrative directly from the
  signal, ensemble uncertainty, order-flow and footprint context. Used
  when no gateway is configured (CI, sandbox, offline) so the agentic AI
  is never blocked by a missing model.

Both return the same ``reasoning`` dict shape so downstream agents are
agnostic to which backend produced it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from ..config import (
    DEBATE_ENABLED, GEMMA_BASE_URL, GEMMA_ENABLED, GEMMA_MODEL,
)
from .interfaces import AgentContext, Direction, Signal


# A synthesised reasoning result always carries at least these keys.
REASONING_SCHEMA = (
    "signal", "confidence", "narrative", "considerations", "backend"
)


class ReasoningBackend(ABC):
    """Contract for all reasoning backends."""

    name: str = "reasoning"

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this backend has a usable model/gateway."""

    @abstractmethod
    def reason(self, context: AgentContext,
               signal: Optional[Signal]) -> Dict[str, Any]:
        """Synthesise a decision intent from context + signal.

        Returns a dict with at least ``signal`` (Direction), ``confidence``
        (0..1), ``narrative`` (str), ``considerations`` (list[str]) and
        ``backend`` (str).
        """


def _normalise_direction(value: Any) -> Direction:
    d = Direction.from_str(str(value or ""))
    return d


# ---------------------------------------------------------------------- #
# Deterministic rule-based reasoner
# ---------------------------------------------------------------------- #
class RuleReasoningBackend(ReasoningBackend):
    """Deterministic reasoner — the always-available fallback.

    Produces a transparent, reproducible synthesis from the signal and
    context. It is *not* a no-op: it applies a confidence-weighted
    confidence gate, an uncertainty veto, and order-flow/footprint
    nudges, then commits to a direction. Every rule is inspectable in the
    ``considerations`` list, so the decision is fully auditable.
    """

    name = "rule"

    def is_configured(self) -> bool:
        # Always available; no external dependency.
        return True

    def reason(self, context: AgentContext,
               signal: Optional[Signal]) -> Dict[str, Any]:
        considerations: list[str] = []
        if signal is None:
            considerations.append("no_brain_signal")
            return {
                "signal": Direction.HOLD,
                "confidence": 0.0,
                "narrative": "No brain signal available; holding.",
                "considerations": considerations,
                "backend": self.name,
            }

        direction = signal.direction
        conviction = float(signal.conviction)
        confidence = float(signal.confidence)
        uncertainty = float(signal.uncertainty)

        considerations.append(
            f"brain_source={signal.brain_source} dir={direction.value} "
            f"conv={conviction:.3f} conf={confidence:.3f} "
            f"unc={uncertainty:.3f} size={signal.position_size:.3f}")

        # Uncertainty veto: if the ensemble is maximally uncertain, hold
        # rather than act on noise. Threshold is deliberately conservative
        # and configurable via the signal's reported uncertainty.
        if uncertainty >= 0.95:
            considerations.append("uncertainty_veto")
            return {
                "signal": Direction.HOLD,
                "confidence": 0.0,
                "narrative": (
                    f"Ensemble uncertainty {uncertainty:.3f} at veto "
                    f"threshold; holding instead of acting on noise."),
                "considerations": considerations,
                "backend": self.name,
            }

        # Confidence floor: act only if the model is at least somewhat
        # confident AND conviction is non-trivial.
        effective_conf = confidence * conviction
        if effective_conf < 0.20:
            considerations.append("low_edge_hold")
            return {
                "signal": Direction.HOLD,
                "confidence": effective_conf,
                "narrative": (
                    f"Combined edge {effective_conf:.3f} below floor; "
                    f"holding."),
                "considerations": considerations,
                "backend": self.name,
            }

        # Order-flow / footprint nudge: reinforce or damp the direction.
        of = context.orderflow or {}
        imbalance = _as_float(of.get("imbalance"), 0.0)
        buy_ratio = _as_float(of.get("buy_ratio"), 0.5)
        if direction is Direction.BUY and imbalance < -0.10:
            considerations.append("orderflow_counter_damp")
            effective_conf *= 0.85
        elif direction is Direction.SELL and imbalance > 0.10:
            considerations.append("orderflow_counter_damp")
            effective_conf *= 0.85
        if direction is Direction.BUY and buy_ratio >= 0.65:
            considerations.append("orderflow_aligned_boost")
            effective_conf = min(1.0, effective_conf * 1.05)
        elif direction is Direction.SELL and buy_ratio <= 0.35:
            considerations.append("orderflow_aligned_boost")
            effective_conf = min(1.0, effective_conf * 1.05)

        # Spread sanity: a wide spread erodes the edge.
        spread = _as_float(context.spread, 0.0)
        if spread > 0 and context.bar_close > 0:
            spread_frac = spread / context.bar_close
            if spread_frac > 0.0005:
                considerations.append("wide_spread_damp")
                effective_conf *= 0.80

        narrative = (
            f"{direction.value} on {context.symbol} @ {context.bar_close:.5f}; "
            f"conviction {conviction:.3f}, confidence {confidence:.3f}, "
            f"uncertainty {uncertainty:.3f}; effective edge "
            f"{effective_conf:.3f} after orderflow/spread adjustments.")

        return {
            "signal": direction,
            "confidence": round(min(1.0, max(0.0, effective_conf)), 4),
            "narrative": narrative,
            "considerations": considerations,
            "backend": self.name,
        }


# ---------------------------------------------------------------------- #
# LLM-backed reasoner (wraps existing V9 advisors)
# ---------------------------------------------------------------------- #
class LLMReasoningBackend(ReasoningBackend):
    """LLM reasoning backend wrapping the V9 Gemma + Debate advisors.

    Advisory-only: the LLM never places orders. On any outage, refusal,
    malformed response or budget exhaustion the advisory degrades to
    ``NO_ADVISORY`` and the caller is expected to fall back to the
    deterministic ``RuleReasoningBackend``. The LLM backend therefore
    never blocks the decision cycle.
    """

    name = "llm"

    def __init__(
        self,
        base_url: str = GEMMA_BASE_URL,
        model: str = GEMMA_MODEL,
        enabled: bool = GEMMA_ENABLED,
        debate_enabled: bool = DEBATE_ENABLED,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.enabled = enabled and bool(base_url)
        self.debate_enabled = debate_enabled
        self._advisor = None
        self._debate = None

    @classmethod
    def from_config(cls) -> "LLMReasoningBackend":
        return cls(base_url=GEMMA_BASE_URL, model=GEMMA_MODEL,
                   enabled=GEMMA_ENABLED, debate_enabled=DEBATE_ENABLED)

    def or_fallback(self) -> ReasoningBackend:
        """Return this backend if configured, else the rule backend."""
        return self if self.is_configured() else RuleReasoningBackend()

    def is_configured(self) -> bool:
        return self.enabled

    def _load_advisors(self):
        """Lazily import the V9 LLM advisors (avoid hard import cost)."""
        if self._advisor is None or self._debate is None:
            from ..apex.gemma import gemma_advisor
            from ..apex.debate import debate_advisor
            self._advisor = gemma_advisor
            self._debate = debate_advisor
        return self._advisor, self._debate

    def reason(self, context: AgentContext,
               signal: Optional[Signal]) -> Dict[str, Any]:
        considerations: list[str] = []
        if not self.is_configured():
            return _no_advisory(considerations, "LLM_DISABLED")

        try:
            advisor, debate = self._load_advisors()
        except Exception as exc:  # noqa: BLE001
            return _no_advisory(considerations, f"LLM_IMPORT:{type(exc).__name__}")

        # Build the advisory context from the signal + market context.
        ctx_for_llm: Dict[str, Any] = {
            "symbol": context.symbol,
            "regime": signal.regime if signal else "",
            "price": context.bar_close,
            "spread": context.spread,
            "orderflow": context.orderflow,
            "footprint": context.footprint,
            "signal": {
                "direction": signal.direction.value if signal else "HOLD",
                "conviction": signal.conviction if signal else 0.0,
                "confidence": signal.confidence if signal else 0.0,
                "uncertainty": signal.uncertainty if signal else 0.0,
            } if signal else None,
        }

        # Prefer the multi-agent debate; fall back to the single advisor.
        advisory = None
        if self.debate_enabled:
            try:
                advisory = debate.advise(ctx_for_llm)
            except Exception as exc:  # noqa: BLE001
                considerations.append(f"debate_error:{type(exc).__name__}")
                advisory = None
        if advisory is None or not getattr(advisory, "available", False):
            try:
                advisory = advisor.advise(ctx_for_llm)
            except Exception as exc:  # noqa: BLE001
                considerations.append(f"advisor_error:{type(exc).__name__}")
                advisory = None

        if advisory is None or not getattr(advisory, "available", False):
            reason = getattr(advisory, "reason", "LLM_UNAVAILABLE")
            considerations.append(f"llm_unavailable:{reason}")
            # Degrade: blend the signal into a deterministic synthesis.
            rule = RuleReasoningBackend().reason(context, signal)
            rule["considerations"] = considerations + rule["considerations"]
            rule["backend"] = self.name + ":fallback"
            return rule

        signal_str = getattr(advisory, "signal", "NEUTRAL")
        confidence = float(getattr(advisory, "confidence", 0.0))
        narrative = getattr(advisory, "narrative", "")
        direction = _normalise_direction(signal_str)
        considerations.append(
            f"llm_model={getattr(advisory, 'model', self.model)} "
            f"source={type(advisory).__name__}")

        return {
            "signal": direction,
            "confidence": round(max(0.0, min(1.0, confidence)), 4),
            "narrative": narrative,
            "considerations": considerations,
            "backend": self.name,
        }


def _no_advisory(considerations: list[str], reason: str) -> Dict[str, Any]:
    considerations.append(reason)
    return {
        "signal": Direction.HOLD,
        "confidence": 0.0,
        "narrative": f"LLM advisory unavailable ({reason}).",
        "considerations": considerations,
        "backend": "llm:unavailable",
    }


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN guard
