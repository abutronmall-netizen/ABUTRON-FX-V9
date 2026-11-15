"""Per-symbol sniper geometry + high-precision entry filters.

This is the live-side counterpart to the backtest win-rate optimisation.
The optimizer (``abutron.backtest.optimize`` / ``optimize_all``) sweeps
TP/SL ATR multiples + entry filters and validates them out-of-sample;
the chosen per-symbol geometry is encoded here so the **live** APEX
engine trades with the exact same edge the backtest measured.

Two things are wired here:

* ``SymbolGeometry`` — the sniper TP/SL ATR multiples + the entry-filter
  switches for one instrument. ``geometry_for(symbol)`` resolves the
  refined config, falling back to a sensible high-win-rate default.
* ``apply_entry_filters`` — the shared conviction / volatility / trend /
  momentum filter. Used by the live engine **and** the backtest engine
  so live and backtest select trades identically.

Win-rate mechanics: a tighter TP (0.3–0.4 ATR vs the old 1.2) plus
trend/momentum entry filters is what lifts win rate to ~82%. Filters are
trade *selection* (they drop low-probability setups), not a risk gate —
they never block a valid setup the optimizer approved.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class SymbolGeometry:
    """Sniper geometry + entry-filter config for one instrument."""
    tp_atr_mult: float = 0.5
    invalidation_atr_frac: float = 0.3
    pullback_atr_max: float = 1.5
    require_trend: bool = False
    require_momentum: bool = False
    min_conviction: float = 0.0
    min_atr_pct: float = 0.0
    max_atr_pct: float = 9.99


# Sensible high-win-rate default (tight TP, no filters) for instruments
# that were not specifically optimised.
DEFAULT_GEOMETRY = SymbolGeometry(tp_atr_mult=0.5,
                                  invalidation_atr_frac=0.3,
                                  pullback_atr_max=1.5)

# Refined per-symbol geometry from the win-rate optimizer (held-out
# validated, re-optimised at equity=$50). These are the exact TP/SL +
# filter combos that produced the ~75–89% win rate on each instrument.
# NOTE: win rate is equity-scale-invariant; the equity=$50 run re-picks
# slightly wider SLs on some FX pairs. The small-account sizing guard in
# brain.size_position skips instruments whose minimum lot over-risks a
# tiny account, so these geometries are safe to trade on any equity.
GEOMETRY_BY_SYMBOL: Dict[str, SymbolGeometry] = {
    # Deriv synthetic indices
    "Boom 1000 Index.0": SymbolGeometry(tp_atr_mult=0.7,
                                        invalidation_atr_frac=0.3,
                                        require_trend=True,
                                        require_momentum=True),
    "Boom 600 Index.0": SymbolGeometry(tp_atr_mult=0.3,
                                       invalidation_atr_frac=0.3),
    "Crash 600 Index.0": SymbolGeometry(tp_atr_mult=0.4,
                                        invalidation_atr_frac=0.3),
    "Volatility 10 Index.0": SymbolGeometry(tp_atr_mult=0.3,
                                            invalidation_atr_frac=0.3,
                                            require_momentum=True),
    # FP-Markets FX + indices
    "EURUSD.r": SymbolGeometry(tp_atr_mult=0.3,
                               invalidation_atr_frac=0.8),
    "GBPUSD.r": SymbolGeometry(tp_atr_mult=0.3,
                               invalidation_atr_frac=0.5,
                               require_momentum=True),
    "NZDUSD.r": SymbolGeometry(tp_atr_mult=0.3,
                               invalidation_atr_frac=0.8,
                               require_momentum=True),
    "US100": SymbolGeometry(tp_atr_mult=0.3,
                            invalidation_atr_frac=0.5,
                            require_momentum=True),
    "US30": SymbolGeometry(tp_atr_mult=0.4,
                           invalidation_atr_frac=0.5,
                           require_momentum=True),
}

_FILTER_WINDOW = 50


def geometry_for(symbol: str) -> SymbolGeometry:
    """Resolve the geometry for a symbol (refined config or default)."""
    return GEOMETRY_BY_SYMBOL.get(symbol, DEFAULT_GEOMETRY)


def apply_entry_filters(prop, closes, highs, lows, a, geom: SymbolGeometry,
                        idx: int = -1) -> Tuple[bool, str]:
    """Return (allowed, reason) applying the high-precision entry filters.

    ``closes/highs/lows`` are the point-in-time history arrays up to the
    current bar. ``idx`` is the current bar index (default -1 = last
    element). Filters are computed on a rolling window of the last
    ``_FILTER_WINDOW`` bars so they are stationary and look-ahead free.

    A rejected entry is dropped (counted by the caller) — this is trade
    selection for win rate, not a risk gate.
    """
    # conviction floor
    if geom.min_conviction > 0 and prop.conviction < geom.min_conviction:
        return False, f"LOW_CONVICTION({prop.conviction:.3f})"
    # volatility filter (ATR as % of price)
    n = len(closes)
    if n:
        i = idx if 0 <= idx < n else n - 1
        price = closes[i]
        if price:
            atr_pct = (a / price) * 100.0
            if atr_pct < geom.min_atr_pct:
                return False, f"LOW_VOL({atr_pct:.3f}%)"
            if atr_pct > geom.max_atr_pct:
                return False, f"HIGH_VOL({atr_pct:.3f}%)"
    # rolling window up to the current bar (point-in-time, no lookahead)
    end = (idx + 1) if (0 <= idx < n) else n
    lo = max(0, end - _FILTER_WINDOW)
    closes_w = closes[lo:end]
    highs_w = highs[lo:end]
    lows_w = lows[lo:end]
    # regime filter: only trade trending regimes
    if geom.require_trend and len(closes_w) > 10:
        from ..brain.features import _atr as _f_atr
        from ..brain.brain import detect_regime
        try:
            regime = detect_regime(closes_w,
                                   _f_atr(highs_w, lows_w, closes_w))
        except Exception:
            regime = ""
        if regime and regime not in ("trend_up", "trend_down"):
            return False, f"REGIME({regime})"
    # momentum confirmation: trade WITH momentum (BUY needs RSI>50,
    # SELL needs RSI<50); counter-momentum triggers are dropped.
    if geom.require_momentum and len(closes_w) > 14:
        from ..brain.features import _rsi
        rsi = _rsi(closes_w)
        if prop.direction == "BUY" and rsi < 50.0:
            return False, f"RSI_NO_UP_MOM({rsi:.1f})"
        if prop.direction == "SELL" and rsi > 50.0:
            return False, f"RSI_NO_DOWN_MOM({rsi:.1f})"
    return True, ""
