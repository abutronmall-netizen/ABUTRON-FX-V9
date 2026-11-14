"""Guardrails for the self-learning layer.

The self-learning AI may only adjust *target weights* (and agent trust).
It must NEVER:
  * place, modify, or cancel an order
  * widen a stop, raise a position beyond caps, or increase risk
  * bypass the fail-closed candidate review / broker-native gates
  * fabricate broker evidence

Every overlay output is clamped to a hard bound, and a guardrail report
is produced so operators can see exactly what the learner changed and why.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class OverlayDecision:
    """The bounded weight overlay the learner is allowed to apply."""
    multiplier: float          # global scale applied to target weights
    per_symbol: Dict[str, float]   # per-symbol multiplier
    clipped: bool              # whether any value hit a hard bound
    notes: List[str]

    def to_dict(self) -> Dict:
        return {
            "multiplier": round(self.multiplier, 4),
            "per_symbol": {k: round(v, 4) for k, v in self.per_symbol.items()},
            "clipped": self.clipped,
            "notes": self.notes,
        }


class SelfLearnGuardrails:
    """Hard bounds on what the self-learning layer may change."""

    def __init__(self, global_min: float = 0.5, global_max: float = 1.5,
                 symbol_min: float = 0.5, symbol_max: float = 1.5):
        self.global_min = global_min
        self.global_max = global_max
        self.symbol_min = symbol_min
        self.symbol_max = symbol_max

    def clamp_multiplier(self, value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    def build_overlay(self, *, learned_multiplier: float,
                      per_symbol_multiplier: Dict[str, float]) -> OverlayDecision:
        """Clamp all learner outputs to hard bounds.

        `learned_multiplier` is the model's global edge-confidence scalar;
        `per_symbol_multiplier` the per-symbol trust adjustment. Both are
        clamped to [lo, hi] so the learner can only *reduce* or modestly
        *increase* weight — never amplify risk beyond the bound.
        """
        notes: List[str] = []
        clipped = False

        if not (self.global_min <= learned_multiplier <= self.global_max):
            clamp = self.clamp_multiplier(learned_multiplier,
                                          self.global_min, self.global_max)
            notes.append(
                f"global multiplier {learned_multiplier:.3f} clamped to {clamp:.3f}")
            clipped = True
            learned_multiplier = clamp

        out_sym: Dict[str, float] = {}
        for sym, val in per_symbol_multiplier.items():
            if not (self.symbol_min <= val <= self.symbol_max):
                clamp = self.clamp_multiplier(val, self.symbol_min,
                                              self.symbol_max)
                notes.append(
                    f"{sym} multiplier {val:.3f} clamped to {clamp:.3f}")
                clipped = True
                val = clamp
            out_sym[sym] = val

        return OverlayDecision(multiplier=learned_multiplier,
                               per_symbol=out_sym, clipped=clipped, notes=notes)

    def apply_to_weights(self, weights: Dict[str, float],
                         overlay: OverlayDecision) -> Dict[str, float]:
        """Apply the bounded overlay to a target-weight book.

        Only multiplies weights; never creates a position that wasn't
        already proposed, and never changes direction.
        """
        out: Dict[str, float] = {}
        for sym, w in weights.items():
            m = overlay.per_symbol.get(sym, overlay.multiplier)
            out[sym] = w * m
        return out

    @staticmethod
    def execution_safety_assertion() -> bool:
        """The self-learning layer has NO execution authority.

        This is a static invariant: the learner produces weights/trust
        only. Order submission is exclusively the single-writer dispatcher's
        job, gated by the fail-closed candidate review.
        """
        return True
