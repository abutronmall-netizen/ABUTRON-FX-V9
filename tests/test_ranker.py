"""Tests for the daily profit ranker (trade the most profitable symbols)."""
from __future__ import annotations

import pytest

from abutron.apex.ranker import (
    DailyProfitRanker, expectancy_r, DEFAULT_TEST_WINDOW_DAYS)
from abutron.apex.eligibility import SYMBOL_REGISTRY, SymbolEligibility


def test_expectancy_r_formula():
    # PF > 1 with a win rate -> positive expectancy in R
    # expectancy_r = (1 - wr) * (pf - 1)
    assert expectancy_r(0.70, 2.0) == pytest.approx(0.30 * 1.0)
    assert expectancy_r(0.691, 2.81) == pytest.approx(0.309 * 1.81)
    # PF <= 1 -> no edge
    assert expectancy_r(0.70, 1.0) == 0.0
    assert expectancy_r(0.70, 0.98) == 0.0
    # unknown PF -> no edge
    assert expectancy_r(0.70, None) == 0.0


def test_ranker_ranks_profitable_symbols_first():
    eng = SymbolEligibility()
    ranker = DailyProfitRanker(eng)
    # at $1000 rank eligible symbols by expected daily profit
    ranks = ranker.rank(1000.0, risk_pct=0.01)
    assert ranks, "expected some eligible+profitable symbols at $1000"
    # sorted desc by profit
    profits = [r.expected_daily_profit for r in ranks]
    assert profits == sorted(profits, reverse=True)
    # known losers (PF <= 1) are excluded
    ranked_syms = {r.symbol for r in ranks}
    for s in ("Volatility 10 Index.0", "Boom 1000 Index.0"):
        prof = eng.profile(s)
        if prof is not None and prof.validation.test_pf <= 1.0:
            assert s not in ranked_syms
    # every ranked symbol is profitable and has positive expected profit
    assert all(r.profitable and r.expected_daily_profit > 0 for r in ranks)
    # EURUSD (highest expectancy * frequency among eligible) ranks first
    assert ranks[0].symbol == "EURUSD.r"


def test_ranker_excludes_unprofitable_at_low_equity():
    eng = SymbolEligibility()
    ranker = DailyProfitRanker(eng)
    # at $50 only FX + Vol10 are eligible; Vol10 is a known loser -> excluded
    ranks = ranker.rank(50.0, risk_pct=0.01)
    syms = {r.symbol for r in ranks}
    assert "Volatility 10 Index.0" not in syms
    assert "EURUSD.r" in syms


def test_ranker_keeps_unknown_symbols():
    eng = SymbolEligibility()
    ranker = DailyProfitRanker(eng)
    # an unregistered symbol has no measured edge but must NOT be dropped
    ranks = ranker.rank(1000.0, symbols=["EURUSD.r", "UNKNOWN.X"],
                        risk_pct=0.01)
    syms = {r.symbol for r in ranks}
    assert "UNKNOWN.X" in syms  # kept (cannot judge it a loser)
    # EURUSD (known edge) ranks above the unknown
    assert ranks[0].symbol == "EURUSD.r"


def test_ranker_select_top_n():
    eng = SymbolEligibility()
    ranker = DailyProfitRanker(eng)
    selected = ranker.select(1000.0, risk_pct=0.01, top_n=3)
    assert len(selected) == 3
    # the top-3 by profit; EURUSD (best expectancy * frequency) is first
    assert selected[0] == "EURUSD.r"


def test_ranker_expected_daily_profit_scales_with_equity():
    eng = SymbolEligibility()
    ranker = DailyProfitRanker(eng)
    d100, _, _, _, _ = ranker.expected_daily_profit("EURUSD.r", 100.0, 0.01)
    d1000, _, _, _, _ = ranker.expected_daily_profit("EURUSD.r", 1000.0, 0.01)
    # daily profit is linear in equity (risk_usd = equity * risk_pct)
    assert d1000 == pytest.approx(d100 * 10.0, rel=0.01)


def test_ranker_report_shape():
    eng = SymbolEligibility()
    ranker = DailyProfitRanker(eng)
    rep = ranker.report(1000.0, risk_pct=0.01)
    assert rep["equity"] == 1000.0
    assert rep["trade_set"]
    assert len(rep["ranked"]) == len(rep["trade_set"])
    # each rank carries the validation evidence
    top = rep["ranked"][0]
    assert "expectancy_r" in top and "profit_factor" in top
