"""Equity-aware symbol eligibility — trade only what the equity can carry.

This is the runtime gate that implements the core directive:

    *"look at the client equity, then trade only the symbols which cater
    for the amount of equity in the client account — and the brain must be
    trained with the information and the tests that have been done."*

Two ideas live here:

1. **Equity-aware tradability.** Every instrument has a broker contract
   (``point``, ``point_value``, ``lot_min`` …) and a sniper stop distance
   derived from its validated geometry (``invalidation_atr_frac`` × ATR).
   The minimum lot already risks ``lot_min * stop_distance * point_value``
   USD. If that minimum-lot risk exceeds the per-trade risk budget
   (``equity * risk_pct``), the account is too small for the instrument —
   flooring to the minimum lot would over-risk and compound into a
   >100% drawdown. The instrument is therefore **not tradable** at that
   equity. This is exactly the condition ``brain.size_position`` refuses
   with ``MIN_LOT_RISK_EXCEEDS_CAP``; ``SymbolEligibility`` computes the
   same threshold *up front* so the engine can skip the instrument before
   spending brain/sniper compute on it.

2. **Validation-aware ranking.** Each symbol carries the forward-test /
   validation results already produced by the platform ("the tests that
   have been done"): trade count, win rate, profit factor, max drawdown,
   and the trained-brain out-of-sample accuracy. Eligible symbols are
   ranked by this evidence so the system trades the best-validated
   instruments first. The per-symbol **trained brains** (``data/brains/
   <slug>_m5/nexus_brain.json``) are loaded by ``load_symbol_brain`` so
   the live decision uses the brain that was trained and validated on
   that symbol — not a generic one.

The runtime check (``is_tradable`` with the actual point-in-time ATR) is
authoritative and look-ahead-free; the static ``min_equity_for`` (using
the representative median ATR) is the deterministic estimate used for the
portal view and ranking. Both use the identical formula, so live and
backtest agree.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..config import DATA_ROOT
from ..brain.risk import RiskPolicy
from .geometry import SymbolGeometry, geometry_for


# ---------------------------------------------------------------------------
# Symbol -> trained-brain slug mapping
# ---------------------------------------------------------------------------
def slug_for(symbol: str) -> str:
    """Map a broker symbol to its trained-brain directory slug.

    ``EURUSD.r`` -> ``eurusd_r``; ``Boom 600 Index.0`` ->
    ``boom_600_index_0``; ``US100`` -> ``us100``. The convention matches
    the ``data/brains/<slug>_m5/`` layout produced by the training CLI.
    """
    return symbol.strip().lower().replace(".", "_").replace(" ", "_")


def brain_dir_for(symbol: str, brain_root: str) -> str:
    """Return the trained-brain directory for a symbol."""
    return os.path.join(brain_root, f"{slug_for(symbol)}_m5")


# ---------------------------------------------------------------------------
# Contract + validation profiles
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SymbolContract:
    """Broker-native contract geometry for one instrument.

    ``point``        — smallest price increment (price units).
    ``point_value``  — USD per point per lot (1.0 = platform default; an
                       admin may set the real broker value per symbol).
    ``lot_min``      — minimum tradable lot.
    ``lot_step``     — lot rounding step.
    ``lot_max``      — maximum tradable lot.
    """
    point: float
    point_value: float = 1.0
    lot_min: float = 0.01
    lot_step: float = 0.01
    lot_max: float = 50.0


@dataclass
class SymbolValidation:
    """The forward-test / validation evidence already produced for a symbol.

    These are the "tests that have been done": the held-out forward-test
    results and the trained-brain out-of-sample accuracy. They drive the
    eligibility ranking (trade the best-validated instruments first) and
    are surfaced to the admin portal.

    ``validated`` is True only when there is positive-expectancy
    forward-test evidence with a sufficient sample (trades >= MIN_VALID_TRADES
    and profit factor > 1.0). Symbols with tiny samples or inverted
    expectancy are not validated and rank last.
    """
    test_trades: int = 0
    test_win_rate: float = 0.0      # 0..1
    test_pf: Optional[float] = None  # profit factor (None if uncomputable)
    test_max_dd_pct: float = 0.0     # max drawdown % on the test window
    oos_brain_accuracy: float = 0.0  # trained-brain held-out direction accuracy
    validated: bool = False
    source: str = ""                 # where the stats came from


MIN_VALID_TRADES = 10
MIN_VALID_PF = 1.0


@dataclass
class SymbolProfile:
    """Full profile for one instrument: contract + geometry + validation."""
    symbol: str
    broker: str
    contract: SymbolContract
    geometry: SymbolGeometry
    validation: SymbolValidation
    representative_atr_points: float = 0.0  # median M5 ATR in points (full data)

    @property
    def invalidation_atr_frac(self) -> float:
        return self.geometry.invalidation_atr_frac


# ---------------------------------------------------------------------------
# The registry — one entry per instrument the platform can trade.
#
# Contracts use the platform's default point_value (1.0, matching the
# backtest contract model). Validation stats are drawn from the forward
# tests already run on the platform (see the win-rate / forward-test
# work): the $50 9-symbol forward test, the $176,72 9-symbol portfolio,
# and the min-deposit sweeps. Representative ATRs are the median M5 ATR
# (in points) over the full loaded dataset.
# ---------------------------------------------------------------------------
def _p(symbol, broker, point, atr_pts, *, pv=1.0, lm=0.01,
       v: SymbolValidation | None = None,
       geom: SymbolGeometry | None = None) -> SymbolProfile:
    return SymbolProfile(
        symbol=symbol, broker=broker,
        contract=SymbolContract(point=point, point_value=pv, lot_min=lm),
        geometry=geom or geometry_for(symbol),
        validation=v or SymbolValidation(),
        representative_atr_points=atr_pts)


SYMBOL_REGISTRY: Dict[str, SymbolProfile] = {
    # ---- FP-Markets FX (.r) ---------------------------------------------
    "EURUSD.r": _p("EURUSD.r", "fpmarkets", 1e-5, 26.00,
        v=SymbolValidation(test_trades=194, test_win_rate=0.691, test_pf=2.81,
                           test_max_dd_pct=5.8, oos_brain_accuracy=0.5185,
                           validated=True, source="ft50_all")),
    "GBPUSD.r": _p("GBPUSD.r", "fpmarkets", 1e-5, 31.14,
        v=SymbolValidation(test_trades=6, test_win_rate=0.833, test_pf=4.81,
                           test_max_dd_pct=2.6, oos_brain_accuracy=0.55,
                           validated=False, source="ft50_all (small sample)")),
    "NZDUSD.r": _p("NZDUSD.r", "fpmarkets", 1e-5, 19.64,
        v=SymbolValidation(test_trades=237, test_win_rate=0.540, test_pf=1.30,
                           test_max_dd_pct=17.3, oos_brain_accuracy=0.53,
                           validated=True, source="ft50_all")),
    # ---- FP-Markets US indices ------------------------------------------
    "US100": _p("US100", "fpmarkets", 0.01, 3126.79,
        v=SymbolValidation(test_trades=3, test_win_rate=1.00, test_pf=None,
                           test_max_dd_pct=0.0, oos_brain_accuracy=0.55,
                           validated=False, source="portfolio_17672 (tiny sample)")),
    "US30": _p("US30", "fpmarkets", 0.01, 2916.43,
        v=SymbolValidation(test_trades=4, test_win_rate=0.75, test_pf=4.18,
                           test_max_dd_pct=0.0, oos_brain_accuracy=0.55,
                           validated=False, source="portfolio_17672 (tiny sample)")),
    # ---- Deriv synthetic indices ----------------------------------------
    "Boom 600 Index.0": _p("Boom 600 Index.0", "deriv", 0.001, 4106.57,
        v=SymbolValidation(test_trades=60, test_win_rate=0.683, test_pf=8.51,
                           test_max_dd_pct=46.2, oos_brain_accuracy=0.684,
                           validated=True, source="min_deposit sweep @25%")),
    "Crash 600 Index.0": _p("Crash 600 Index.0", "deriv", 0.001, 17829.68,
        v=SymbolValidation(test_trades=25, test_win_rate=0.680, test_pf=6.04,
                           test_max_dd_pct=37.9, oos_brain_accuracy=0.66,
                           validated=True, source="min_deposit sweep @25%")),
    "Boom 1000 Index.0": _p("Boom 1000 Index.0", "deriv", 0.001, 7452.11,
        v=SymbolValidation(test_trades=3, test_win_rate=0.333, test_pf=0.93,
                           test_max_dd_pct=35.4, oos_brain_accuracy=0.55,
                           validated=False, source="portfolio_17672 (inverted)")),
    "Volatility 10 Index.0": _p("Volatility 10 Index.0", "deriv", 0.001, 2239.04,
        v=SymbolValidation(test_trades=31, test_win_rate=0.387, test_pf=0.98,
                           test_max_dd_pct=75.9, oos_brain_accuracy=0.54,
                           validated=False, source="portfolio_17672 (degenerate)")),
}


# ---------------------------------------------------------------------------
# Eligibility result
# ---------------------------------------------------------------------------
@dataclass
class EligibilityResult:
    symbol: str
    eligible: bool
    reason: str
    min_equity: float = 0.0          # equity at/above which it is tradable
    risk_per_lot: float = 0.0        # USD risked by the minimum lot
    stop_distance_points: float = 0.0
    atr_points: float = 0.0
    risk_pct: float = 0.0
    risk_scale: float = 1.0
    validated: bool = False
    profile: Optional[SymbolProfile] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "eligible": self.eligible,
            "reason": self.reason, "min_equity": round(self.min_equity, 4),
            "risk_per_lot": round(self.risk_per_lot, 4),
            "stop_distance_points": round(self.stop_distance_points, 4),
            "atr_points": round(self.atr_points, 4),
            "risk_pct": round(self.risk_pct, 4),
            "risk_scale": round(self.risk_scale, 4),
            "validated": self.validated,
        }


# ---------------------------------------------------------------------------
# The eligibility engine
# ---------------------------------------------------------------------------
class SymbolEligibility:
    """Decide which symbols an account of a given equity may trade.

    The runtime path (``is_tradable`` with the actual ATR) is the
    authoritative, look-ahead-free check. The static path
    (``min_equity_for`` / ``tradable_symbols`` with the representative
    ATR) powers the portal view and ranking. Both share one formula:

        min_equity = lot_min * (invalidation_atr_frac * atr_points)
                     * point_value / (risk_pct * risk_scale)

    which is exactly the equity below which ``brain.size_position``
    refuses the instrument with ``MIN_LOT_RISK_EXCEEDS_CAP``.
    """

    def __init__(
        self,
        registry: Optional[Dict[str, SymbolProfile]] = None,
        default_risk_pct: Optional[float] = None,
        tiers=None,
    ):
        self.registry = registry if registry is not None else SYMBOL_REGISTRY
        # default risk = the committed flat 15% tier risk (b7c31c9) unless
        # an explicit value is supplied.
        self._risk_policy = RiskPolicy(tiers=tiers) if tiers else RiskPolicy()
        self.default_risk_pct = (
            default_risk_pct if default_risk_pct is not None
            else self._risk_policy.risk_pct_for(50.0))

    # -- profile lookup ------------------------------------------------
    def profile(self, symbol: str) -> Optional[SymbolProfile]:
        return self.registry.get(symbol)

    # -- the core formula ---------------------------------------------
    def _contract_for(self, symbol: str, *, point: Optional[float] = None,
                      point_value: Optional[float] = None,
                      lot_min: Optional[float] = None) -> Tuple[SymbolContract, SymbolGeometry, float]:
        """Resolve contract + geometry + representative ATR for a symbol.

        For a registered symbol the profile is authoritative. For an
        unregistered symbol (e.g. a newly enabled instrument) the runtime
        ``point`` / ``point_value`` / ``lot_min`` overrides are used with
        the default contract, so eligibility is still computed correctly —
        the symbol simply ranks as unvalidated.
        """
        prof = self.registry.get(symbol)
        if prof is not None:
            return prof.contract, prof.geometry, prof.representative_atr_points
        c = SymbolContract(
            point=float(point or 0.0),
            point_value=float(point_value if point_value is not None else 1.0),
            lot_min=float(lot_min if lot_min is not None else 0.01))
        g = geometry_for(symbol)
        return c, g, 0.0

    def min_equity_for(
        self,
        symbol: str,
        *,
        risk_pct: Optional[float] = None,
        risk_scale: float = 1.0,
        atr_points: Optional[float] = None,
        point: Optional[float] = None,
        point_value: Optional[float] = None,
        lot_min: Optional[float] = None,
    ) -> float:
        """Minimum equity at which ``symbol`` is tradable.

        ``risk_scale`` mirrors ``brain.size_position``'s entry-quality
        scaling (0.4..1.0). At the eligibility gate the entry quality is
        not yet known, so the default ``1.0`` is the standard
        full-risk threshold; ``size_position`` is the authoritative final
        guard with the actual entry quality.
        """
        contract, geom, rep_atr = self._contract_for(
            symbol, point=point, point_value=point_value, lot_min=lot_min)
        risk_pct = risk_pct if risk_pct is not None else self.default_risk_pct
        atr = atr_points if atr_points is not None else rep_atr
        if risk_pct <= 0 or risk_scale <= 0 or atr <= 0:
            return 0.0
        stop_distance_points = geom.invalidation_atr_frac * atr
        risk_per_lot = stop_distance_points * contract.point_value
        if risk_per_lot <= 0:
            return 0.0
        return (contract.lot_min * risk_per_lot
                / (risk_pct * risk_scale))

    def is_tradable(
        self,
        symbol: str,
        equity: float,
        *,
        atr_points: Optional[float] = None,
        risk_pct: Optional[float] = None,
        risk_scale: float = 1.0,
        point: Optional[float] = None,
        point_value: Optional[float] = None,
        lot_min: Optional[float] = None,
    ) -> EligibilityResult:
        """Return whether ``symbol`` may be traded at ``equity``.

        Uses the actual point-in-time ``atr_points`` when supplied (the
        runtime path); otherwise the representative median ATR (the
        static/portal path).
        """
        contract, geom, rep_atr = self._contract_for(
            symbol, point=point, point_value=point_value, lot_min=lot_min)
        risk_pct = risk_pct if risk_pct is not None else self.default_risk_pct
        atr = atr_points if atr_points is not None else rep_atr
        prof = self.registry.get(symbol)
        stop_distance_points = geom.invalidation_atr_frac * atr
        risk_per_lot = stop_distance_points * contract.point_value
        # No-fail-closed rule: block ONLY when a positive threshold is
        # computable AND the equity is below it. If the price scale / ATR
        # is unavailable (e.g. an unregistered symbol with no point yet),
        # the threshold cannot be computed — do NOT block; size_position
        # is the authoritative final guard and will refuse an un-sizable
        # trade. This keeps the gate from ever fail-closing on indeterminate
        # input, per the platform's no-fail-closed directive.
        if risk_pct <= 0 or atr <= 0 or risk_per_lot <= 0:
            return EligibilityResult(
                symbol=symbol, eligible=True,
                reason="THRESHOLD_UNKNOWN",
                min_equity=0.0, risk_per_lot=risk_per_lot,
                stop_distance_points=stop_distance_points, atr_points=atr,
                risk_pct=risk_pct, risk_scale=risk_scale,
                validated=bool(prof and prof.validation.validated),
                profile=prof)
        min_equity = (contract.lot_min * risk_per_lot
                      / (risk_pct * risk_scale))
        eligible = float(equity or 0.0) >= min_equity
        return EligibilityResult(
            symbol=symbol, eligible=eligible,
            reason="EQUITY_OK" if eligible else "EQUITY_BELOW_MIN_LOT",
            min_equity=min_equity, risk_per_lot=risk_per_lot,
            stop_distance_points=stop_distance_points, atr_points=atr,
            risk_pct=risk_pct, risk_scale=risk_scale,
            validated=bool(prof and prof.validation.validated),
            profile=prof)

    # -- ranking -------------------------------------------------------
    def rank_key(self, symbol: str) -> Tuple[int, float, float, int]:
        """Sort key for eligible symbols: best-validated first.

        Order: validated (1) before unvalidated (0); then profit factor;
        then win rate; then trade count (more evidence ranks higher).
        """
        prof = self.registry.get(symbol)
        if prof is None:
            return (0, 0.0, 0.0, 0)
        v = prof.validation
        if not v.validated:
            return (0, 0.0, v.test_win_rate, v.test_trades)
        pf = v.test_pf if v.test_pf else 0.0
        return (1, min(pf, 99.0), v.test_win_rate, v.test_trades)

    def tradable_symbols(
        self,
        equity: float,
        symbols: Optional[List[str]] = None,
        *,
        risk_pct: Optional[float] = None,
        risk_scale: float = 1.0,
        atr_points_by_symbol: Optional[Dict[str, float]] = None,
    ) -> List[EligibilityResult]:
        """Return the eligible symbols for ``equity``, ranked by evidence.

        ``atr_points_by_symbol`` supplies the actual point-in-time ATR per
        symbol (runtime path); symbols without an entry fall back to the
        representative ATR.
        """
        symbols = symbols if symbols is not None else list(self.registry)
        out: List[EligibilityResult] = []
        for s in symbols:
            atr = (atr_points_by_symbol or {}).get(s)
            res = self.is_tradable(
                s, equity, atr_points=atr, risk_pct=risk_pct,
                risk_scale=risk_scale)
            if res.eligible:
                out.append(res)
        out.sort(key=lambda r: self.rank_key(r.symbol), reverse=True)
        return out

    # -- reporting -----------------------------------------------------
    def report(self, equity: float, symbols: Optional[List[str]] = None,
               *, risk_pct: Optional[float] = None) -> Dict[str, Any]:
        """Full eligibility report for an equity level (portal / demo)."""
        symbols = symbols if symbols is not None else list(self.registry)
        tradable = self.tradable_symbols(equity, symbols, risk_pct=risk_pct)
        tradable_syms = [r.symbol for r in tradable]
        rejected = []
        for s in symbols:
            r = self.is_tradable(s, equity, risk_pct=risk_pct)
            if not r.eligible:
                rejected.append(r.to_dict())
        return {
            "equity": equity,
            "risk_pct": risk_pct if risk_pct is not None else self.default_risk_pct,
            "tradable": [r.to_dict() for r in tradable],
            "tradable_symbols": tradable_syms,
            "rejected": rejected,
        }


# ---------------------------------------------------------------------------
# Per-symbol trained-brain loading
# ---------------------------------------------------------------------------
def load_symbol_brain(
    symbol: str,
    *,
    brain_root: Optional[str] = None,
    mc_samples: int = 16,
    brain_kwargs: Optional[Dict[str, Any]] = None,
):
    """Load the trained NexusBrain for ``symbol`` from ``data/brains``.

    The brain was trained and validated on this exact symbol (its
    held-out accuracy is in the brain JSON's ``report``). Returns a
    ``NexusBrain`` wired to that ensemble, or ``None`` if no trained
    brain exists for the symbol (the caller falls back to a default).
    """
    from ..brain import NexusBrain
    from ..brain.trainer import load_brain
    from ..brain.unified import (SYMBOL_UNIVERSE, load_unified,
                                 unified_brain_dir)

    brain_root = brain_root or os.path.join(DATA_ROOT, "brains")
    brain_dir = brain_dir_for(symbol, brain_root)
    if os.path.isdir(brain_dir):
        try:
            ensemble = load_brain(brain_dir, mc_samples=mc_samples)
            brain = NexusBrain(ensemble=ensemble, **(brain_kwargs or {}))
            brain.ensemble.mc_samples = mc_samples
            return brain
        except Exception:
            pass
    # "All brains work as one brain": the trained unified checkpoint is the
    # fallback for known symbols when no legacy per-symbol checkpoint exists.
    if symbol not in SYMBOL_UNIVERSE:
        return None
    try:
        brain = load_unified(unified_brain_dir(brain_root),
                             mc_samples=mc_samples)
        if brain is not None:
            brain.ensemble.mc_samples = mc_samples
            return brain
    except Exception:
        pass
    return None


def default_brain_root() -> str:
    """The trained-brain root directory (``data/brains``)."""
    return os.path.join(DATA_ROOT, "brains")
