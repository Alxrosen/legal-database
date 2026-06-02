"""Thread-safe burstable token-bucket rate limiter with optional ramp-up.

Three parameters that callers tune:

* ``rps`` — target / steady-state tokens-per-second refill rate.
* ``burst`` — bucket capacity (max tokens). Defaults to ``max(1, rps)``,
  giving roughly one second of full-rate burst headroom after the
  limiter has been idle.
* ``initial_rps`` + ``ramp_seconds`` — optional polite-startup ramp.
  When set, the effective refill rate begins at ``initial_rps`` and
  linearly interpolates to ``rps`` over ``ramp_seconds``. After the
  ramp window the limiter runs at the target rate forever.

Behavior:

* If the bucket has ≥ 1 token, ``acquire()`` consumes one and returns
  immediately. This is the burst case — useful after idle periods.
* If the bucket is depleted, the caller sleeps
  ``(1 - tokens) / current_rate`` seconds OUTSIDE the lock, then
  retries. Multiple threads block in parallel rather than serializing
  their sleeps.

The ramp is time-based (not request-count-based) so a long-running
scrape with idle periods doesn't accidentally reset its politeness
budget on every resume.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(
        self,
        rps: float,
        burst: float | None = None,
        *,
        initial_rps: float | None = None,
        ramp_seconds: float = 0.0,
    ):
        if rps <= 0:
            raise ValueError(f"rps must be > 0, got {rps}")
        if initial_rps is None:
            initial_rps = rps  # no ramp
        if initial_rps <= 0:
            raise ValueError(f"initial_rps must be > 0, got {initial_rps}")
        if ramp_seconds < 0:
            raise ValueError(f"ramp_seconds must be >= 0, got {ramp_seconds}")

        self._target_rps = float(rps)
        self._initial_rps = float(initial_rps)
        self._ramp_seconds = float(ramp_seconds)

        # Default burst = one second's worth of full-rate work, with a
        # floor of 1.0 so even sub-1-RPS configs allow single requests.
        self._capacity = float(burst) if burst is not None else max(1.0, float(rps))
        if self._capacity < 1.0:
            raise ValueError(f"burst capacity must be >= 1, got {self._capacity}")

        self._start_time = time.monotonic()
        self._tokens = self._capacity  # start full → first burst is allowed
        self._last_refill = self._start_time
        self._lock = threading.Lock()

    @property
    def rps(self) -> float:
        """Target (steady-state) RPS. After ramp_seconds, the actual rate."""
        return self._target_rps

    @property
    def capacity(self) -> float:
        return self._capacity

    def effective_rps(self, now: float | None = None) -> float:
        """Refill rate currently in effect, accounting for the ramp."""
        if self._ramp_seconds <= 0:
            return self._target_rps
        if now is None:
            now = time.monotonic()
        elapsed = now - self._start_time
        if elapsed >= self._ramp_seconds:
            return self._target_rps
        progress = elapsed / self._ramp_seconds
        return self._initial_rps + (self._target_rps - self._initial_rps) * progress

    def acquire(self) -> None:
        """Block the calling thread until a token is available, then
        consume it. Sleeps happen OUTSIDE the lock so concurrent
        acquirers can wait in parallel.
        """
        while True:
            with self._lock:
                now = time.monotonic()
                rate = self.effective_rps(now)
                elapsed = now - self._last_refill
                if elapsed > 0:
                    # Approximation during the ramp: use the current
                    # rate for the elapsed window. Error is bounded by
                    # the ramp duration and is invisible at our scale.
                    self._tokens = min(self._capacity, self._tokens + elapsed * rate)
                    self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                # Compute wait time inside the lock, then release before sleeping.
                needed = 1.0 - self._tokens
                wait = needed / rate

            time.sleep(wait)
            # Loop — re-check under the lock in case another thread
            # consumed the token we were waiting for (thundering-herd).
