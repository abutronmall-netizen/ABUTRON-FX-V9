"""Paper-trading simulator (kweinmeister simulator pattern, FX-adapted).

A self-contained, offline harness to validate strategies and risk logic
**before** anything is trusted on a DEMO account. It does not touch the
broker, the writer, or the order path — it replays a synthetic price stream
against a paper portfolio and a strategy callback, recording trades, P&L and
an equity curve.

Components:
  - `MarketSimulator` — a trend+volatility random walk generating a price
    stream (the reference simulator's `MarketDataSimulator`, FX-tuned).
  - `PaperPortfolio` — equity, open lots, unrealized/realized P&L.
  - `PaperTradingEngine` — drives a strategy callback over the stream and
    enforces the `RiskGuard` gate on every proposed trade.

This is a *pre-deployment* validation tool; DEMO/LIVE remain strictly
segregated and are never referenced here.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .riskguard import (PortfolioState, RiskGuard, TradeProposal)


class MarketSimulator:
    """Generates a stream of simulated FX prices (trend + volatility)."""

    def __init__(self, initial_price: float = 1.1000,
                 volatility: float = 0.0008,
                 trend: float = 0.0,
                 seed: Optional[int] = None,
                 history_size: int = 200):
        self.current_price = initial_price
        self.volatility = volatility
        self.trend = trend
        self._rng = random.Random(seed)
        self.history: List[float] = [initial_price]
        self._max = history_size

    def next_price(self) -> float:
        change = self._rng.normalvariate(self.trend, self.volatility)
        self.current_price *= 1.0 + change
        # Keep price in a plausible positive band.
        self.current_price = max(0.0001, self.current_price)
        self.history.append(self.current_price)
        if len(self.history) > self._max:
            self.history.pop(0)
        return self.current_price

    def get_history(self) -> List[float]:
        return list(self.history)


@dataclass
class PaperTrade:
    bar: int
    symbol: str
    direction: str
    volume_lots: float
    entry_price: float
    exit_price: Optional[float] = None
    pnl_usd: float = 0.0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class PaperPortfolio:
    """Margin-based FX paper portfolio: equity = base + realized + unrealized."""

    initial_equity: float = 100_000.0
    lots: float = 0.0
    entry_price: float = 0.0
    entry_bar: int = -1
    realized_pnl: float = 0.0
    last_price: float = 1.1000
    pip_size: float = 0.0001

    @property
    def unrealized_pnl(self) -> float:
        if self.lots <= 0 or self.entry_price <= 0:
            return 0.0
        # 1 lot = 100,000 units; P&L = lots * 100k * price_move (in price).
        return self.lots * 100_000.0 * (self.last_price - self.entry_price)

    @property
    def equity(self) -> float:
        return self.initial_equity + self.realized_pnl + self.unrealized_pnl

    def exposure_usd(self) -> float:
        return abs(self.lots) * self.last_price * 100_000.0

    def set_price(self, price: float, pip_size: float = 0.0001) -> None:
        self.last_price = price
        self.pip_size = pip_size

    def to_state(self) -> PortfolioState:
        return PortfolioState(
            equity=self.equity,
            risk_used_usd=abs(self.lots) * self.last_price * 100_000.0,
            exposure_by_symbol={"SIM": self.exposure_usd()})


@dataclass
class SimulationResult:
    bars: int
    final_equity: float
    return_pct: float
    trades: List[PaperTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    rejected: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"bars": self.bars, "final_equity": self.final_equity,
                "return_pct": self.return_pct,
                "trades": [t.to_dict() for t in self.trades],
                "equity_curve": self.equity_curve,
                "rejected": self.rejected}


# A strategy callback: given the simulator + current paper portfolio + bar
# index, return a TradeProposal (or None). The engine handles execution.
StrategyFn = Callable[[MarketSimulator, PaperPortfolio, int],
                      Optional[TradeProposal]]


class PaperTradingEngine:
    """Replay a strategy over a simulated price stream, risk-gated."""

    def __init__(self, guard: Optional[RiskGuard] = None,
                 initial_equity: float = 100_000.0,
                 pip_size: float = 0.0001):
        self.guard = guard or RiskGuard()
        self.initial_equity = initial_equity
        self.pip_size = pip_size

    def run(self, strategy: StrategyFn, bars: int,
            symbol: str = "SIM",
            initial_price: float = 1.1000,
            volatility: float = 0.0008,
            trend: float = 0.0,
            seed: Optional[int] = None) -> SimulationResult:
        sim = MarketSimulator(initial_price=initial_price,
                              volatility=volatility, trend=trend, seed=seed)
        portfolio = PaperPortfolio(initial_equity=self.initial_equity)
        portfolio.set_price(initial_price, self.pip_size)
        trades: List[PaperTrade] = []
        equity_curve: List[float] = []
        rejected = 0

        for i in range(bars):
            price = sim.next_price()
            portfolio.set_price(price, self.pip_size)

            # Manage an open position: exit after a holding horizon or on a
            # profit target, then allow a fresh entry.
            if portfolio.lots != 0:
                if self._should_exit(portfolio, i):
                    trades.append(self._close(portfolio, i, symbol, price))

            if portfolio.lots == 0 and strategy is not None:
                proposal = strategy(sim, portfolio, i)
                if proposal is not None:
                    proposal.symbol = proposal.symbol or symbol
                    check = self.guard.check_trade(proposal,
                                                   portfolio.to_state())
                    if check.approved:
                        trades.append(self._open(portfolio, i, proposal, price))
                    else:
                        rejected += 1
                        trades.append(PaperTrade(
                            bar=i, symbol=proposal.symbol,
                            direction=proposal.direction,
                            volume_lots=proposal.volume_lots,
                            entry_price=price, reason=check.reason))

            equity_curve.append(portfolio.equity)

        return SimulationResult(
            bars=bars, final_equity=portfolio.equity,
            return_pct=(portfolio.equity - self.initial_equity)
            / self.initial_equity * 100.0,
            trades=trades, equity_curve=equity_curve, rejected=rejected)

    def _should_exit(self, portfolio: PaperPortfolio, i: int) -> bool:
        # Simple, transparent exit rule: hold at most 20 bars, or take a
        # profit once unrealized P&L reaches a threshold. Strategies can
        # override by returning an opposite-direction proposal.
        if i - portfolio.entry_bar >= 20:
            return True
        if portfolio.unrealized_pnl >= 50.0:
            return True
        return False

    def _open(self, portfolio: PaperPortfolio, bar: int,
              proposal: TradeProposal, price: float) -> PaperTrade:
        portfolio.lots = proposal.volume_lots
        portfolio.entry_price = price
        portfolio.entry_bar = bar
        return PaperTrade(bar=bar, symbol=proposal.symbol,
                          direction=proposal.direction,
                          volume_lots=proposal.volume_lots,
                          entry_price=price, reason="OPENED")

    def _close(self, portfolio: PaperPortfolio, bar: int,
               symbol: str, price: float) -> PaperTrade:
        pnl = portfolio.unrealized_pnl
        portfolio.realized_pnl += pnl
        lots_closed = portfolio.lots
        portfolio.lots = 0.0
        portfolio.entry_price = 0.0
        portfolio.entry_bar = -1
        return PaperTrade(bar=bar, symbol=symbol, direction="CLOSE",
                          volume_lots=lots_closed, entry_price=price,
                          exit_price=price, pnl_usd=pnl, reason="CLOSED")
