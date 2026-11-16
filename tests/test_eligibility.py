"""Tests for equity-aware symbol eligibility.

Covers the runtime gate that trades only the symbols the client equity
can carry, the validation-aware ranking (the "tests that have been
done"), the no-fail-closed indeterminate path, per-symbol trained-brain
loading, and the live + backtest engine wiring.
"""
from __future__ import annotations

import os

import pytest

from abutron.config import DATA_ROOT
from abutron.apex.eligibility import (
    SYMBOL_REGISTRY, SymbolEligibility, SymbolContract, SymbolValidation,
    SymbolProfile, slug_for, brain_dir_for, load_symbol_brain,
    default_brain_root)
from abutron.apex.geometry import geometry_for


# ---------------------------------------------------------------------------
# Slug / brain-dir mapping
# ---------------------------------------------------------------------------
def test_slug_for_maps_symbols_to_brain_dirs():
    assert slug_for("EURUSD.r") == "eurusd_r"
    assert slug_for("Boom 600 Index.0") == "boom_600_index_0"
    assert slug_for("US100") == "us100"
    assert slug_for("Volatility 10 Index.0") == "volatility_10_index_0"


def test_brain_dir_for_uses_m5_suffix():
    d = brain_dir_for("EURUSD.r", "/tmp/brains")
    assert d == os.path.join("/tmp/brains", "eurusd_r_m5")


# ---------------------------------------------------------------------------
# Min-equity thresholds match the computed contract model
# ---------------------------------------------------------------------------
def test_min_equity_matches_contract_model_at_15_percent():
    eng = SymbolEligibility()
    # min_equity = lot_min * (invalidation_atr_frac * rep_atr) * pv
    #             / (risk_pct * risk_scale), with pv=1.0, lot_min=0.01,
    # risk_pct=0.15, risk_scale=1.0.
    cases = {
        "EURUSD.r": 1.39,          # 0.01 * 0.8*26.0 * 1.0 / 0.15
        "GBPUSD.r": 1.04,
        "NZDUSD.r": 1.05,
        "US100": 104.23,           # 0.01 * 0.5*3126.79 / 0.15
        "US30": 97.21,
        "Boom 600 Index.0": 82.13,  # 0.01 * 0.3*4106.57 / 0.15
        "Crash 600 Index.0": 356.59,
        "Boom 1000 Index.0": 149.04,
        "Volatility 10 Index.0": 44.78,
    }
    for sym, expected in cases.items():
        got = eng.min_equity_for(sym, risk_pct=0.15)
        assert got == pytest.approx(expected, rel=0.02), (sym, got, expected)


def test_min_equity_scales_with_risk_pct():
    eng = SymbolEligibility()
    # Lower risk_pct -> higher min_equity (smaller risk budget per trade).
    m15 = eng.min_equity_for("Boom 600 Index.0", risk_pct=0.15)
    m25 = eng.min_equity_for("Boom 600 Index.0", risk_pct=0.25)
    assert m25 == pytest.approx(m15 * 0.15 / 0.25, rel=0.01)
    assert m25 < m15


def test_min_equity_uses_runtime_atr_when_supplied():
    eng = SymbolEligibility()
    # Double the ATR -> double the min_equity (linear in ATR).
    base = eng.min_equity_for("US100", risk_pct=0.15)
    doubled = eng.min_equity_for("US100", risk_pct=0.15,
                                 atr_points=2 * SYMBOL_REGISTRY["US100"]
                                 .representative_atr_points)
    assert doubled == pytest.approx(base * 2.0, rel=0.01)


# ---------------------------------------------------------------------------
# Tradability at equity levels
# ---------------------------------------------------------------------------
def test_at_40_equity_only_fx_trades_at_15_percent():
    eng = SymbolEligibility()
    tradable = {r.symbol for r in eng.tradable_symbols(40.0, risk_pct=0.15)}
    # At $40 / 15%, FX thresholds (~$1) are cleared; the index/synthetic
    # thresholds ($44.78–$356.59) are not.
    assert tradable == {"EURUSD.r", "GBPUSD.r", "NZDUSD.r"}
    assert "US100" not in tradable
    assert "US30" not in tradable
    assert "Boom 600 Index.0" not in tradable
    assert "Crash 600 Index.0" not in tradable
    assert "Volatility 10 Index.0" not in tradable
    assert "Boom 1000 Index.0" not in tradable


def test_at_1000_equity_all_symbols_trade():
    eng = SymbolEligibility()
    tradable = {r.symbol for r in eng.tradable_symbols(1000.0, risk_pct=0.15)}
    assert tradable == set(SYMBOL_REGISTRY)


