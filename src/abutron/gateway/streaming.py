"""Thread-safe pub/sub hub bridging worker threads and asyncio WebSockets.

Transports publish from their own threads (a Wine bridge, a simulator
timer). WebSocket handlers consume from an ``asyncio.Queue``. The hub is
the seam: ``publish`` is safe from any thread and never blocks, and a slow
consumer loses its oldest event instead of stalling the producer.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict, Optional, Tuple

MAX_QUEUE = 512


class StreamHub:
    def __init__(self, max_queue: int = MAX_QUEUE):
        self._max_queue = max_queue
        self._subs: Dict[int, Tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = {}
        self._next_id = 0
        self._lock = threading.Lock()
        self._seq = 0
        self._published = 0
        self._dropped = 0
        self._started = time.time()

    # ------------------------------------------------------------------
    def subscribe(self, loop: Optional[asyncio.AbstractEventLoop] = None
                  ) -> Tuple[int, "asyncio.Queue[Dict[str, Any]]"]:
        loop = loop or asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        with self._lock:
            self._next_id += 1
            sub_id = self._next_id
            self._subs[sub_id] = (loop, queue)
        return sub_id, queue

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            self._subs.pop(sub_id, None)

    def publish(self, event: Dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            self._published += 1
            envelope = dict(event)
            envelope.setdefault("sequence", self._seq)
            envelope.setdefault("timestamp",
                                time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime()))
            subscribers = list(self._subs.values())
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(self._deliver, queue, envelope)
            except RuntimeError:
                # Loop closed between snapshot and call — subscriber is gone.
                continue

    def _deliver(self, queue: "asyncio.Queue[Dict[str, Any]]",
                 event: Dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            with self._lock:
                self._dropped += 1
            try:
                queue.get_nowait()
                queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "subscribers": len(self._subs),
                "published": self._published,
                "dropped": self._dropped,
                "uptime_s": round(time.time() - self._started, 1),
            }
