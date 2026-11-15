"""Equity-tiered compounding + daily-target risk policy.

This serves the small-account growth path the platform is designed for:
start at **$50**, compound through milestones ($50 -> $100 -> $500 ->
...), auto-scale lot size as equity grows, and apply a daily profit
target + loss cap so a $50 starter grows sustainably instead of
over-risking to chase a number.

**Honest design (critical):** the daily target is a *profit-stop* — the
engine stops trading for the day once the target is reached. The loss
cap is a *hard stop*. **Neither ever increases per-trade risk to chase
the target**; risk is always bounded by the equity tier. A fixed
``$1000/day`` target on ``$50`` equity is not reachable at sane risk;
the policy simply does not force it (it trades at tier risk all day and
grows slowly) rather than over-risking into a blowup. ``$1000/day``
becomes reachable only once equity is large enough that the strategy's
expectancy supports it (see ``equity_for_daily_target``).

Three pieces:

* ``EquityTier`` / ``DEFAULT_TIERS`` — compounding milestones. Each tier
  sets the per-trade ``risk_pct`` and ``lot_max``; as equity crosses a
  milestone, risk and lot scale up automatically. The $50 -> $100 step
  is the first tier transition.
* ``RiskPolicy`` — resolves the tier for a given equity, applies the
  daily profit-stop / loss-cap, and reports realistic expectations.
* ``equity_for_daily_target`` — the equity at which a given daily target
  is actually reachable at sane risk (used for honest goal-setting).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class EquityTier:
    """One compounding milestone.

    ``min_equity``: equity at/above which this tier applies.
    ``risk_pct``:  per-trade risk fraction at this tier.
    ``lot_max``:   maximum lot size at this tier (auto lot scaling).
    ``label``:     human-readable milestone name.
    """
    min_equity: float
    risk_pct: float
    lot_max: float = 50.0
    label: str = ""


# Default compounding ladder for the $50 -> $100 -> ... growth path.
# Risk steps up modestly as equity grows; lot_max scales with it so the
# position size grows with the account rather than staying fixed.
DEFAULT_TIERS: Tuple[EquityTier, ...] = (
    EquityTier(0.0,     0.04, 0.5,  "$50 starter"),
    EquityTier(100.0,   0.05, 1.0,  "$100"),
    EquityTier(500.0,   0.07, 2.0,  "$500"),
    EquityTier(1000.0,  0.08, 5.0,  "$1k"),
    EquityTier(5000.0,  0.10, 10.0, "$5k"),
    EquityTier(10000.0, 0.12, 25.0, "$10k"),
    EquityTier(25000.0, 0.15, 50.0, "$25k+"),
)


def tier_for_equity(equity: float,
                    tiers: Tuple[EquityTier, ...] = DEFAULT_TIERS
                    ) -> EquityTier:
    """Return the tier whose ``min_equity`` is the largest value <= equity."""
    chosen = tiers[0]
    for cand in tiers:
        if equity >= cand.min_equity:
            chosen = cand
    return chosen


@dataclass
class RiskPolicy:
    """Equity-tiered compounding + daily target/loss-cap policy.

    The policy is the single source of truth for how much to risk and
    when to stop for the day. It is intentionally conservative: the
    daily target is a *stop*, never a reason to raise risk.
    """
    tiers: Tuple[EquityTier, ...] = DEFAULT_TIERS
    target_pnl: Optional[float] = None   # daily profit-stop (None = off)
    daily_loss_pct: Optional[float] = None  # daily loss cap = equity * this

    # -- tier resolution ------------------------------------------------
    def risk_pct_for(self, equity: float) -> float:
        return tier_for_equity(equity, self.tiers).risk_pct

    def lot_max_for(self, equity: float) -> float:
        return tier_for_equity(equity, self.tiers).lot_max

    def tier_label(self, equity: float) -> str:
        return tier_for_equity(equity, self.tiers).label

    def loss_cap_for(self, equity: float) -> float:
        if self.daily_loss_pct is None:
            return float("inf")
        return equity * self.daily_loss_pct

    # -- daily stops ---------------------------------------------------
    def allow_entry(self, equity: float, daily_pnl: float
                    ) -> Tuple[bool, str]:
        """Return (allowed, reason). False => stop trading for the day.

        * ``daily_pnl >= target_pnl``  -> profit-stop (target reached).
        * ``daily_pnl <= -loss_cap``   -> hard stop (loss cap hit).

        Both are opt-in (``None`` disables). Risk is never raised to
        chase the target; these are pure stops.
        """
        if self.target_pnl is not None and daily_pnl >= self.target_pnl:
            return False, "DAILY_TARGET_REACHED"
        if daily_pnl <= -self.loss_cap_for(equity):
            return False, "DAILY_LOSS_CAP"
        return True, "OK"

    # -- honest expectations ------------------------------------------
    def realistic_daily_pnl(self, equity: float, *, trades_per_day: int = 30,
                            win_rate: float = 0.70,
                            avg_win_rr: float = 0.8,
                            avg_loss_rr: float = 1.0) -> float:
        """Estimated net daily PnL at this equity under the sniper edge.

        Expectancy per trade in R = win_rate*avg_win_rr - (1-win_rate)*
        avg_loss_rr. Daily PnL = R_expectancy * trades * risk_usd.
        This is an estimate for honest goal-setting, not a promise.
        """
        rr = win_rate * avg_win_rr - (1.0 - win_rate) * avg_loss_rr
        risk_usd = equity * self.risk_pct_for(equity)
        return rr * trades_per_day * risk_usd


def equity_for_daily_target(
    target_pnl: float, *,
    trades_per_day: int = 30,
    win_rate: float = 0.70,
    avg_win_rr: float = 0.8,
    avg_loss_rr: float = 1.0,
    tiers: Tuple[EquityTier, ...] = DEFAULT_TIERS,
) -> float:
    """Equity at which ``target_pnl``/day is reachable at sane risk.

    Solves expectancy = target for equity within each tier's range.
    Returns ``inf`` if the target is unreachable within the ladder.
    """
    rr = win_rate * avg_win_rr - (1.0 - win_rate) * avg_loss_rr
    if rr <= 0:
        return float("inf")
    # sorted ascending; last tier is the ceiling
    ordered = sorted(tiers, key=lambda t: t.min_equity)
    for i, t in enumerate(ordered):
        risk_pct = t.risk_pct
        # equity needed so that rr * trades * risk_pct * equity == target
        need = target_pnl / (rr * trades_per_day * risk_pct)
        # this tier applies for equity in [t.min_equity, next.min_equity)
        upper = ordered[i + 1].min_equity if i + 1 < len(ordered) else float("inf")
        if t.min_equity <= need < upper:
            return round(need, 2)
    return float("inf")