def test_is_tradable_reason_codes():
    eng = SymbolEligibility()
    ok = eng.is_tradable("EURUSD.r", 100.0, risk_pct=0.15)
    assert ok.eligible and ok.reason == "EQUITY_OK"
    bad = eng.is_tradable("US100", 50.0, risk_pct=0.15)
    assert not bad.eligible and bad.reason == "EQUITY_BELOW_MIN_LOT"
    assert bad.min_equity > 50.0


def test_unregistered_symbol_without_scale_is_not_fail_closed():
    eng = SymbolEligibility()
    # No registry entry, no ATR, no point -> threshold unknown -> do NOT
    # block (no-fail-closed). size_position is the final guard.
    r = eng.is_tradable("UNKNOWN.X", 10.0, risk_pct=0.15)
    assert r.eligible
    assert r.reason == "THRESHOLD_UNKNOWN"


# ---------------------------------------------------------------------------
# Validation-aware ranking (the tests that have been done)
# ---------------------------------------------------------------------------
def test_tradable_symbols_rank_validated_first():
    eng = SymbolEligibility()
    ranked = eng.tradable_symbols(1000.0, risk_pct=0.15)
    # The four validated symbols must occupy the top ranks.
    top4 = {r.symbol for r in ranked[:4]}
    assert top4 == {"Boom 600 Index.0", "Crash 600 Index.0",
                    "EURUSD.r", "NZDUSD.r"}
    # Boom 600 has the strongest PF -> ranks first.
    assert ranked[0].symbol == "Boom 600 Index.0"
    # validated flag is set on the results.
    assert all(r.validated for r in ranked[:4])


def test_registry_validation_flags():
    # Symbols with inverted / tiny-sample evidence are not validated.
    assert SYMBOL_REGISTRY["Boom 600 Index.0"].validation.validated
    assert SYMBOL_REGISTRY["EURUSD.r"].validation.validated
    assert not SYMBOL_REGISTRY["Volatility 10 Index.0"].validation.validated
    assert not SYMBOL_REGISTRY["Boom 1000 Index.0"].validation.validated
    assert not SYMBOL_REGISTRY["US100"].validation.validated  # tiny sample


def test_report_shape():
    eng = SymbolEligibility()
    rep = eng.report(100.0)
    assert rep["equity"] == 100.0
    assert "tradable" in rep and "rejected" in rep
    assert rep["tradable_symbols"]  # FX at $100
    assert any(r["symbol"] == "US100" for r in rep["rejected"])


# ---------------------------------------------------------------------------
# Per-symbol trained-brain loading
# ---------------------------------------------------------------------------
def test_load_symbol_brain_loads_trained_ensemble():
    brain = load_symbol_brain("EURUSD.r", brain_root=default_brain_root())
    assert brain is not None
    assert brain.trained
    # the loaded ensemble is the trained one (has members)
    assert len(brain.ensemble.members) > 0


def test_load_symbol_brain_returns_none_for_unknown():
    brain = load_symbol_brain("NOPE.notreal",
                              brain_root=default_brain_root())
    assert brain is None


# ---------------------------------------------------------------------------
# Live engine wiring — trades only eligible symbols
# ---------------------------------------------------------------------------
def _clear_candidates():
    from abutron import db as dbmod
    from sqlalchemy import delete
    from abutron.db import CandidateRow
    dbmod.engine_db.session.execute(delete(CandidateRow))
    dbmod.engine_db.session.commit()


