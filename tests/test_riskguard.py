"""Tests for the hardened pre-trade RiskGuard + paper-trading simulator
(kweinmeister pattern, FX-adapted)."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.risk.riskguard import (PortfolioState, RiskGuard,
                                    RiskCheckResult, TradeProposal)
from abutron.risk.simulator import (MarketSimulator, PaperPortfolio,
                                    PaperTradingEngine, SimulationResult)


def _guard(**kw):
    return RiskGuard(**kw)


def _portfolio(equity=100_000.0, **kw):
    return PortfolioState(equity=equity, **kw)


def _proposal(**kw):
    base = dict(symbol="EURUSD", direction="BUY", volume_lots=1.0,
                price=1.1000, stop_loss=1.0990, take_profit=1.1020,
                stop_risk_usd=100.0)
    base.update(kw)
    return TradeProposal(**base)


# --------------------------------------------------------------------------
# RiskGuard
# --------------------------------------------------------------------------
def test_approves_normal_trade():
    rg = _guard()
    r = rg.check_trade(_proposal(), _portfolio())
    assert r.approved is True
    assert r.reason == "RISKGUARD_APPROVED"


def test_rejects_invalid_direction():
    r = _guard().check_trade(_proposal(direction="SIDEWAYS"), _portfolio())
    assert r.approved is False
    assert r.reason == "RISKGUARD_INVALID_DIRECTION"


def test_rejects_invalid_volume():
    r = _guard().check_trade(_proposal(volume_lots=0), _portfolio())
    assert r.approved is False
    assert r.reason == "RISKGUARD_INVALID_VOLUME"


def test_rejects_risk_cap():
    # equity 100k, risk cap 0.15% -> max 150 USD; propose 500 USD risk.
    r = _guard().check_trade(_proposal(stop_risk_usd=500.0), _portfolio())
    assert r.approved is False
    assert r.reason == "RISKGUARD_RISK_CAP"
    assert r.detail["max_risk_usd"] == pytest.approx(150.0)


def test_rejects_position_size():
    r = _guard(max_position_lots=2.0).check_trade(
        _proposal(volume_lots=10.0), _portfolio())
    assert r.approved is False
    assert r.reason == "RISKGUARD_POSITION_SIZE"


def test_rejects_concentration():
    # max concentration 10%; a large notional (20k lots @ 1.10 = 22k USD)
    # blows past it. Position-size limit raised so concentration is the gate.
    r = _guard(max_position_lots=100_000.0,
               max_concentration_pct=0.10).check_trade(
        _proposal(volume_lots=20_000.0, price=1.1000), _portfolio())
    assert r.approved is False
    assert r.reason == "RISKGUARD_CONCENTRATION"


def test_rejects_total_exposure():
    # Spread exposure across many symbols (each < 20% concentration) so the
    # per-symbol gate passes but gross exposure exceeds 100% of equity.
    many = {f"SYM{i}": 15_000.0 for i in range(7)}  # 105k total, 15% each
    existing = PortfolioState(equity=100_000.0, exposure_by_symbol=many)
    r = _guard().check_trade(_proposal(volume_lots=1.0, price=1.1000),
                             existing)
    assert r.approved is False
    assert r.reason == "RISKGUARD_TOTAL_EXPOSURE"


def test_rejects_invalid_equity():
    r = _guard().check_trade(_proposal(), _portfolio(equity=0.0))
    assert r.approved is False
    assert r.reason == "RISKGUARD_INVALID_EQUITY"


def test_sell_valid_direction():
    r = _guard().check_trade(_proposal(direction="SELL"), _portfolio())
    assert r.approved is True


# --------------------------------------------------------------------------
# Simulator
# --------------------------------------------------------------------------
def test_market_simulator_generates_prices():
    sim = MarketSimulator(initial_price=1.1, seed=1)
    prices = [sim.next_price() for _ in range(20)]
    assert len(prices) == 20
    assert all(p > 0 for p in prices)
    assert len(sim.get_history()) == 21


def test_paper_portfolio_equity_and_pnl():
    pf = PaperPortfolio(initial_equity=100_000.0)
    pf.set_price(1.1000)
    pf.lots = 1.0
    pf.entry_price = 1.0990
    pf.last_price = 1.1000
    # 1 lot * 100k * (1.1000 - 1.0990) = 100 USD unrealized.
    assert pf.unrealized_pnl == pytest.approx(100.0)
    assert pf.equity == pytest.approx(100_100.0)


def test_simulation_runs_and_returns_result():
    engine = PaperTradingEngine(initial_equity=100_000.0)

    def _strategy(sim, portfolio, i):
        hist = sim.get_history()
        if len(hist) < 5:
            return None
        mom = (hist[-1] - hist[-5]) / hist[-5]
        if mom > 0.0006:
            return TradeProposal(symbol="SIM", direction="BUY",
                                 volume_lots=1.0, price=sim.current_price,
                                 stop_loss=sim.current_price * 0.999,
                                 take_profit=sim.current_price * 1.002,
                                 stop_risk_usd=100.0)
        return None

    res = engine.run(_strategy, bars=60, seed=42)
    assert isinstance(res, SimulationResult)
    assert res.bars == 60
    assert len(res.equity_curve) == 60
    assert res.final_equity > 0
    # Every executed trade was risk-gated.
    for t in res.trades:
        if t.reason == "OPENED":
            assert t.direction in ("BUY", "SELL")


def test_simulation_respects_riskguard_rejection():
    # Tight risk cap -> the 100 USD risk proposal is rejected, trades record
    # the rejection reason, none are OPENED.
    engine = PaperTradingEngine(
        guard=RiskGuard(max_risk_per_trade_pct=0.0005),  # max 50 USD
        initial_equity=100_000.0)

    def _strategy(sim, portfolio, i):
        if i % 10 == 0:
            return TradeProposal(symbol="SIM", direction="BUY",
                                 volume_lots=1.0, price=sim.current_price,
                                 stop_loss=sim.current_price * 0.999,
                                 take_profit=sim.current_price * 1.002,
                                 stop_risk_usd=100.0)
        return None

    res = engine.run(_strategy, bars=30, seed=7)
    assert res.rejected > 0
    assert not any(t.reason == "OPENED" for t in res.trades)
