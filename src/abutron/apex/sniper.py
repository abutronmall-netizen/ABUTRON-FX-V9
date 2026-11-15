"""Sniper entry engine — precise, low-drawdown trade entries.

The user's core insight: a trading system only works if every entry is a
*sniper entry* — filled at the best price with no (or very small) drawdown.
Drawdown is overwhelmingly an *entry* problem, not a direction problem: a
correct direction entered at a bad price still draws down before it runs.

This module replaces naive market-at-close entries with a trigger-based
entry plan computed from the market's own structure:

- **LIMIT (pullback) entry** — enter at a structural level (recent swing,
  EMA, Donchian band) where the signal edge is still intact. Filling at
  the level means the trade starts near its edge, so adverse excursion to
  invalidation is minimal.
- **CONFIRM (breakout) entry** — when price is already extended and no
  pullback is available, enter on a continuation trigger (a confirmed
  break of the recent swing) rather than chasing a stretched close.
- **Tight invalidation** — a stop just beyond the trigger (a fraction of
  ATR), so a wrong entry exits with a tiny loss instead of a full ATR.
- **Entry quality score** — ranks how clean/precise the entry is
  (level proximity, flow/footprint alignment, regime, extension). Sizing
  is scaled by this score: a sniper-grade entry takes full size; a
  stretched entry takes less. This is sizing by entry precision — still
  always-trading (every edge is entered), but drawdown is minimised by
  entering precisely and sizing down when the entry is imperfect.

No abstention: this is an entry-precision layer, not a trade blocker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..apex.indicators import atr, ema, donchian, rsi


@dataclass
class SniperEntry:
    """A precise entry plan for one signal."""
    entry_type: str               # "LIMIT" (pullback) | "CONFIRM" (breakout)
    trigger_price: float          # the exact price to enter at
    invalidation: float           # tight stop beyond the trigger (tiny loss)
    entry_quality: float          # 0..1 — how clean/precise the entry is
    take_profit: float            # tight TP that locks profit fast
    rationale: str = ""
    level_type: str = ""          # "swing" | "ema" | "donchian" | "confirm"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_type": self.entry_type,
            "trigger_price": round(self.trigger_price, 6),
            "invalidation": round(self.invalidation, 6),
            "entry_quality": round(self.entry_quality, 4),
            "take_profit": round(self.take_profit, 6),
            "rationale": self.rationale,
            "level_type": self.level_type,
        }


class SniperEntryEngine:
    """Computes a low-drawdown entry plan from market structure."""

    # Fraction of ATR used for the tight invalidation buffer. Smaller =
    # smaller wrong-entry loss, but more whipsaw exits. 0.3 is a tight,
    # sniper-style invalidation.
    INVALIDATION_ATR_FRAC = 0.3
    # Tight take-profit multiples (lock profit fast -> less time-in-trade
    # drawdown). Kept close to the entry edge.
    TP_ATR_MULT = 1.2
    # Max distance (in ATR) for a structural level to count as a practical
    # pullback target. In a trend the EMA/swing sits a few ATR away and
    # price reliably returns to it — that is the sniper entry. Beyond this
    # the market is gapped/extended and a CONFIRM entry is used instead.
    PULLBACK_ATR_MAX = 4.0

    def __init__(self, *, tp_atr_mult: float = TP_ATR_MULT,
                 invalidation_atr_frac: float = INVALIDATION_ATR_FRAC,
                 pullback_atr_max: float = PULLBACK_ATR_MAX):
        # Configurable geometry so a win-rate optimizer can sweep TP/SL
        # multiples without touching the class defaults.
        self.tp_atr_mult = tp_atr_mult
        self.invalidation_atr_frac = invalidation_atr_frac
        self.pullback_atr_max = pullback_atr_max

    def plan(self, *, symbol: str, direction: str,
             closes: List[float], highs: List[float], lows: List[float],
             atr_val: float, of_features: Optional[Dict[str, float]] = None,
             fp_features: Optional[Dict[str, float]] = None,
             regime: str = "", uncertainty: float = 0.0,
             conviction: float = 0.0,
             confirm_bias: float = 0.0,
             quality_floor: float = 0.0) -> SniperEntry:
        """Return a SniperEntry for the signal, or a best-effort plan.

        confirm_bias / quality_floor come from the brain's TradeMemory:
        after losses the brain raises confirm_bias (prefer CONFIRM entries
        — wait for confirmation, never enter early) and quality_floor (the
        effective entry quality is floored, demanding a cleaner setup).
        """
        of = of_features or {}
        fp = fp_features or {}
        price = closes[-1] if closes else 0.0
        a = atr_val or 0.0
        is_buy = direction == "BUY"

        # --- Structural levels ---------------------------------------
        ema_val = ema(closes, 21) if len(closes) >= 21 else None
        dn_low, dn_high = donchian(highs, lows, 20) if len(highs) >= 20 \
            else (None, None)
        swing_low = min(lows[-10:]) if lows else None
        swing_high = max(highs[-10:]) if highs else None
        rsi_val = rsi(closes, 14) if len(closes) >= 15 else None

        # Candidate pullback levels toward which price tends back. For a
        # BUY we want support below price; for a SELL resistance above.
        levels = []
        if is_buy:
            if swing_low is not None:
                levels.append(("swing", swing_low))
            if ema_val is not None:
                levels.append(("ema", ema_val))
            if dn_low is not None:
                levels.append(("donchian", dn_low))
        else:
            if swing_high is not None:
                levels.append(("swing", swing_high))
            if ema_val is not None:
                levels.append(("ema", ema_val))
            if dn_high is not None:
                levels.append(("donchian", dn_high))

        # A usable LIMIT level must be on the correct side of price and
        # within a sensible pullback distance (< ~1.5 ATR away, otherwise
        # the limit won't fill in a normal pullback).
        usable = []
        for kind, lvl in levels:
            if lvl is None or lvl <= 0:
                continue
            if is_buy and lvl > price:
                continue  # support must be at/below price for a BUY
            if not is_buy and lvl < price:
                continue  # resistance must be at/above price for a SELL
            dist = abs(price - lvl)
            if a > 0 and dist > self.pullback_atr_max * a:
                continue  # too far to be a practical pullback
            usable.append((kind, lvl, dist))

        if usable and confirm_bias <= 0.5:
            # Prefer the closest valid level (cleanest pullback = least
            # drawdown to fill). Ties broken by structural priority.
            priority = {"ema": 0, "swing": 1, "donchian": 2}
            usable.sort(key=lambda u: (u[2], priority.get(u[0], 3)))
            level_type, trigger, dist = usable[0]
            entry_type = "LIMIT"
            inv = trigger - (self.invalidation_atr_frac * a) if is_buy \
                else trigger + (self.invalidation_atr_frac * a)
        else:
            # No usable pullback level, OR the brain's learned confirm_bias
            # is high (it learned from a loss to wait for confirmation and
            # never enter too early). Enter on a CONFIRM trigger.
            level_type = "confirm"
            trigger = price
            entry_type = "CONFIRM"
            ref = swing_low if is_buy else swing_high
            if ref is not None:
                inv = ref - (self.invalidation_atr_frac * a) if is_buy \
                    else ref + (self.invalidation_atr_frac * a)
            else:
                inv = price - (self.invalidation_atr_frac * a) if is_buy \
                    else price + (self.invalidation_atr_frac * a)

        # --- Tight take-profit (lock profit fast) ---------------------
        if is_buy:
            tp = trigger + self.tp_atr_mult * a
        else:
            tp = trigger - self.tp_atr_mult * a

        # --- Entry quality score (0..1) ------------------------------
        quality = self._quality(
            is_buy=is_buy, entry_type=entry_type, dist=dist if usable else a,
            of=of, fp=fp, regime=regime, uncertainty=uncertainty,
            conviction=conviction, rsi=rsi_val, price=price, trigger=trigger,
            a=a)

        # Apply the brain's learned quality floor (after a loss the brain
        # demands a cleaner setup before sizing up).
        effective_quality = max(quality, quality_floor)
        rationale = (
            f"{entry_type} {level_type} @{trigger:.5f} "
            f"inv={inv:.5f} tp={tp:.5f} quality={effective_quality:.3f} "
            f"dir={direction}")
        return SniperEntry(
            entry_type=entry_type, trigger_price=trigger,
            invalidation=inv, entry_quality=effective_quality,
            take_profit=tp, rationale=rationale, level_type=level_type)

    # ------------------------------------------------------------------
    def _quality(self, *, is_buy: bool, entry_type: str, dist: float,
                 of: Dict[str, float], fp: Dict[str, float],
                 regime: str, uncertainty: float, conviction: float,
                 rsi: Optional[float], price: float, trigger: float,
                 a: float) -> float:
        """Score entry precision 0..1. Higher = cleaner, lower-drawdown."""
        q = 0.5  # baseline

        # 1. Level proximity: a LIMIT pullback close to price is the
        #    sniper ideal — least adverse path to the fill.
        if entry_type == "LIMIT":
            a_ref = a if a > 0 else (abs(price) * 0.001 or 1.0)
            proximity = 1.0 - min(1.0, dist / (self.pullback_atr_max * a_ref))
            q += 0.25 * proximity
        else:
            # CONFIRM entries are inherently less precise.
            q -= 0.10

        # 2. Order-flow alignment with the direction.
        flow = 0.0
        if of:
            if is_buy:
                flow = of.get("delta_pct", 0.0)
                flow += of.get("buy_ratio", 0.5) - 0.5
            else:
                flow = -of.get("delta_pct", 0.0)
                flow += 0.5 - of.get("buy_ratio", 0.5)
            flow = max(-1.0, min(1.0, flow * 2.0))
            q += 0.15 * flow

        # 3. Footprint value-area alignment.
        if fp:
            v = fp.get("va_imbalance", 0.0)
            align = v if is_buy else -v
            q += 0.10 * max(-1.0, min(1.0, align * 2.0))

        # 4. Regime alignment (trend regimes favour directional entries).
        regime = (regime or "").lower()
        if is_buy and "up" in regime or (not is_buy and "down" in regime):
            q += 0.05
        elif regime and ("up" in regime or "down" in regime):
            q -= 0.05

        # 5. Conviction (stronger edge -> cleaner entry) and uncertainty
        #    (higher uncertainty -> slightly less precise).
        q += 0.10 * max(0.0, min(1.0, conviction))
        q -= 0.10 * max(0.0, min(1.0, uncertainty))

        # 6. RSI overextension penalty for CONFIRM entries (chasing).
        if entry_type == "CONFIRM" and rsi is not None:
            if is_buy and rsi > 70:
                q -= 0.10 * (rsi - 70) / 30
            if not is_buy and rsi < 30:
                q -= 0.10 * (30 - rsi) / 30

        return max(0.0, min(1.0, q))


# Module-level singleton (mirrors the engine/agent pattern).
sniper_engine = SniperEntryEngine()