def _mk_candles(symbol, point, n=200, start=1.25, hl_points=30):
    from abutron.feeds.feed_quality import Candle
    import math
    candles = []
    base = 1_600_000_000
    price = start
    # hl_points = high-low amplitude in POINTS -> ATR in points ~ hl_points,
    # so the eligibility threshold is controllable (matches real contracts
    # when hl_points is set to the instrument's median ATR in points).
    amp = hl_points * point
    for i in range(n):
        drift = math.sin(i / 20) * 0.0008 * max(start, 1.0)
        price += drift
        o = price
        h = o + abs(math.sin(i / 9)) * amp
        l = o - abs(math.cos(i / 11)) * amp
        c = o + math.sin(i / 7) * amp * 0.7
        candles.append(Candle(
            symbol=symbol, timeframe="M5",
            open_time=__import__("time").strftime(
                "%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime(base + i * 300)),
            close_time=__import__("time").strftime(
                "%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime(base + i * 300 + 300)),
            open=o, high=h, low=l, close=c,
            point=point, tick_size=point, closed=True))
    return candles


def test_live_engine_skips_ineligible_symbol_at_low_equity():
    from abutron.events import EventLog
    from abutron.apex.engine import ApexEngine
    from abutron.feeds.feed_quality import Quote

    _clear_candidates()
    log = EventLog(os.path.join(DATA_ROOT, "t_elig_engine.jsonl"))
    engine = ApexEngine(log)
    # US100 with realistic median ATR (~3100 pts) needs ~$103 at 15%;
    # $50 is too small -> must be skipped by the eligibility gate.
    candles = _mk_candles("US100", 0.01, start=27000.0, hl_points=3100)
    for c in candles:
        engine.set_history(c.symbol, c.timeframe, candles)
    quote = Quote(symbol="US100", bid=candles[-1].close,
                  ask=candles[-1].close * 1.0001, spread=2.0,
                  tick_size=0.01, point=0.01, point_value=1.0,
                  volume_min=0.01, volume_max=50.0)
    account = {"connection_id": "elig_conn", "broker": "fpmarkets",
               "owner_id": "owner1", "effective_on": True, "desired_on": True,
               "equity": 50.0}
    result = engine.run_decision_pass(
        connection_id="elig_conn", broker="fpmarkets", owner_id="owner1",
        candles=candles[-1:], quotes=[quote], account=account)
    assert result == []  # no candidate — eligibility rejected US100


def test_live_engine_trades_eligible_fx_at_low_equity():
    from abutron.events import EventLog
    from abutron.apex.engine import ApexEngine
    from abutron.feeds.feed_quality import Quote

    _clear_candidates()
    log = EventLog(os.path.join(DATA_ROOT, "t_elig_fx.jsonl"))
    engine = ApexEngine(log)
    # EURUSD.r threshold ~$1.4 at 15%; $50 is plenty -> trades.
    candles = _mk_candles("EURUSD.r", 1e-5, start=1.10)
    for c in candles:
        engine.set_history(c.symbol, c.timeframe, candles)
    quote = Quote(symbol="EURUSD.r", bid=candles[-1].close,
                  ask=candles[-1].close * 1.0001, spread=0.2,
                  tick_size=1e-5, point=1e-5, point_value=1.0,
                  volume_min=0.01, volume_max=50.0)
    account = {"connection_id": "elig_fx_conn", "broker": "fpmarkets",
               "owner_id": "owner1", "effective_on": True, "desired_on": True,
               "equity": 50.0}
    result = engine.run_decision_pass(
        connection_id="elig_fx_conn", broker="fpmarkets", owner_id="owner1",
        candles=candles[-1:], quotes=[quote], account=account)
    # EURUSD.r is eligible; a candidate is produced (no entry filters on it).
    assert result, "expected a candidate for eligible EURUSD.r at $50"
    assert result[0].symbol == "EURUSD.r"


# ---------------------------------------------------------------------------
# Backtest wiring — equity_filtered counter
# ---------------------------------------------------------------------------
def test_backtest_equity_filtered_counter():
    from abutron.backtest.engine import BacktestEngine
    from abutron.brain import make_brain, default_ensemble

    brain = make_brain(ensemble=default_ensemble(seed=0), size_cap=1.0)
    # US100 with a realistic median ATR (~3100 points) -> min-equity ~$103
    # at 15%; $50 equity is below it -> every entry is filtered.
    candles = _mk_candles("US100", 0.01, start=27000.0, hl_points=3100)
    eng = BacktestEngine(brain, equity=50.0, risk_pct=0.15, point=0.01,
                         point_value=1.0, lot_min=0.01, warmup=20,
                         mtf_confluence=False)
    r = eng.run(candles, [], symbol="US100", timeframe="M5")
    assert r.equity_filtered > 0
    assert r.trades == 0


def test_backtest_trades_eligible_symbol():
    from abutron.backtest.engine import BacktestEngine
    from abutron.brain import make_brain, default_ensemble

    brain = make_brain(ensemble=default_ensemble(seed=0), size_cap=1.0)
    # EURUSD.r threshold ~$1.4 at 15%; at $10000 it is eligible and trades
    # occur. (equity_filtered may become > 0 only if the run's equity later
    # drops below the tiny threshold — the gate is equity-aware by design.)
    candles = _mk_candles("EURUSD.r", 1e-5, start=1.10)
    eng = BacktestEngine(brain, equity=10000.0, risk_pct=0.15, point=1e-5,
                         point_value=1.0, lot_min=0.01, warmup=20,
                         mtf_confluence=False)
    r = eng.run(candles, [], symbol="EURUSD.r", timeframe="M5")
    # EURUSD.r is eligible at $10000 -> trades occur (the gate does not
    # block an eligible symbol; equity_filtered only rises if equity later
    # drops below the tiny ~$1.4 threshold, which is the gate working).
    assert r.trades > 0
