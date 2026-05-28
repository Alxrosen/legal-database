"""Thread-safe rate limiter.

Implementation: maintain a `next_allowed_at` timestamp under a Lock.
Each call to `acquire()` blocks until that timestamp, then advances it
by 1/RPS so the NEXT caller is gated. This naturally handles
multi-threaded callers — they queue at the lock and serialize their
sleeps.

Trade-off vs token bucket: this gives strict pacing (exactly 1/RPS
between starts) rather than burstable capacity. For polite scraping
that's what we want.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, rps: float):
        if rps <= 0:
            raise ValueError(f"rps must be > 0, got {rps}")
        self._interval = 1.0 / float(rps)
        self._next_allowed_at = 0.0
        self._lock = threading.Lock()

    @property
    def rps(self) -> float:
        return 1.0 / self._interval

    def acquire(self) -> None:
        """Block the calling thread until the next request slot opens."""
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed_at:
                wait = self._next_allowed_at - now
                # Release the lock during the sleep would let multiple
                # callers race; we WANT serialization, so sleep under the
                # lock. Other threads queue behind us in lock-acquire order.
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed_at = now + self._interval
