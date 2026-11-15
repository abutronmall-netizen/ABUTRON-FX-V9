"""Market-data loader for backtesting / forward testing.

Loads the public `abutronmall-netizen/market-data` datasets (Deriv synthetic
indices + FP Markets FX candles/quotes) into the platform's Candle / Quote
models so the NexusBrain + sniper + trailing + lot-sizing pipeline can be
driven over historical bars.

Data is gzip CSV. Deriv candles are split into ordered byte-chunk parts that
must be concatenated before decompression; FP Markets ships single files.
Timestamps are unix epoch UTC and are converted to ISO-UTC strings matching
the platform's Candle.open_time / Quote.observed_at convention.
"""
from __future__ import annotations

import gzip
import io
import os
import time
from typing import Dict, List, Optional, Tuple

from ..feeds.feed_quality import Candle, Quote, _tf_seconds


def _epoch_to_iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(epoch)))


def _parse_float(x: str) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _decimals(x: str) -> int:
    """Number of decimal places in a price string (e.g. '0.70207' -> 5)."""
    x = x.strip()
    if "." in x:
        return len(x.split(".")[1])
    return 0


class MarketDataLoader:
    """Loads broker market datasets into Candle / Quote lists."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir

    # ------------------------------------------------------------------
    # Deriv
    # ------------------------------------------------------------------
    def load_deriv(self, *, symbol: Optional[str] = None,
                   timeframe: Optional[str] = None) -> List[Candle]:
        d = os.path.join(self.data_dir, "Deriv")
        if not os.path.isdir(d):
            return []
        gz_path = self._assemble_deriv(d)
        candles = self._read_candles_gz(gz_path, broker="deriv",
                                        symbol_col="symbol",
                                        use_broker_symbol=False)
        return self._filter(candles, symbol, timeframe)

    def _assemble_deriv(self, d: str) -> Optional[str]:
        """Concatenate the ordered part-* chunks into one gzip stream.

        Writes to a per-process temp file and atomically replaces the
        assembled file, so concurrent workers never read a torn/partial
        stream (which would raise gzip EOFError).
        """
        parts = sorted(f for f in os.listdir(d) if ".part-" in f)
        out = os.path.join(d, "_assembled.csv.gz")
        # skip if a complete assembled file already exists (cache)
        if os.path.exists(out):
            return out
        tmp = os.path.join(d, f"._assembled.{os.getpid()}.tmp.gz")
        try:
            with open(tmp, "wb") as w:
                for p in parts:
                    with open(os.path.join(d, p), "rb") as r:
                        w.write(r.read())
            os.replace(tmp, out)  # atomic on POSIX
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return out

    # ------------------------------------------------------------------
    # FP Markets
    # ------------------------------------------------------------------
    def load_fp_markets(self, *, symbol: Optional[str] = None,
                        timeframe: Optional[str] = None
                        ) -> Tuple[List[Candle], List[Quote]]:
        d = os.path.join(self.data_dir, "FP-Markets")
        if not os.path.isdir(d):
            return [], []
        candles = self._read_candles_gz(
            os.path.join(d, "candles.csv.gz"), broker="fpmarkets",
            symbol_col="broker_symbol", use_broker_symbol=True)
        candles = self._filter(candles, symbol, timeframe)
        quotes = self._read_quotes_gz(os.path.join(d, "quotes.csv.gz"))
        return candles, quotes

    # ------------------------------------------------------------------
    # Generic CSV-gz readers
    # ------------------------------------------------------------------
    def _read_candles_gz(self, path: str, *, broker: str, symbol_col: str,
                         use_broker_symbol: bool) -> List[Candle]:
        candles: List[Candle] = []
        seen = set()
        max_dec: Dict[Tuple[str, str], int] = {}
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            header = fh.readline().strip().split(",")
            idx = {c: i for i, c in enumerate(header)}
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                f = line.split(",")
                sym_raw = f[idx["symbol"]]
                broker_sym = f[idx.get("broker_symbol", idx["symbol"])]
                symbol = broker_sym if use_broker_symbol else sym_raw
                tf = f[idx["timeframe"]]
                ts = _parse_float(f[idx["start_time_utc"]])
                ot = _epoch_to_iso(ts)
                # track price precision to derive the instrument's point size
                for pcol in ("open", "high", "low", "close"):
                    d = _decimals(f[idx[pcol]])
                    if d > max_dec.get((symbol, tf), 0):
                        max_dec[(symbol, tf)] = d
                try:
                    candle = Candle(
                        symbol=symbol, timeframe=tf,
                        open_time=ot,
                        close_time=_epoch_to_iso(ts + _tf_seconds(tf)),
                        open=_parse_float(f[idx["open"]]),
                        high=_parse_float(f[idx["high"]]),
                        low=_parse_float(f[idx["low"]]),
                        close=_parse_float(f[idx["close"]]),
                        volume=0.0, tick_size=0.0, point=0.0, closed=True,
                    )
                except KeyError:
                    continue
                key = (symbol, tf, ot)
                if key in seen:  # dedupe (FP candles ship duplicated)
                    continue
                seen.add(key)
                candles.append(candle)
        # assign point size = smallest price increment (10^-max_decimals)
        for c in candles:
            d = max_dec.get((c.symbol, c.timeframe), 0)
            c.point = 10.0 ** (-d) if d else 0.0
        candles.sort(key=lambda c: (c.symbol, c.timeframe, c.open_time))
        return candles

    def _read_quotes_gz(self, path: str) -> List[Quote]:
        quotes: List[Quote] = []
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            header = fh.readline().strip().split(",")
            idx = {c: i for i, c in enumerate(header)}
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                f = line.split(",")
                bid = _parse_float(f[idx["bid"]])
                ask = _parse_float(f[idx["ask"]])
                spread = abs(ask - bid)
                quotes.append(Quote(
                    symbol=f[idx.get("broker_symbol", idx["symbol"])],
                    bid=bid, ask=ask, spread=spread,
                    tick_size=0.0, point=0.0, observed_at=_epoch_to_iso(
                        _parse_float(f[idx["observed_utc"]]))))
        quotes.sort(key=lambda q: (q.symbol, q.observed_at))
        return quotes

    # ------------------------------------------------------------------
    def _filter(self, candles: List[Candle], symbol: Optional[str],
                timeframe: Optional[str]) -> List[Candle]:
        out = candles
        if symbol:
            out = [c for c in out if c.symbol == symbol]
        if timeframe:
            out = [c for c in out if c.timeframe == timeframe]
        return out

    # ------------------------------------------------------------------
    def discover(self) -> Dict[str, List[Dict[str, str]]]:
        """Return a summary of available symbols/timeframes per broker."""
        summary: Dict[str, List[Dict[str, str]]] = {}
        for broker, loader in (("deriv", self.load_deriv),
                               ("fpmarkets", lambda **kw:
                                self.load_fp_markets(**kw)[0])):
            try:
                candles = loader()
            except Exception:
                candles = []
            seen = set()
            for c in candles:
                seen.add((c.symbol, c.timeframe))
            summary[broker] = [
                {"symbol": s, "timeframe": t} for s, t in sorted(seen)]
        return summary


def load_dataset(data_dir: str, *, broker: str = "deriv",
                 symbol: Optional[str] = None,
                 timeframe: Optional[str] = None
                 ) -> Tuple[List[Candle], List[Quote]]:
    """Convenience: load a single broker's candles + quotes."""
    loader = MarketDataLoader(data_dir)
    if broker in ("deriv",):
        return loader.load_deriv(symbol=symbol, timeframe=timeframe), []
    return loader.load_fp_markets(symbol=symbol, timeframe=timeframe)
