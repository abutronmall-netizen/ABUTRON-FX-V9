"""Train the brain from REAL trade history (per broker / per account).

The user exports trade history per broker per account. Each settled
trade is a labelled outcome of the brain's own decisions:

    (features at entry)  ->  (the move the market actually made)

This script turns that history into training signal for the unified
brain. It:

  1. Loads trade files (CSV or JSON, one or many, per broker/account).
     Column names are auto-detected (symbol, direction, entry/exit
     time & price, lots, pnl, broker, account ...).
  2. Aligns each trade to the market-data candles by symbol + entry
     time, so the feature vector is built from the exact window the
     brain saw at decision time (no look-ahead).
  3. Trains the brain two ways:
       --mode online : one online gradient step per trade from the
                       realised move (feeds the live self-learner).
       --mode offline: collect all (features, label) pairs and run
                       mini-batch Adam training for stable refinement.
  4. Saves the refined brain (persisting the online state).

Usage:
    PYTHONPATH=src python -m scripts.train_from_trades \
        --trades /path/to/trade_history/*.csv \
        --data /tmp/market-data \
        --mode online --lr 1e-3 \
        [--in data/brains/unified_m5] [--out data/brains/unified_m5]

If a trade's entry time is not covered by the market data, it is skipped
(counted and reported) — market data covering the trade period is
required to build entry-time features.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import numpy as np

from abutron.backtest.loader import MarketDataLoader
from abutron.brain.unified import (
    SYMBOL_UNIVERSE, build_unified_feature_vector, load_unified,
    n_unified_features, save_unified, unified_brain_dir)
from abutron.brain.features import returns_from_closes


# --------------------------------------------------------------------------
# Column detection
# --------------------------------------------------------------------------
_COL_ALIASES = {
    "symbol": ["symbol", "instrument", "pair", "trading_symbol",
               "trading_pair", "asset"],
    "broker": ["broker", "broker_name", "broker_name", "venue"],
    "account": ["account", "account_id", "account_number", "login",
                "account_name", "client"],
    "direction": ["direction", "side", "action", "order_type", "type",
                  "position_type", "buy_sell"],
    "entry_time": ["entry_time", "entry_datetime", "open_time",
                   "opened_at", "open_time_epoch", "entry_ts", "opened_time"],
    "exit_time": ["exit_time", "exit_datetime", "close_time", "closed_at",
                  "close_time_epoch", "exit_ts", "closed_time"],
    "entry_price": ["entry_price", "entry", "open_price", "entry_px",
                    "open_px", "avg_entry"],
    "exit_price": ["exit_price", "exit", "close_price", "exit_px",
                   "close_px", "avg_exit"],
    "lots": ["lots", "lot", "volume", "size", "lot_size", "quantity",
             "contracts"],
    "pnl": ["pnl", "profit", "profit_loss", "realized_pnl", "net_pnl",
            "realized", "pnl_usd"],
    "entry_type": ["entry_type", "fill_type", "order_type", "entry_kind"],
}


def _norm(s: str) -> str:
    return str(s).strip().lower().replace(" ", "_").replace("-", "_")


def _detect_columns(fieldnames: List[str]) -> Dict[str, str]:
    """Map canonical field -> actual column name."""
    lookup = {_norm(f): f for f in fieldnames}
    found: Dict[str, str] = {}
    for canon, aliases in _COL_ALIASES.items():
        for a in aliases:
            if _norm(a) in lookup:
                found[canon] = lookup[_norm(a)]
                break
    return found


# --------------------------------------------------------------------------
# Time parsing
# --------------------------------------------------------------------------
def parse_time(val: Any) -> float:
    """Parse ISO / epoch / numeric time to epoch seconds."""
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    # epoch seconds (10 or 13 digits)
    if s.isdigit() and len(s) in (10, 13):
        v = float(s)
        return v / 1000.0 if len(s) == 13 else v
    # ISO with T and Z
    import time as _time
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _time.mktime(_time.strptime(s, fmt))
        except ValueError:
            continue
    # try fromisoformat (handles offsets)
    try:
        import datetime as _dt
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------
# Trade record
# --------------------------------------------------------------------------
@dataclass
class Trade:
    symbol: str
    direction: str          # BUY | SELL
    entry_time: float       # epoch
    exit_time: float
    entry_price: float
    exit_price: float
    lots: float
    pnl: float
    broker: str = ""
    account: str = ""
    entry_type: str = ""

    @property
    def realised_return(self) -> float:
        """Signed price move over the holding period (exit - entry)."""
        return self.exit_price - self.entry_price


def _to_dir(v: Any) -> str:
    s = _norm(v)
    if s in ("buy", "long", "b", "1"):
        return "BUY"
    if s in ("sell", "short", "s", "0", "-1"):
        return "SELL"
    return "BUY" if str(v).strip().upper().startswith("B") else "SELL"


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load_trade_file(path: str) -> List[Trade]:
    """Load trades from a CSV or JSON file (auto-detected)."""
    trades: List[Trade] = []
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".csv", ".txt"):
            with open(path, newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                cols = _detect_columns(list(reader.fieldnames or []))
                for row in reader:
                    t = _trade_from_row(row, cols)
                    if t:
                        trades.append(t)
        elif ext in (".json", ""):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            for row in _flatten_json(data):
                cols = _detect_columns(list(row.keys()))
                t = _trade_from_row(row, cols)
                if t:
                    trades.append(t)
        else:
            # try CSV then JSON
            try:
                return load_trade_file(path.replace(ext, ".csv"))
            except Exception:
                return load_trade_file(path.replace(ext, ".json"))
    except Exception as exc:
        print(f"  ! could not read {path}: {exc}")
    return trades


def _flatten_json(data: Any) -> List[Dict[str, Any]]:
    """Flatten nested JSON (list of trades, or {account: [trades]})."""
    out: List[Dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                out.append(item)
    elif isinstance(data, dict):
        # could be {account: [trades]} or a single trade dict
        for k, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                for item in v:
                    merged = dict(item)
                    merged.setdefault("account", k)
                    out.append(merged)
            elif isinstance(v, dict):
                merged = dict(v)
                merged.setdefault("account", k)
                out.append(merged)
        # also a bare trade dict
        if not out and all(isinstance(v, (str, int, float))
                           for v in data.values()):
            out.append(data)
    return out


def _trade_from_row(row: Dict[str, Any],
                    cols: Dict[str, str]) -> Optional[Trade]:
    if "symbol" not in cols or "entry_time" not in cols:
        return None
    sym = str(row.get(cols["symbol"], "")).strip()
    if not sym:
        return None
    # normalise symbol to universe form if possible
    sym = _match_symbol(sym)
    ep = _to_float(row.get(cols.get("entry_price", ""), 0))
    xp = _to_float(row.get(cols.get("exit_price", ""), 0))
    pnl = _to_float(row.get(cols.get("pnl", ""), 0))
    # derive exit price from pnl if missing
    if xp == 0.0 and ep != 0.0:
        # pnl sign depends on direction; approximate exit price
        won = pnl > 0
        d = _to_dir(row.get(cols.get("direction", ""), "BUY"))
        # we can't know the exact move without stop distance, so leave
        # realised_return to be derived from pnl sign below
    et = parse_time(row.get(cols["entry_time"], ""))
    xt = parse_time(row.get(cols.get("exit_time", ""), ""))
    if et == 0.0:
        return None
    return Trade(
        symbol=sym,
        direction=_to_dir(row.get(cols.get("direction", ""), "BUY")),
        entry_time=et, exit_time=xt,
        entry_price=ep, exit_price=xp,
        lots=_to_float(row.get(cols.get("lots", ""), 0)),
        pnl=pnl,
        broker=str(row.get(cols.get("broker", ""), "")),
        account=str(row.get(cols.get("account", ""), "")),
        entry_type=str(row.get(cols.get("entry_type", ""), "")),
    )


def _match_symbol(s: str) -> str:
    """Map an arbitrary symbol string to the canonical universe form."""
    s = s.strip()
    if s in SYMBOL_UNIVERSE:
        return s
    # case-insensitive match
    low = s.lower()
    for sym in SYMBOL_UNIVERSE:
        if sym.lower() == low:
            return sym
    # partial: e.g. 'EURUSD' -> 'EURUSD.r'
    for sym in SYMBOL_UNIVERSE:
        base = sym.split(".")[0].lower()
        if low.startswith(base) or base.startswith(low):
            return sym
    return s  # keep as-is; will be skipped if not in universe


# --------------------------------------------------------------------------
# Market-data alignment
# --------------------------------------------------------------------------
class MarketAlign:
    """Maps trades to feature windows from market-data candles."""

    def __init__(self, data_dir: str, timeframe: str = "M5"):
        self.loader = MarketDataLoader(data_dir)
        self.timeframe = timeframe
        self._by_symbol: Dict[str, List] = {}

    def _candles(self, symbol: str):
        if symbol not in self._by_symbol:
            c = self.loader.load_deriv(symbol=symbol,
                                       timeframe=self.timeframe)
            if not c:
                c, _ = self.loader.load_fp_markets(
                    symbol=symbol, timeframe=self.timeframe)
            # sort by open_time
            c = sorted(c, key=lambda x: parse_time(x.open_time))
            self._by_symbol[symbol] = c
        return self._by_symbol[symbol]

    def feature_window(self, symbol: str, entry_time: float
                       ) -> Optional[Tuple[List[float], List[float],
                                           List[float]]]:
        """Return (closes, highs, lows) up to the bar at/before entry_time."""
        candles = self._candles(symbol)
        if not candles:
            return None
        # find last candle whose open_time <= entry_time
        lo, hi, best = 0, len(candles) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if parse_time(candles[mid].open_time) <= entry_time:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if best < 30:
            return None
        cl = candles[: best + 1]
        closes = [x.close for x in cl]
        highs = [x.high for x in cl]
        lows = [x.low for x in cl]
        return closes, highs, lows


# --------------------------------------------------------------------------
# Training
# --------------------------------------------------------------------------
def train_online(brain, trades: List[Trade],
                 align: MarketAlign) -> Tuple[int, int]:
    """One online gradient step per trade from its realised move."""
    trained, skipped = 0, 0
    for t in trades:
        win = align.feature_window(t.symbol, t.entry_time)
        if win is None:
            skipped += 1
            continue
        closes, highs, lows = win
        # realised move: use exit-entry if known, else infer from pnl+dir
        if t.exit_price and t.entry_price:
            rr = t.realised_return
        else:
            # price-move sign: BUY won => up; SELL won => down
            up = (t.pnl > 0) if t.direction == "BUY" else (t.pnl < 0)
            rr = 1.0 if up else -1.0
        brain.online_learn(symbol=t.symbol, closes=closes, highs=highs,
                           lows=lows, realised_return=rr)
        trained += 1
    return trained, skipped


def train_offline(brain, trades: List[Trade], align: MarketAlign,
                  *, epochs: int = 30, batch_size: int = 32,
                  lr: float = 3e-3) -> Tuple[int, int, float]:
    """Collect (features, label) pairs and batch-train the ensemble."""
    X: List[np.ndarray] = []
    y: List[float] = []
    skipped = 0
    for t in trades:
        win = align.feature_window(t.symbol, t.entry_time)
        if win is None:
            skipped += 1
            continue
        closes, highs, lows = win
        rets = returns_from_closes(closes)
        feats = build_unified_feature_vector(
            symbol=t.symbol, closes=closes, highs=highs, lows=lows,
            regime="", universe=brain.symbol_ids, **rets)
        if t.exit_price and t.entry_price:
            rr = t.realised_return
        else:
            up = (t.pnl > 0) if t.direction == "BUY" else (t.pnl < 0)
            rr = 1.0 if up else -1.0
        X.append(np.asarray(feats, dtype=float))
        y.append(1.0 if rr > 0 else 0.0)
    if not X:
        return 0, skipped, 0.0
    X = np.asarray(X)
    y = np.asarray(y)
    rng = np.random.default_rng(0)
    last_loss = 0.0
    for epoch in range(epochs):
        order = rng.permutation(len(X))
        for start in range(0, len(X), batch_size):
            idx = order[start:start + batch_size]
            for m in brain.ensemble.members:
                if hasattr(m.model, "_online_opt"):
                    m.model._online_opt.lr = lr
                last_loss = m.model.online_step(X[idx], y[idx])
    return len(X), skipped, last_loss


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trades", required=True,
                    help="trade history file/glob or directory")
    ap.add_argument("--data", default="/tmp/market-data",
                    help="market data dir for feature alignment")
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--mode", choices=["online", "offline"],
                    default="online")
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--in", dest="in_dir", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    # collect trade files
    paths: List[str] = []
    if os.path.isdir(args.trades):
        for ext in ("*.csv", "*.json", "*.txt"):
            paths.extend(glob.glob(os.path.join(args.trades, ext)))
    else:
        paths.extend(glob.glob(args.trades))
        if not paths and os.path.isfile(args.trades):
            paths.append(args.trades)
    if not paths:
        print(f"ERROR: no trade files found at {args.trades}")
        return 1
    print(f"Found {len(paths)} trade file(s).")

    all_trades: List[Trade] = []
    for p in sorted(paths):
        ts = load_trade_file(p)
        print(f"  {os.path.basename(p):30} trades={len(ts)}")
        all_trades.extend(ts)
    # keep only symbols in the universe
    all_trades = [t for t in all_trades if t.symbol in SYMBOL_UNIVERSE]
    print(f"\nTotal trades (universe symbols): {len(all_trades)}")
    if not all_trades:
        print("ERROR: no trades mapped to the symbol universe.")
        return 1

    in_dir = args.in_dir or unified_brain_dir()
    out_dir = args.out or in_dir
    brain = load_unified(in_dir)
    if brain is None:
        print(f"ERROR: no unified brain at {in_dir}. "
              f"Run scripts/train_unified.py first.")
        return 1
    print(f"Loaded unified brain: {len(brain.ensemble.members)} members, "
          f"{len(brain.symbol_ids)} symbols | "
          f"online_updates so far={brain.online_updates}")
    brain.nn_online_lr = args.lr

    align = MarketAlign(args.data, args.timeframe)
    t0 = time.time()
    if args.mode == "online":
        trained, skipped = train_online(brain, all_trades, align)
        print(f"\nOnline trade training: {trained} trades trained, "
              f"{skipped} skipped (no market data / too early)")
        print(f"  brain.online_updates now = {brain.online_updates}")
    else:
        n, skipped, loss = train_offline(
            brain, all_trades, align, epochs=args.epochs, lr=args.lr)
        print(f"\nOffline batch training: {n} trades trained, "
              f"{skipped} skipped | final_loss={loss:.4f}")
        brain.online_updates += n
    dt = time.time() - t0
    print(f"done in {dt:.1f}s")

    os.makedirs(out_dir, exist_ok=True)
    path = save_unified(out_dir, brain)
    print(f"Refined brain saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
