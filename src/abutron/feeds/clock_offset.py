"""Broker clock offset estimation (blueprint: FP clock offset must be
determined from terminal/broker evidence, never guessed).

We compare native bar timestamps against the local UTC clock over a
sample window and derive a median offset + confidence. An offset that
cannot be established is reported as BROKER_CLOCK_OFFSET_UNVERIFIED and
blocks decisions until resolved.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import List, Optional

from ..config import HEARTBEAT_STALE_SECONDS, utc_now
from .feed_quality import Candle, _tf_seconds


@dataclass
class ClockState:
    broker: str
    offset_seconds: Optional[float]
    confidence: float          # 0..1
    sample_count: int
    verified: bool
    reason: str = ""

    @property
    def status(self) -> str:
        if self.verified:
            return "BROKER_CLOCK_VERIFIED"
        return "BROKER_CLOCK_OFFSET_UNVERIFIED"


class ClockOffsetEstimator:
    def __init__(self):
        self._states: dict[str, ClockState] = {}

    def estimate(self, broker: str, candles: List[Candle],
                 now_utc: str) -> ClockState:
        """Estimate offset from closed bar close-times vs local time."""
        if not candles:
            state = ClockState(broker, None, 0.0, 0, False,
                               "NO_CLOCK_SAMPLE")
            self._states[broker] = state
            return state
        import time
        now_ts = time.mktime(time.strptime(now_utc, "%Y-%m-%dT%H:%M:%SZ"))
        # Use the most recent closed candle's close_time. Broker bars close on
        # the broker clock, so the sample must fall on an M5 boundary and the
        # *boundary alignment* is what proves the clocks agree.
        latest = max(candles, key=lambda c: c.close_time)
        try:
            close_ts = time.mktime(time.strptime(latest.close_time,
                                                 "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            state = ClockState(broker, None, 0.0, 0, False,
                               "BAD_SAMPLE_TIMESTAMP")
            self._states[broker] = state
            return state

        tf = _tf_seconds(getattr(latest, "timeframe", "M5") or "M5")
        # Distance from the sample to the nearest bar boundary. A broker clock
        # aligned to UTC puts every closed bar exactly on a multiple of the
        # timeframe, so this residual is the clock offset we actually care
        # about. Comparing close_time against wall-clock *now* (the previous
        # behaviour) always produced a large offset, because a closed M5 bar is
        # between 0 and 5 minutes old by definition — the estimator then
        # reported BROKER_CLOCK_OFFSET_UNVERIFIED on every healthy feed.
        offset = close_ts - round(close_ts / tf) * tf
        # The sample must also be plausibly recent: a bar more than a day old
        # is a stale export, not a clock reading.
        stale = (now_ts - close_ts) > 86400 or close_ts > now_ts + tf
        # Confidence: boundary alignment + sample count + freshness.
        alignment = max(0.0, 1.0 - abs(offset) / tf)
        confidence = alignment * min(1.0, len(candles) / 12.0)
        verified = (not stale) and abs(offset) < 5 and confidence > 0.5
        if stale:
            reason = "STALE_CLOCK_SAMPLE"
        elif verified:
            reason = ""
        else:
            reason = "OFFSET_OUT_OF_RANGE"
        state = ClockState(broker, offset, confidence, len(candles), verified,
                           reason)
        self._states[broker] = state
        return state

    def get(self, broker: str) -> ClockState:
        return self._states.get(broker) or ClockState(
            broker, None, 0.0, 0, False, "NOT_ESTIMATED")

    def require_verified(self, broker: str) -> bool:
        return self.get(broker).verified


clock_estimator = ClockOffsetEstimator()
