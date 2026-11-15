"""Tests for the per-symbol sniper geometry + shared entry filters.

These cover the live-side wiring that backs the win-rate optimisation:
the refined per-symbol geometry and the conviction/volatility/trend/
momentum filters that the live APEX engine and the backtest engine both
use (via ``apply_entry_filters``).
"""
from __future__ import annotations

import pytest

from abutron.apex.geometry import (
    SymbolGeometry, GEOMETRY_BY_SYMBOL, DEFAULT_GEOMETRY,
    geometry_for, apply_entry_filters,
)
from abutron.brain.brain import BrainProposal


def _prop(direction, conviction=0.9):
    return BrainProposal(symbol="SYN.0", direction=direction,
                         conviction=conviction, confidence=conviction,
                         uncertainty=0.1)


def _rising(n=60, start=100.0, step=0.5):
    return [start + i * step for i in range(n)]


def _falling(n=60, start=100.0, step=0.5):
    return [start - i * step for i in range(n)]


# --- geometry resolution -------------------------------------------------
def test_geometry_for_refined_symbols():
    boom = geometry_for("Boom 1000 Index.0")
    assert boom.tp_atr_mult == 0.4
    assert boom.invalidation_atr_frac == 0.3
    assert boom.require_trend is True
    eurusd = geometry_for("EURUSD.r")
    assert eurusd.tp_atr_mult == 0.3
    assert eurusd.invalidation_atr_frac == 0.5
    assert eurusd.require_momentum is True


def test_geometry_for_unknown_symbol_uses_default():
    geom = geometry_for("SOMETHING.ELSE")
    assert geom is DEFAULT_GEOMETRY
    assert geom.tp_atr_mult == 0.5
    assert geom.invalidation_atr_frac == 0.3


def test_geometry_by_symbol_is_immutable_config():
    # all refined geometries use a tight TP (win-rate lever)
    for sym, g in GEOMETRY_BY_SYMBOL.items():
        assert g.tp_atr_mult <= 0.5, sym


# --- shared entry filters ------------------------------------------------
def test_filter_momentum_requires_agreement():
    geom = SymbolGeometry(require_momentum=True)
    rising, falling = _rising(), _falling()
    # BUY in uptrend allowed; SELL in uptrend rejected
    assert apply_entry_filters(_prop("BUY"), rising, rising, rising, 1.0,
                               geom)[0] is True
    ok, reason = apply_entry_filters(_prop("SELL"), rising, rising, rising,
                                     1.0, geom)
    assert ok is False and reason.startswith("RSI_NO_DOWN_MOM")
    # mirror
    assert apply_entry_filters(_prop("SELL"), falling, falling, falling,
                               1.0, geom)[0] is True
    ok2, reason2 = apply_entry_filters(_prop("BUY"), falling, falling,
                                       falling, 1.0, geom)
    assert ok2 is False and reason2.startswith("RSI_NO_UP_MOM")


def test_filter_conviction_floor():
    geom = SymbolGeometry(min_conviction=0.9)
    ok, reason = apply_entry_filters(_prop("BUY", 0.3), _rising(), _rising(),
                                     _rising(), 1.0, geom)
    assert ok is False and reason.startswith("LOW_CONVICTION")
    assert apply_entry_filters(_prop("BUY", 0.95), _rising(), _rising(),
                               _rising(), 1.0, geom)[0] is True


def test_filter_volatility_band():
    geom = SymbolGeometry(min_atr_pct=5.0)
    ok, reason = apply_entry_filters(_prop("BUY"), _rising(), _rising(),
                                     _rising(), 1.0, geom)
    assert ok is False and reason.startswith("LOW_VOL")
    geom2 = SymbolGeometry(max_atr_pct=0.5)
    ok2, reason2 = apply_entry_filters(_prop("BUY"), _rising(), _rising(),
                                       _rising(), 1.0, geom2)
    assert ok2 is False and reason2.startswith("HIGH_VOL")


def test_filter_no_filters_allows_everything():
    geom = SymbolGeometry()  # no filters on
    assert apply_entry_filters(_prop("BUY"), _rising(), _rising(), _rising(),
                               1.0, geom)[0] is True
    assert apply_entry_filters(_prop("SELL"), _rising(), _rising(), _rising(),
                               1.0, geom)[0] is True


def test_filter_point_in_time_no_lookahead():
    # idx=-1 (last bar) and an explicit idx in the middle must agree on
    # the window up to that bar (no future data leaks in).
    rising = _rising(80)
    geom = SymbolGeometry(require_momentum=True)
    ok_last, _ = apply_entry_filters(_prop("BUY"), rising, rising, rising,
                                     1.0, geom, idx=-1)
    ok_mid, _ = apply_entry_filters(_prop("BUY"), rising, rising, rising,
                                    1.0, geom, idx=79)
    assert ok_last == ok_mid
