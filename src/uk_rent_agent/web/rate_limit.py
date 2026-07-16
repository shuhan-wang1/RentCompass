from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowRateLimiter:
    """Small process-local limiter used as a safe default at the web boundary.

    Deployments with multiple replicas should additionally enforce equivalent limits
    at the reverse proxy or a shared Redis-backed limiter.
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = self._clock()
        with self._lock:
            bucket = self._events.setdefault(key, deque())
            cutoff = now - window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + window_seconds - now) + 1)
                return False, retry_after
            bucket.append(now)
            return True, 0
