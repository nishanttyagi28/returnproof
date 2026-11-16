"""In-memory sliding-window rate limiting for authentication endpoints."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        bucket = self._events[key]
        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return bucket

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._prune(key, now)
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            bucket = self._prune(key, now)
            if len(bucket) < self.limit:
                return 0
            return max(1, int(self.window_seconds - (now - bucket[0])) + 1)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
