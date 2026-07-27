"""Rate-limit interface (Phase 3 requirement 9: "even if the initial implementation is
simple").

`InMemoryFixedWindowRateLimiter` is genuinely simple — a per-process, per-key fixed window
counter. It is explicitly **not** safe for a multi-process or multi-instance deployment (each
process has its own counters, so the effective limit multiplies with instance count); a
production deployment should implement `RateLimiter` against a shared store (e.g. Redis) using
the same interface. That swap is out of scope for this repository at this stage — see TASKS.md.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Protocol


class RateLimitExceededError(Exception):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"Rate limit exceeded for {key!r}")


class RateLimiter(Protocol):
    def check(self, key: str) -> None:
        """Raises RateLimitExceededError if `key` has exceeded its limit; otherwise records
        one more request against it and returns."""
        ...


class InMemoryFixedWindowRateLimiter:
    def __init__(self, *, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._windows: dict[str, tuple[float, int]] = defaultdict(lambda: (0.0, 0))

    def check(self, key: str) -> None:
        now = time.monotonic()
        window_start, count = self._windows[key]
        if now - window_start >= self._window_seconds:
            window_start, count = now, 0
        count += 1
        self._windows[key] = (window_start, count)
        if count > self._max_requests:
            raise RateLimitExceededError(key)
