"""Annualisation helpers required by the APEX signal-only engine."""
from __future__ import annotations

import math
from statistics import median
from typing import Iterable


def bars_per_year(asset_class: str) -> float:
    """Return a conventional yearly bar count for the supported market class."""
    kind = str(asset_class or "").strip().lower()
    days = 365.25 if kind in {
        "crypto", "crypto_spot", "deriv_synthetic", "synthetic",
        "24/7", "24x7",
    } else 252.0
    # Current APEX input is M5, but this helper is class-level and is only a
    # volatility annualiser, not a timeframe validator.
    return days * 24.0 * 12.0


def annualisation_from_ts(timestamps: Iterable[float]) -> float:
    """Infer bars/year from timestamp spacing; use M5 annualisation if sparse."""
    try:
        values = [float(ts) for ts in timestamps]
        gaps = [b - a for a, b in zip(values, values[1:])
                if math.isfinite(a) and math.isfinite(b) and b > a]
        seconds = median(gaps) if gaps else 300.0
        if not math.isfinite(seconds) or seconds <= 0:
            seconds = 300.0
        return 365.25 * 24.0 * 60.0 * 60.0 / seconds
    except (TypeError, ValueError, OverflowError):
        return 365.25 * 24.0 * 12.0
