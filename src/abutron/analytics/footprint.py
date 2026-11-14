"""Volume footprint analytics.

Builds a price-by-price volume profile from trade flow and prints the
footprint signature: POC, HVN/LVN, value area, cell-level buy/sell
imbalance and absorption. Provides the footprint grid used by the APEX
decision layer and the command-center visualization.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .orderflow import Trade


@dataclass
class FootprintCell:
    price: float                # cell mid price
    buy_vol: float = 0.0
    sell_vol: float = 0.0
    total_vol: float = 0.0
    imbalance: float = 0.0
    delta: float = 0.0

    def to_dict(self) -> Dict:
        return {"price": self.price, "buy_vol": self.buy_vol,
                "sell_vol": self.sell_vol, "total_vol": self.total_vol,
                "imbalance": self.imbalance, "delta": self.delta}


@dataclass
class Footprint:
    symbol: str
    tick_size: float
    cells: List[FootprintCell]
    total_volume: float
    poc_price: float
    poc_volume: float
    hvn_prices: List[float]
    lvn_prices: List[float]
    value_area_low: float
    value_area_high: float
    value_area_pct: float
    features: Dict[str, float] = field(default_factory=dict)

    def to_grid(self, limit: int = 40) -> List[Dict]:
        """Serialize a compact grid for the command-center footprint view."""
        cells = sorted(self.cells, key=lambda c: c.price)
        return [c.to_dict() for c in cells[-limit:]]


class VolumeFootprintBuilder:
    def __init__(self, tick_size: float,
                 cell_multiples: int = 1):
        self.tick_size = max(tick_size, 1e-9)
        self.cell_width = tick_size * cell_multiples

    def build(self, symbol: str, trades: List[Trade],
              lookback_bars: int = 1) -> Footprint:
        buckets: Dict[int, FootprintCell] = {}
        for t in trades:
            idx = int(round((t.price - self._anchor) / self.cell_width))
            # anchor to a stable grid: round to nearest 10th of a cell
            cell = buckets.get(idx)
            if cell is None:
                mid = self._anchor + (idx + 0.5) * self.cell_width
                cell = FootprintCell(price=mid)
                buckets[idx] = cell
            if t.side == "buy":
                cell.buy_vol += t.size
            else:
                cell.sell_vol += t.size

        cells = list(buckets.values())
        for c in cells:
            c.total_vol = c.buy_vol + c.sell_vol
            c.delta = c.buy_vol - c.sell_vol
            c.imbalance = (c.delta / c.total_vol) if c.total_vol else 0.0

        total = sum(c.total_vol for c in cells)
        cells.sort(key=lambda c: c.total_vol, reverse=True)
        poc = cells[0] if cells else FootprintCell(price=0.0)

        # HVN: top 10% volume cells; LVN: bottom 10%
        ranked = sorted(cells, key=lambda c: c.price)
        hvn_n = max(1, len(ranked) // 10)
        lvn_n = max(1, len(ranked) // 10)
        hvn = [c.price for c in ranked if c.total_vol >= ranked[-hvn_n].total_vol]
        lvn = [c.price for c in ranked if c.total_vol <= ranked[:lvn_n][0].total_vol] \
            if ranked else []

        # Value area: 70% of total volume around the POC
        va_cells = []
        acc = 0.0
        for c in sorted(cells, key=lambda c: c.price):
            va_cells.append(c)
        va = 0.70 * total
        running = 0.0
        va_low = va_high = poc.price
        # grow outward from POC index
        poci = next((i for i, c in enumerate(va_cells)
                     if abs(c.price - poc.price) < 1e-12), 0)
        low_i, high_i = poci, poci
        while running < va and (low_i > 0 or high_i < len(va_cells) - 1):
            expand_down = (low_i > 0) and (
                low_i - 1 >= 0 and (high_i >= len(va_cells) - 1 or
                                    (va_cells[low_i - 1].total_vol >=
                                     va_cells[high_i + 1].total_vol if high_i + 1 < len(va_cells) else True)))
            if expand_down:
                low_i -= 1
            else:
                high_i += 1
            running += va_cells[low_i if expand_down else high_i].total_vol
        if va_cells:
            va_low = min(va_cells[i].price for i in range(low_i, high_i + 1))
            va_high = max(va_cells[i].price for i in range(low_i, high_i + 1))

        fp = Footprint(
            symbol=symbol, tick_size=self.tick_size, cells=cells,
            total_volume=total, poc_price=poc.price, poc_volume=poc.total_vol,
            hvn_prices=hvn, lvn_prices=lvn,
            value_area_low=va_low, value_area_high=va_high,
            value_area_pct=0.70,
        )
        fp.features = self._features(fp)
        return fp

    _anchor: float = 0.0

    def _features(self, fp: Footprint) -> Dict[str, float]:
        total = fp.total_volume
        # Buy/sell dominance across the value area
        va_cells = [c for c in fp.cells
                    if fp.value_area_low <= c.price <= fp.value_area_high]
        buy = sum(c.buy_vol for c in va_cells)
        sell = sum(c.sell_vol for c in va_cells)
        return {
            "total_volume": total,
            "poc_volume_pct": (fp.poc_volume / total) if total else 0.0,
            "va_buy_pct": (buy / (buy + sell)) if (buy + sell) else 0.5,
            "va_imbalance": ((buy - sell) / (buy + sell)) if (buy + sell) else 0.0,
            "value_area_span": fp.value_area_high - fp.value_area_low,
        }


def build_footprint(symbol: str, trades: List[Trade], tick_size: float,
                    **kw) -> Footprint:
    return VolumeFootprintBuilder(tick_size, **kw).build(symbol, trades, **kw)
