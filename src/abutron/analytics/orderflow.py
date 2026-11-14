"""Order flow analytics engine.

Consumes tick-level trade flow (price, size, aggressor side) exported by
the MT5 order-flow EA, and computes:
  * trade delta / cumulative delta
  * buy/sell imbalance and imbalance ratio
  * delta divergence vs price action
  * absorption detection (large orders holding price)
  * footprint-aligned flow features used by the APEX signal layer

This is market context for the decision engine — it does not itself
generate orders.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Trade:
    """One aggressor trade (tick) from the native order-flow export."""
    price: float
    size: float
    side: str            # 'buy' | 'sell'
    time: str = ""
    order_id: int = 0

    @property
    def signed_size(self) -> float:
        return self.size if self.side == "buy" else -self.size


@dataclass
class OrderFlowSnapshot:
    symbol: str
    bar_open: str
    bar_close: str
    trades: List[Trade]
    buy_volume: float
    sell_volume: float
    delta: float
    cumulative_delta: float
    imbalance: float           # (buy - sell) / (buy + sell), -1..1
    trade_count: int
    max_imbalance: float
    absorbed_up_lots: float
    absorbed_down_lots: float
    features: Dict[str, float] = field(default_factory=dict)


class OrderFlowEngine:
    """Aggregates trade flow per closed M5 bar and derives features."""

    def __init__(self):
        self._snapshots: Dict[str, OrderFlowSnapshot] = {}

    def aggregate(self, symbol: str, bar_open: str, bar_close: str,
                  trades: List[Trade]) -> OrderFlowSnapshot:
        buy = sum(t.size for t in trades if t.side == "buy")
        sell = sum(t.size for t in trades if t.side == "sell")
        total = buy + sell
        delta = buy - sell
        cum_delta = 0.0
        max_imb = 0.0
        running = 0.0
        absorbed_up = 0.0
        absorbed_down = 0.0
        prices = [t.price for t in trades]
        for t in trades:
            running += t.signed_size
            max_imb = max(max_imb, abs(running / total) if total else 0.0)
        # Absorption: large sell blocks while price holds/rises = buyers
        # absorbing supply; large buy blocks while price falls = sellers
        # absorbing demand.
        if prices and total > 0:
            hi, lo = max(prices), min(prices)
            for t in trades:
                if t.size > 3 * (total / max(1, len(trades))):  # outlier size
                    if t.side == "sell" and (hi > lo) and t.price >= lo + 0.35 * (hi - lo):
                        absorbed_up += t.size
                    elif t.side == "buy" and (hi > lo) and t.price <= lo + 0.35 * (hi - lo):
                        absorbed_down += t.size

        snap = OrderFlowSnapshot(
            symbol=symbol, bar_open=bar_open, bar_close=bar_close,
            trades=trades, buy_volume=buy, sell_volume=sell, delta=delta,
            cumulative_delta=running,
            imbalance=(delta / total) if total else 0.0,
            trade_count=len(trades),
            max_imbalance=max_imb,
            absorbed_up_lots=absorbed_up,
            absorbed_down_lots=absorbed_down,
        )
        snap.features = self._features(snap)
        self._snapshots[f"{symbol}|{bar_open}"] = snap
        return snap

    def _features(self, s: OrderFlowSnapshot) -> Dict[str, float]:
        total = s.buy_volume + s.sell_volume
        return {
            "delta": s.delta,
            "delta_pct": (s.delta / total) if total else 0.0,
            "imbalance": s.imbalance,
            "max_imbalance": s.max_imbalance,
            "buy_ratio": (s.buy_volume / total) if total else 0.5,
            "absorption_bias": (s.absorbed_up_lots - s.absorbed_down_lots)
            / max(1.0, total),
            "trade_count": float(s.trade_count),
        }

    def latest(self, symbol: str) -> Optional[OrderFlowSnapshot]:
        for key, snap in self._snapshots.items():
            if key.startswith(symbol + "|"):
                return snap
        return None

    def all_snapshots(self) -> Dict[str, OrderFlowSnapshot]:
        return dict(self._snapshots)


orderflow_engine = OrderFlowEngine()
