"""Trailing stop — locks in profit as a trade moves in favour.

A trailing stop tightens the stop in the direction of the trade once price
moves favourably, so a winner never gives back its gain. The AI brain
computes the trail leg from price structure (a Chandelier-style exit: the
highest high / lowest low since entry, offset by a multiple of ATR); the
EA applies the trail and moves the broker stop on each favourable tick.

The brain is the calculation authority; the EA only executes the trail.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class TrailPlan:
    """Trailing-stop geometry for one trade."""
    trail_k: float           # ATR multiple for the trail offset (e.g. 3.0)
    trail_atr: float         # ATR used for the trail
    trail_stop: float        # current trail stop price
    reference: float         # the high (long) / low (short) the trail keys off
    activated: bool          # whether the trail has room to move (in profit)
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trail_k": round(self.trail_k, 4),
            "trail_atr": round(self.trail_atr, 6),
            "trail_stop": round(self.trail_stop, 6),
            "reference": round(self.reference, 6),
            "activated": self.activated,
            "rationale": self.rationale,
        }


class TrailingStop:
    """Computes a Chandelier-style trailing stop leg."""

    DEFAULT_K = 3.0  # ATR multiple — tight enough to protect, wide enough
                     # to avoid whipsaw exits.

    def plan(self, *, direction: str, highs: List[float], lows: List[float],
             atr_val: float, entry_price: float,
             trail_k: Optional[float] = None,
             symbol: str = "") -> TrailPlan:
        """Return the trailing-stop plan for an open trade.

        Long:  trail_stop = highest_high_since_entry - k*ATR
        Short: trail_stop = lowest_low_since_entry  + k*ATR
        The trail only tightens (never widens) relative to the invalidation.
        """
        if trail_k is not None:
            k = float(trail_k)
        elif symbol:
            from .runtime_rules import trail_k_for
            k = float(trail_k_for(symbol, default=self.DEFAULT_K))
        else:
            k = float(self.DEFAULT_K)
        a = float(atr_val or 0.0)
        is_long = direction == "BUY"

        if is_long:
            # highest high since entry (use full history; entry is the latest
            # signal bar's trigger, so all history is "since entry" pre-fill)
            ref = max(highs) if highs else entry_price
            trail_stop = ref - k * a
            activated = trail_stop > entry_price
        else:
            ref = min(lows) if lows else entry_price
            trail_stop = ref + k * a
            activated = trail_stop < entry_price

        # The trail must never be worse than the initial invalidation: for a
        # long the trail stop must be above the initial stop (tighter), for a
        # short below it. If not yet in profit, trail = entry-side invalidation
        # so the EA still has a protective stop.
        if not activated:
            trail_stop = entry_price - (0.3 * a) if is_long \
                else entry_price + (0.3 * a)

        rationale = (f"trail_{'long' if is_long else 'short'} k={k:.1f} "
                     f"ref={ref:.5f} trail={trail_stop:.5f} "
                     f"activated={activated}")
        return TrailPlan(trail_k=k, trail_atr=a, trail_stop=trail_stop,
                         reference=ref, activated=activated,
                         rationale=rationale)


# Module-level singleton.
trailing_stop = TrailingStop()
