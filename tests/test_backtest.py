"""Tests for the backtest + forward-test capability.

Covers the market-data loader (assembly, point auto-detection, dedupe,
sorting) and the backtest engine (fills, SL/TP/trail exits, commission,
metrics, forward-test train/test split).
"""
from __future__ import annotations

import gzip
import math
import os
import time

import pytest

from abutron.backtest import MarketDataLoader, BacktestEngine
from abutron.backtest.loader import _decimals, _epoch_to_iso
from abutron.feeds.feed_quality import Candle
from abutron.brain import make_brain, default_ensemble


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _synth(symbol="SYN.0", tf="M5", n=200, start=1.0, point=0.0001,
           trend=True):
    """Deterministic synthetic candles exercising up/down moves."""
    candles = []
    base = 1_600_000_000
    price = start
    for i in range(n):
        drift = math.sin(i / 20) * 0.0008 if trend else 0.0
        price += drift
        o = price
        h = o + abs(math.sin(i / 9)) * point * 30
        l = o - abs(math.cos(i / 11)) * point * 30
        c = o + math.sin(i / 7) * point * 20
        candles.append(Candle(
            symbol=symbol, timeframe=tf,
            open_time=_iso(base + i * 300),
            close_time=_iso(base + i * 300 + 300),
            open=o, high=h, low=l, close=c,
            point=point, tick_size=point, closed=True))
    return candles


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------
def _write_gz_csv(path, header, rows):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(",".join(header) + "\n")
        for r in rows:
            fh.write(",".join(str(x) for x in r) + "\n")


def test_decimals_helper():
    assert _decimals("0.70207") == 5
    assert _decimals("15267.167") == 3
    assert _decimals("15") == 0


def test_epoch_to_iso():
    assert _epoch_to_iso(0) == "1970-01-01T00:00:00Z"


def test_loader_fp_markets(tmp_path):
    d = tmp_path / "FP-Markets"
    d.mkdir()
    # candles with 5-decimal precision (point should auto-detect to 1e-5)
    _write_gz_csv(d / "candles.csv.gz",
                  ["symbol", "broker_symbol", "timeframe", "start_time_utc",
                   "open", "high", "low", "close", "broker_time_raw"],
                  [["AUDUSD", "AUDUSD.r", "H1", 1785448800,
                    "0.70207", "0.70445", "0.69915", "0.70041", 1785456000],
                   ["AUDUSD", "AUDUSD.r", "H1", 1785448800,
                    "0.70207", "0.70445", "0.69915", "0.70041", 1785456000],
                   ["AUDUSD", "AUDUSD.r", "H1", 1785452400,
                    "0.70041", "0.70110", "0.69980", "0.70090", 1785460000]])
    _write_gz_csv(d / "quotes.csv.gz",
                  ["symbol", "broker_symbol", "observed_utc", "bid", "ask",
                   "spread_price"],
                  [["AUDUSD", "AUDUSD.r", 1785448860, "0.70200", "0.70214",
                    "0.00014"]])
    loader = MarketDataLoader(str(tmp_path))
    candles, quotes = loader.load_fp_markets(symbol="AUDUSD.r",
                                             timeframe="H1")
    # duplicate row deduped -> 2 candles
    assert len(candles) == 2
    assert candles[0].symbol == "AUDUSD.r"
    assert candles[0].point == 1e-5
    # sorted by open_time
    assert candles[0].open_time <= candles[1].open_time
    assert len(quotes) == 1
    assert quotes[0].spread == pytest.approx(0.00014)


