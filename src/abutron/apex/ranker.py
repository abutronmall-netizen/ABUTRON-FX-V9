"""Daily profit ranker — trade the symbols that give the most profit daily.

The directive: *"trade the symbols which give the most profit daily."*
Combined with the equity-aware eligibility gate, this selects, for a given
client equity, the instruments that are both **tradable** (the equity can
carry the minimum lot) and **profitable** (positive expectancy), ranked
by their expected daily profit.

**Expectancy from validated stats.** For a sniper trade the loss is the
tight invalidation (normalised to 1R), so profit factor and win rate
determine the per-trade expectancy in R:

    expectancy_r = (1 - win_rate) * (profit_factor - 1)

(derived from PF = win_rate * avg_win_rr / ((1 - win_rate) * 1R) with
avg_loss = 1R). PF <= 1 -> expectancy <= 0 -> not worth trading.

**Daily profit.** expected_daily_profit = expectancy_r * risk_usd *
trades_per_day, where risk_usd = equity * risk_pct (the per-trade risk
budget, identical for every eligible symbol at a given equity) and
trades_per_day is the symbol's observed forward-test trade frequency
(test_trades / test_window_days). Ranking by this directly answers
"which symbols give the most profit today."

The ranker is equity-aware (only eligible symbols are considered) and
extreme-low-drawdown aware: unprofitable symbols (PF <= 1) are dropped
first, so the engine never trades a known loser just because it is
tradable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .eligibility import SYMBOL_REGISTRY, SymbolEligibility, SymbolProfile


# Default forward-test window (days) the validation trade counts cover.
DEFAULT_TEST_WINDOW_DAYS = 7
# Fallback trades/day when a symbol has no measured trade count.
DEFAULT_TRADES_PER_DAY = 20.0


@dataclass
class ProfitRank:
    symbol: str
    expected_daily_profit: float
    expectancy_r: float
    risk_usd: float
    trades_per_day: float
    win_rate: float
    profit_factor: Optional[float]
    validated: bool
    min_equity: float
    eligible: bool
    profitable: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "expected_daily_profit": round(self.expected_daily_profit, 4),
            "expectancy_r": round(self.expectancy_r, 4),
            "risk_usd": round(self.risk_usd, 4),
            "trades_per_day": round(self.trades_per_day, 3),
            "win_rate": round(self.win_rate, 4),
            "profit_factor": (round(self.profit_factor, 3)
                              if self.profit_factor is not None else None),
            "validated": self.validated,
            "min_equity": round(self.min_equity, 4),
            "eligible": self.eligible,
            "profitable": self.profitable,
        }


def expectancy_r(win_rate: float, profit_factor: Optional[float]) -> float:
    """Per-trade expectancy in R from win rate + profit factor.

    loss normalised to 1R (sniper tight invalidation). Returns <= 0 when
    the symbol has no edge (PF <= 1 or unknown PF).
    """
    if profit_factor is None or profit_factor <= 1.0:
        return 0.0
    wr = max(0.0, min(1.0, win_rate))
    return (1.0 - wr) * (profit_factor - 1.0)


class DailyProfitRanker:
    """Rank eligible symbols by expected daily profit."""

    def __init__(
        self,
        eligibility: Optional[SymbolEligibility] = None,
        *,
        test_window_days: float = DEFAULT_TEST_WINDOW_DAYS,
        trades_per_day_default: float = DEFAULT_TRADES_PER_DAY,
    ):
        self.eligibility = eligibility or SymbolEligibility()
        self.test_window_days = test_window_days
        self.trades_per_day_default = trades_per_day_default

    # -- per-symbol estimate -------------------------------------------
    def trades_per_day(self, profile: Optional[SymbolProfile]) -> float:
        if profile is not None and profile.validation.test_trades > 0:
            return profile.validation.test_trades / self.test_window_days
        return self.trades_per_day_default

    def expected_daily_profit(self, symbol: str, equity: float,
                              risk_pct: float) -> Tuple[float, float, float,
                                                        float, Optional[float]]:
        """Return (daily_profit, expectancy_r, risk_usd, trades_per_day, pf)."""
        prof = self.eligibility.profile(symbol)
        if prof is None:
            # No validation data -> no measurable edge -> zero profit estimate.
            return 0.0, 0.0, equity * risk_pct, self.trades_per_day_default, None
        v = prof.validation
        pf = v.test_pf
        er = expectancy_r(v.test_win_rate, pf)
        risk_usd = equity * risk_pct
        tpd = self.trades_per_day(prof)
        daily = er * risk_usd * tpd
        return daily, er, risk_usd, tpd, pf

    # -- ranking -------------------------------------------------------
    def rank(
        self,
        equity: float,
        symbols: Optional[List[str]] = None,
        *,
        risk_pct: Optional[float] = None,
        exclude_unprofitable: bool = True,
        top_n: Optional[int] = None,
    ) -> List[ProfitRank]:
        """Rank eligible symbols by expected daily profit (desc).

        ``exclude_unprofitable`` drops symbols with PF <= 1 (no edge) so the
        engine never trades a known loser. If that would leave nothing
        tradeable, falls back to all eligible symbols.
        """
        risk_pct = risk_pct if risk_pct is not None else \
            self.eligibility.default_risk_pct
        symbols = symbols if symbols is not None else list(SYMBOL_REGISTRY)
        ranks: List[ProfitRank] = []
        for s in symbols:
            elig = self.eligibility.is_tradable(s, equity, risk_pct=risk_pct)
            if not elig.eligible:
                continue
            prof = self.eligibility.profile(s)
            v = prof.validation if prof else None
            pf = v.test_pf if v else None
            er = expectancy_r(v.test_win_rate, pf) if v else 0.0
            profitable = (pf is not None and pf > 1.0)
            # Exclude only KNOWN losers (registered with a measured PF <= 1).
            # Unknown / unmeasurable-PF symbols are kept (ranked last) so the
            # engine never drops a symbol it cannot actually judge.
            known_loser = (prof is not None and pf is not None and pf <= 1.0)
            if exclude_unprofitable and known_loser:
                continue
            daily, _, risk_usd, tpd, _ = self.expected_daily_profit(
                s, equity, risk_pct)
            ranks.append(ProfitRank(
                symbol=s, expected_daily_profit=daily, expectancy_r=er,
                risk_usd=risk_usd, trades_per_day=tpd,
                win_rate=(v.test_win_rate if v else 0.0),
                profit_factor=pf,
                validated=bool(v and v.validated),
                min_equity=elig.min_equity, eligible=True,
                profitable=profitable))
        # fall back to all eligible if excluding losers left nothing
        if not ranks:
            for s in symbols:
                elig = self.eligibility.is_tradable(s, equity,
                                                    risk_pct=risk_pct)
                if not elig.eligible:
                    continue
                prof = self.eligibility.profile(s)
                v = prof.validation if prof else None
                pf = v.test_pf if v else None
                er = expectancy_r(v.test_win_rate, pf) if v else 0.0
                daily, _, risk_usd, tpd, _ = self.expected_daily_profit(
                    s, equity, risk_pct)
                ranks.append(ProfitRank(
                    symbol=s, expected_daily_profit=daily, expectancy_r=er,
                    risk_usd=risk_usd, trades_per_day=tpd,
                    win_rate=(v.test_win_rate if v else 0.0),
                    profit_factor=pf,
                    validated=bool(v and v.validated),
                    min_equity=elig.min_equity, eligible=True,
                    profitable=False))
        ranks.sort(key=lambda r: r.expected_daily_profit, reverse=True)
        if top_n is not None:
            ranks = ranks[:top_n]
        return ranks

    def select(
        self,
        equity: float,
        symbols: Optional[List[str]] = None,
        *,
        risk_pct: Optional[float] = None,
        top_n: Optional[int] = None,
    ) -> List[str]:
        """Return the symbols to trade today (most profit first)."""
        return [r.symbol for r in self.rank(
            equity, symbols, risk_pct=risk_pct, top_n=top_n)]

    def report(
        self,
        equity: float,
        symbols: Optional[List[str]] = None,
        *,
        risk_pct: Optional[float] = None,
        top_n: Optional[int] = None,
    ) -> Dict[str, Any]:
        ranks = self.rank(equity, symbols, risk_pct=risk_pct, top_n=top_n)
        return {
            "equity": equity,
            "risk_pct": risk_pct if risk_pct is not None else
            self.eligibility.default_risk_pct,
            "trade_set": [r.symbol for r in ranks],
            "ranked": [r.to_dict() for r in ranks],
        }
