"""Shared, thread-safe rate limiter for the SpaceTraders API.

The live API advertises (via response headers) a limit of 2 requests/second
with a burst bucket of 30 (scoped per IP address):

    x-ratelimit-limit-per-second: 2
    x-ratelimit-limit-burst: 30

Because the TUI runs several bot threads against a single IP, every request in
the process must pass through one shared limiter or the whole fleet gets 429'd.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """A token bucket. ``acquire()`` blocks until a token is available.

    Tokens refill continuously at ``rate`` per second up to ``capacity`` (the
    burst allowance), so a mostly-idle client may fire a short burst but the
    sustained rate can never exceed ``rate``.
    """

    def __init__(self, rate: float = 2.0, capacity: int = 30):
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now

    def acquire(self) -> None:
        # Loop because, after sleeping, another thread may have drained the
        # bucket we were waiting on.
        while True:
            with self._lock:
                now = time.monotonic()
                self._refill(now)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(wait)


# Process-wide shared instance. Import and use this rather than constructing
# per-client limiters, so all threads share one bucket.
LIMITER = RateLimiter()