def test_loader_deriv_assembly(tmp_path):
    d = tmp_path / "Deriv"
    d.mkdir()
    # split a small csv into two parts to exercise assembly
    rows = [["Boom 1000 Index.0", "M5", 1793630400, "15267.167",
             "15267.167", "15262.552", "15262.552"]]
    rows.append(["Boom 1000 Index.0", "M5", 1793630700, "15262.537",
                 "15287.732", "15261.649", "15284.27"])
    for i, r in enumerate(rows):
        _write_gz_csv(d / f"candles.csv.gz.part-{i:02d}",
                      ["symbol", "timeframe", "start_time_utc", "open",
                       "high", "low", "close"], [r])
    loader = MarketDataLoader(str(tmp_path))
    candles = loader.load_deriv(symbol="Boom 1000 Index.0", timeframe="M5")
    assert len(candles) == 2
    assert candles[0].point == 1e-3  # 3 decimals


def test_discover_lists_symbols(tmp_path):
    d = tmp_path / "Deriv"
    d.mkdir()
    _write_gz_csv(d / "candles.csv.gz.part-00",
                  ["symbol", "timeframe", "start_time_utc", "open", "high",
                   "low", "close"],
                  [["Boom 1000 Index.0", "M5", 1793630400, "1.0", "1.1",
                    "0.9", "1.05"]])
    loader = MarketDataLoader(str(tmp_path))
    summary = loader.discover()
    assert ("Boom 1000 Index.0", "M5") in {
        (s["symbol"], s["timeframe"]) for s in summary["deriv"]}


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
def _engine(**kw):
    brain = make_brain(ensemble=default_ensemble(seed=0), size_cap=1.0)
    kw.setdefault("equity", 10000.0)
    kw.setdefault("risk_pct", 0.05)
    kw.setdefault("point", 0.0)
    kw.setdefault("point_value", 1.0)
    return BacktestEngine(brain, **kw)


def test_backtest_runs_and_metrics_consistent():
    candles = _synth(n=200)
    eng = _engine(warmup=20)
    r = eng.run(candles, [], symbol="SYN.0", timeframe="M5")
    assert r.mode == "backtest"
    assert r.trades == r.wins + r.losses
    assert 0.0 <= r.win_rate <= 100.0
    assert r.equity_end == pytest.approx(r.equity_start + r.total_pnl)
    # max drawdown is a non-negative percentage
    assert r.max_drawdown >= 0.0
    assert r.max_consecutive_losses >= 0


def test_forward_test_trains_and_splits():
    candles = _synth(n=300)
    eng = _engine(warmup=20)
    r = eng.run(candles, [], symbol="SYN.0", timeframe="M5",
                forward_test=True, forward_frac=0.3)
    assert r.mode == "forward_test"
    # only the held-out window is reported
    assert r.trades > 0
    assert eng._train_report is not None or eng._train_error == ""


def test_engine_empty_input():
    eng = _engine()
    r = eng.run([], [], symbol="X", timeframe="M5")
    assert r.mode == "empty"
    assert r.trades == 0


def test_engine_commission_reduces_pnl():
    candles = _synth(n=200)
    eng_no = _engine(warmup=20, commission_per_lot=0.0)
    eng_yes = _engine(warmup=20, commission_per_lot=5.0)
    r_no = eng_no.run(candles, [], symbol="SYN.0", timeframe="M5")
    r_yes = eng_yes.run(candles, [], symbol="SYN.0", timeframe="M5")
    # with identical signals, commission must not increase PnL
    assert r_yes.total_pnl <= r_no.total_pnl


def test_engine_fixed_vs_compound_sizing():
    candles = _synth(n=200)
    fixed = _engine(warmup=20, compound=False)
    comp = _engine(warmup=20, compound=True)
    rf = fixed.run(candles, [], symbol="SYN.0", timeframe="M5")
    rc = comp.run(candles, [], symbol="SYN.0", timeframe="M5")
    # both produce valid results; compounding can diverge but stays sane
    assert rf.trades == rc.trades
    assert rc.equity_end > 0


