"""True tick/trade data ingestion — replaces candle-derived proxies.

Reads the native MT5 export files produced by ApexOrderFlowExport.mq5
(or Deriv API tick history) and converts them into the Trade objects
that OrderFlowEngine and VolumeFootprintBuilder already consume.

This is the path from **PROXY → VERIFIED** for order flow / imbalance /
delta / absorption and volume footprint / POC:

    Broker terminal (MT5 EA / Deriv API)
      → export files (JSON in MQL5/Files)
      → TickIngestion.load_export()
      → {bar_time: [Trade, ...]}
      → signals.prepare_signal_data(tick_trades=..., depth=...)
      → true_tick_bars > 0 (not proxy_bars)

The signal pipeline in ``brain/signals.py`` already checks for
``tick_trades.get(key)`` — if present, it uses real OrderFlowEngine
aggregation and VolumeFootprintBuilder instead of _proxy_orderflow()
and _proxy_footprint(). This module bridges the export files to that
path.
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..analytics.orderflow import Trade


@dataclass
class DepthSnapshot:
    """A single market depth (bid/ask) snapshot."""
    symbol: str
    bid: float
    ask: float
    spread: int
    last: float
    volume: int
    timestamp: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "bid": self.bid, "ask": self.ask,
            "spread": self.spread, "last": self.last,
            "volume": self.volume, "timestamp": self.timestamp,
        }


class TickIngestion:
    """Loads true tick/trade data from MT5 export files.

    Reads the JSON files written by ApexOrderFlowExport.mq5:
      - ABUTRON_NATIVE_TRADES_V2_*.txt  → Trade objects
      - ABUTRON_NATIVE_DEPTH_V2_*.txt   → DepthSnapshot objects

    The trades are grouped by bar time (M5 bucket) so the signal
    pipeline's ``tick_trades`` parameter receives ``{bar_time: [Trade]}``.
    """

    TRADE_PREFIX = "ABUTRON_NATIVE_TRADES_V2_"
    DEPTH_PREFIX = "ABUTRON_NATIVE_DEPTH_V2_"
    BARS_PREFIX = "ABUTRON_NATIVE_BARS_V2_"
    IDENT_PREFIX = "ABUTRON_NATIVE_ACCOUNT_V2_"

    def __init__(self, mql5_files_dir: str):
        self.dir = mql5_files_dir

    # ------------------------------------------------------------------
    # Trade ingestion
    # ------------------------------------------------------------------
    def load_trades(self) -> Dict[str, List[Trade]]:
        """Load all trade deals from export files, grouped by bar time.

        Returns a dict mapping bar-time (epoch seconds) → list of Trade
        objects. This is the ``tick_trades`` argument for
        ``prepare_signal_data()``.
        """
        result: Dict[str, List[Trade]] = {}
        pattern = os.path.join(self.dir, self.TRADE_PREFIX + "*.txt")
        for path in sorted(glob.glob(pattern)):
            try:
                with open(path, "r") as fh:
                    data = json.load(fh)
            except (ValueError, OSError):
                continue

            symbol = data.get("symbol", "")
            deals = data.get("deals", [])
            for deal in deals:
                if not isinstance(deal, dict):
                    continue
                price = float(deal.get("price", 0.0))
                volume = float(deal.get("volume", 0.0))
                deal_type = int(deal.get("type", 0))
                entry = int(deal.get("entry", 0))

                if price <= 0 or volume <= 0:
                    continue

                # DEAL_TYPE_BUY = 0, DEAL_TYPE_SELL = 1
                # entry: DEAL_TYPE_BUY_LIMIT=0, DEAL_TYPE_SELL_LIMIT=1,
                #        DEAL_TYPE_BUY_MARKET=2, DEAL_TYPE_SELL_MARKET=3
                if deal_type == 0:  # BUY
                    side = "buy"
                elif deal_type == 1:  # SELL
                    side = "sell"
                else:
                    continue  # skip non-trade deals

                # Group by M5 bar time
                deal_time = int(deal.get("time", 0))
                bar_time = self._to_bar_time(deal_time, "M5")
                result.setdefault(str(bar_time), []).append(
                    Trade(price=price, size=volume, side=side,
                          time=str(deal_time),
                          order_id=int(deal.get("order", 0))))
        return result

    # ------------------------------------------------------------------
    # Depth ingestion
    # ------------------------------------------------------------------
    def load_depth(self) -> Dict[str, Dict[str, Any]]:
        """Load market depth snapshots, keyed by timestamp.

        Returns a dict mapping timestamp string → depth features dict.
        This is the ``depth`` argument for ``prepare_signal_data()``.
        """
        result: Dict[str, Dict[str, Any]] = {}
        pattern = os.path.join(self.dir, self.DEPTH_PREFIX + "*.txt")
        for path in sorted(glob.glob(pattern)):
            try:
                with open(path, "r") as fh:
                    data = json.load(fh)
            except (ValueError, OSError):
                continue

            ts = data.get("timestamp", "")
            result[ts] = {
                "bid": float(data.get("bid", 0.0)),
                "ask": float(data.get("ask", 0.0)),
                "spread": int(data.get("spread", 0)),
                "last": float(data.get("last", 0.0)),
                "volume": int(data.get("volume", 0)),
                "symbol": data.get("symbol", ""),
                "source": "native_depth",
            }
        return result

    # ------------------------------------------------------------------
    # Bars ingestion
    # ------------------------------------------------------------------
    def load_bars(self) -> List[Dict[str, Any]]:
        """Load bar data from export files.

        Returns a list of bar dicts with time, OHLCV. Useful for
        verifying the broker-side bar data matches offline data.
        """
        bars: List[Dict[str, Any]] = []
        pattern = os.path.join(self.dir, self.BARS_PREFIX + "*.txt")
        for path in sorted(glob.glob(pattern)):
            try:
                with open(path, "r") as fh:
                    data = json.load(fh)
            except (ValueError, OSError):
                continue
            for bar in data.get("bars", []):
                bars.append({
                    "time": int(bar.get("time", 0)),
                    "open": float(bar.get("open", 0.0)),
                    "high": float(bar.get("high", 0.0)),
                    "low": float(bar.get("low", 0.0)),
                    "close": float(bar.get("close", 0.0)),
                    "tick_volume": int(bar.get("tick_volume", 0)),
                    "spread": int(bar.get("spread", 0)),
                    "symbol": data.get("symbol", ""),
                })
        return bars

    # ------------------------------------------------------------------
    # Identity ingestion
    # ------------------------------------------------------------------
    def load_identity(self) -> Optional[Dict[str, Any]]:
        """Load the latest account identity export."""
        pattern = os.path.join(self.dir, self.IDENT_PREFIX + "*.txt")
        matches = sorted(glob.glob(pattern))
        if not matches:
            return None
        try:
            with open(matches[-1], "r") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            return None

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------
    @staticmethod
    def _to_bar_time(epoch: int, timeframe: str = "M5") -> int:
        """Floor an epoch timestamp to the start of the bar."""
        minutes = 1
        if timeframe == "M1":
            minutes = 1
        elif timeframe == "M5":
            minutes = 5
        elif timeframe == "M15":
            minutes = 15
        elif timeframe == "H1":
            minutes = 60
        elif timeframe == "H4":
            minutes = 240
        elif timeframe == "D1":
            minutes = 1440
        return epoch - (epoch % (minutes * 60))

    # ------------------------------------------------------------------
    # Verification summary
    # ------------------------------------------------------------------
    def verification_summary(self) -> Dict[str, Any]:
        """Return a summary of how much true tick data is available.

        Used to confirm the PROXY → VERIFIED transition: if
        true_tick_bars > 0, the signal pipeline is using real broker
        trade data instead of candle-derived proxies.
        """
        trades = self.load_trades()
        depth = self.load_depth()
        bars = self.load_bars()
        ident = self.load_identity()

        total_trades = sum(len(t) for t in trades.values())
        return {
            "mql5_files_dir": self.dir,
            "bars_with_trades": len(trades),
            "total_trade_events": total_trades,
            "depth_snapshots": len(depth),
            "bars_exported": len(bars),
            "identity_exported": ident is not None,
            "source": "native_mt5_export",
            "verified": total_trades > 0,
            "honest_status": (
                "VERIFIED — true broker tick/trade data from MT5 export"
                if total_trades > 0
                else "NO_DATA — no tick export files found; using "
                     "candle-derived proxies"
            ),
        }
