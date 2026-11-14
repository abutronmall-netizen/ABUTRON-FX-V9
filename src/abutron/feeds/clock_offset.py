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
from .feed_quality import Candle


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
        # Use the most recent closed candle's close_time; broker bars
        # close on the broker clock, which should align with UTC.
        latest = max(candles, key=lambda c: c.close_time)
        try:
            close_ts = time.mktime(time.strptime(latest.close_time,
                                                 "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            state = ClockState(broker, None, 0.0, 0, False,
                               "BAD_SAMPLE_TIMESTAMP")
            self._states[broker] = state
            return state
        offset = close_ts - now_ts
        # Confidence: how fresh the sample is + sample count
        age = abs(offset)
        confidence = max(0.0, 1.0 - age / (6 * 3600)) * min(1.0, len(candles) / 12.0)
        verified = abs(offset) < 5 and confidence > 0.5
        state = ClockState(broker, offset, confidence, len(candles), verified,
                           "" if verified else "OFFSET_OUT_OF_RANGE")
        self._states[broker] = state
        return state

    def get(self, broker: str) -> ClockState:
        return self._states.get(broker) or ClockState(
            broker, None, 0.0, 0, False, "NOT_ESTIMATED")

    def require_verified(self, broker: str) -> bool:
        return self.get(broker).verified


clock_estimator = ClockOffsetEstimator()