# --------------------------------------------------------------------------
# Train / save / load round-trip
# --------------------------------------------------------------------------
def test_train_save_load_roundtrip(tmp_path):
    from abutron.brain import make_brain, default_ensemble
    from abutron.brain.trainer import (prepare_samples, save_brain,
                                       load_brain, walk_forward_train)
    candles = _synth(n=120)
    samples = prepare_samples([c.close for c in candles],
                              [c.high for c in candles],
                              [c.low for c in candles], horizon=1)
    assert len(samples) > 50
    ens = default_ensemble(seed=0)
    rep = walk_forward_train(ens, samples, epochs=5, batch_size=16,
                             val_fraction=0.2, seed=0)
    assert rep.calibrated is True
    out = tmp_path / "brain"
    path = save_brain(str(out), ens, rep)
    assert os.path.exists(path)
    # load back and verify it predicts
    ens2 = load_brain(str(out))
    assert len(ens2.members) == len(ens.members)
    assert ens2._fitted is True
    brain = make_brain(ensemble=ens2, size_cap=1.0)
    assert brain.trained is True
    import numpy as np
    from abutron.brain.features import N_FEATURES
    pred = ens2.predict(np.zeros((1, N_FEATURES), dtype=float))
    assert pred.direction in ("BUY", "SELL", "HOLD")


def test_train_cli_runs(tmp_path, capsys):
    from abutron.backtest.train import main as train_main
    d = tmp_path / "Deriv"
    d.mkdir()
    # small deriv dataset
    import gzip
    rows = [["Boom 1000 Index.0", "M5", 1793630400 + i * 300,
             "1.0", "1.1", "0.9", "1.05"] for i in range(120)]
    with gzip.open(d / "candles.csv.gz.part-00", "wt") as fh:
        fh.write("symbol,timeframe,start_time_utc,open,high,low,close\n")
        for r in rows:
            fh.write(",".join(str(x) for x in r) + "\n")
    out = tmp_path / "brain_out"
    rc = train_main(["--data", str(tmp_path), "--broker", "deriv",
                     "--symbol", "Boom 1000 Index.0", "--timeframe", "M5",
                     "--epochs", "3", "--folds", "1",
                     "--out", str(out)])
    assert rc == 0
    assert os.path.exists(os.path.join(str(out), "nexus_brain.json"))


def test_mtf_confluence_filters_disagreeing_entries():
    """Only enter when the HTF direction agrees with the LTF trigger."""
    candles = _synth(n=200, tf="M5", point=0.001)
    htf = _synth(n=40, tf="H1", point=0.001)
    brain = make_brain(ensemble=default_ensemble(seed=0), size_cap=1.0)
    eng_on = BacktestEngine(brain, equity=10000, risk_pct=0.05, point=0.0,
                            point_value=1.0, warmup=20, mtf_confluence=True)
    eng_off = BacktestEngine(brain, equity=10000, risk_pct=0.05, point=0.0,
                             point_value=1.0, warmup=20, mtf_confluence=False)
    r_on = eng_on.run(candles, [], symbol="SYN.0", timeframe="M5",
                      htf_candles=htf)
    r_off = eng_off.run(candles, [], symbol="SYN.0", timeframe="M5",
                        htf_candles=htf)
    # confluence must not increase trades and must report filtered entries
    assert r_on.trades <= r_off.trades
    assert r_on.confluence_filtered >= 0
    assert r_on.htf == "H1"


def test_mtf_no_htf_trades_every_trigger():
    """Without HTF data, confluence is a no-op (trades every trigger)."""
    candles = _synth(n=200, tf="M5", point=0.001)
    brain = make_brain(ensemble=default_ensemble(seed=0), size_cap=1.0)
    eng = BacktestEngine(brain, equity=10000, risk_pct=0.05, point=0.0,
                         point_value=1.0, warmup=20, mtf_confluence=True)
    r = eng.run(candles, [], symbol="SYN.0", timeframe="M5")  # no htf
    assert r.htf == ""
    assert r.confluence_filtered == 0
    assert r.trades > 0
