"""Explicit per-trade loss-pattern guard ("never repeat").

Neural weights and the scalar ``confirm_bias`` / ``quality_floor`` learn
* tendencies*. They do not guarantee that the exact feature pattern of a
losing trade will not be taken again. ``LossPatternGuard`` is a deterministic,
persistent, per-(symbol, direction, regime) veto:

* a losing trade records a quantized signature of the decision features;
* a later proposal with the same signature returns ``HOLD`` before sizing
  or dispatch;
* a winning trade in that signature clears the veto, so the brain can
  recover when the pattern has demonstrably become profitable.

The guard uses only the 23 market features (symbol one-hots are excluded)
and is serialized with the unified brain. It is intentionally conservative:
it stores compact quantized signatures, never broker credentials or PnL
histories.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .features import N_FEATURES
from .signals import SIGNAL_SLOT_INDICES

GUARD_VERSION = "loss_guard_v1"


def _finite(v: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) else 0.0


class LossPatternGuard:
    def __init__(self, *, min_losses: int = 1, market_bin: float = 0.5,
                 signal_bin: float = 0.25, max_patterns: int = 5000):
        self.min_losses = max(1, int(min_losses))
        self.market_bin = max(0.05, float(market_bin))
        self.signal_bin = max(0.05, float(signal_bin))
        self.max_patterns = max(100, int(max_patterns))
        self._patterns: Dict[str, Dict[str, Any]] = {}
        self.blocked = 0
        self.total_losses = 0
        self.total_wins = 0

    @staticmethod
    def _signatures(features: Sequence[float], market_bin: float,
                    signal_bin: float) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
        arr = np.asarray([_finite(v) for v in list(features)[:N_FEATURES]], dtype=float)
        if len(arr) < N_FEATURES:
            arr = np.pad(arr, (0, N_FEATURES - len(arr)))
        arr = np.clip(arr, -8.0, 8.0)
        market = tuple(int(round(float(v) / market_bin)) for v in arr)
        signal = tuple(int(round(float(arr[i]) / signal_bin))
                       for i in SIGNAL_SLOT_INDICES)
        return market, signal

    @staticmethod
    def _key(symbol: str, direction: str, regime: str,
             market: Tuple[int, ...], signal: Tuple[int, ...]) -> str:
        raw = "|".join((str(symbol), str(direction), str(regime),
                        ",".join(map(str, market)), ",".join(map(str, signal))))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _match(self, *, symbol: str, direction: str, regime: str,
               market: Tuple[int, ...], signal: Tuple[int, ...],
               active_only: bool = True) -> Optional[Dict[str, Any]]:
        exact = self._patterns.get(self._key(symbol, direction, regime,
                                             market, signal))
        candidates = [exact] if exact else []
        if not candidates:
            # A signal-only match catches the same microstructure/regime
            # setup when a small price feature changes the full signature.
            candidates = [p for p in self._patterns.values()
                          if p.get("symbol") == symbol
                          and p.get("direction") == direction
                          and p.get("regime") == regime
                          and tuple(p.get("signal_sig", ())) == signal]
        for p in candidates:
            if not p:
                continue
            if active_only and not p.get("active"):
                continue
            return p
        return None

    def learn(self, *, symbol: str, direction: str, regime: str,
              features: Sequence[float], pnl: float) -> Dict[str, Any]:
        market, signal = self._signatures(features, self.market_bin,
                                          self.signal_bin)
        key = self._key(symbol, direction, regime, market, signal)
        pnl = _finite(pnl)
        if pnl < 0:
            self.total_losses += 1
            entry = self._patterns.get(key)
            if entry is None:
                entry = {
                    "symbol": symbol, "direction": direction, "regime": regime,
                    "market_sig": list(market), "signal_sig": list(signal),
                    "losses": 0, "wins": 0, "active": True, "last_pnl": pnl,
                }
                self._patterns[key] = entry
            entry["losses"] = int(entry.get("losses", 0)) + 1
            entry["active"] = True
            entry["last_pnl"] = pnl
        elif pnl > 0:
            self.total_wins += 1
            entry = self._match(symbol=symbol, direction=direction,
                                regime=regime, market=market, signal=signal,
                                active_only=True)
            if entry is not None:
                entry["wins"] = int(entry.get("wins", 0)) + 1
                entry["active"] = False
                entry["last_pnl"] = pnl
        # Bound memory by retiring the oldest inactive records first.
        if len(self._patterns) > self.max_patterns:
            removable = [k for k, v in self._patterns.items()
                         if not v.get("active")]
            if not removable:
                removable = list(self._patterns)
            for k in removable[:len(self._patterns) - self.max_patterns]:
                self._patterns.pop(k, None)
        return self.stats()

    def should_block(self, *, symbol: str, direction: str, regime: str,
                     features: Sequence[float]) -> Tuple[bool, str]:
        market, signal = self._signatures(features, self.market_bin,
                                          self.signal_bin)
        entry = self._match(symbol=symbol, direction=direction,
                            regime=regime, market=market, signal=signal,
                            active_only=True)
        if entry and int(entry.get("losses", 0)) >= self.min_losses:
            self.blocked += 1
            return True, (f"LOSS_PATTERN_REPEAT:{entry.get('losses', 0)}"
                          f"_losses:{str(entry.get('last_pnl', 0))}")
        return False, ""

    def stats(self) -> Dict[str, Any]:
        return {
            "active_loss_patterns": sum(1 for p in self._patterns.values()
                                        if p.get("active")),
            "total_patterns": len(self._patterns),
            "total_losses": self.total_losses,
            "total_wins": self.total_wins,
            "blocked": self.blocked,
        }

    def state_dict(self) -> Dict[str, Any]:
        return {
            "version": GUARD_VERSION,
            "min_losses": self.min_losses,
            "market_bin": self.market_bin,
            "signal_bin": self.signal_bin,
            "max_patterns": self.max_patterns,
            "patterns": self._patterns,
            "blocked": self.blocked,
            "total_losses": self.total_losses,
            "total_wins": self.total_wins,
        }

    def load_state(self, data: Optional[Dict[str, Any]]) -> None:
        d = dict(data or {})
        self.min_losses = max(1, int(d.get("min_losses", self.min_losses)))
        self.market_bin = max(0.05, float(d.get("market_bin", self.market_bin)))
        self.signal_bin = max(0.05, float(d.get("signal_bin", self.signal_bin)))
        self.max_patterns = max(100, int(d.get("max_patterns", self.max_patterns)))
        self._patterns = {str(k): dict(v) for k, v in
                          (d.get("patterns") or {}).items()}
        self.blocked = int(d.get("blocked", 0) or 0)
        self.total_losses = int(d.get("total_losses", 0) or 0)
        self.total_wins = int(d.get("total_wins", 0) or 0)
