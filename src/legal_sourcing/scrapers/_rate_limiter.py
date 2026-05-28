"""Thread-safe burstable token-bucket rate limiter.

Two parameters:

* ``rps`` — sustained tokens-per-second refill rate.
* ``burst`` — bucket capacity (max tokens). Defaults to ``max(1, rps)``,
  giving roughly one second of full-rate burst headroom after the
  limiter has been idle.

Behavior:

* If the bucket has ≥ 1 token, ``acquire()`` consumes one and returns
  immediately. This is the burst case — useful after idle periods.
* If the bucket is depleted, the caller sleeps ``(1 - tokens) /
  refill_rate`` seconds OUTSIDE the lock, then retries. Multiple
  threads block in parallel rather than serializing their sleeps
  (the older "sleep under the lock" design was strictly-paced and
  not burstable).

Trade-off vs strict pacing: short bursts can exceed the sustained
rate when the bucket has been refilling — that's the intent. Long
runs converge to the sustained rate.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, rps: float, burst: float | None = None):
        if rps <= 0:
            raise ValueError(f"rps must be > 0, got {rps}")
        self._refill_rate = float(rps)
        # Default burst = one second's worth of full-rate work, with a
        # floor of 1.0 so even sub-1-RPS configs allow single requests.
        self._capacity = float(burst) if burst is not None else max(1.0, float(rps))
        if self._capacity < 1.0:
            raise ValueError(f"burst capacity must be >= 1, got {self._capacity}")

        self._tokens = self._capacity  # start full → first burst is allowed
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    @property
    def rps(self) -> float:
        return self._refill_rate

    @property
    def capacity(self) -> float:
        return self._capacity

    def acquire(self) -> None:
        """Block the calling thread until a token is available, then
        consume it. Sleeps happen OUTSIDE the lock so concurrent
        acquirers can wait in parallel.
        """
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                if elapsed > 0:
                    self._tokens = min(
                        self._capacity, self._tokens + elapsed * self._refill_rate
                    )
                    self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                # Compute wait time inside the lock, then release before sleeping.
                needed = 1.0 - self._tokens
                wait = needed / self._refill_rate

            time.sleep(wait)
            # Loop — re-check under the lock in case another thread
            # consumed the token we were waiting for (thundering-herd).
