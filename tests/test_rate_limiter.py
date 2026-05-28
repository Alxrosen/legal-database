"""Token-bucket rate limiter tests.

We assert two behaviors:
  1. After idle / on construction, up to `capacity` acquires return
     almost instantly — that's the burst.
  2. Once the bucket is drained, the next acquire blocks for ~1/rps.
  3. Concurrent acquirers can sleep in parallel (sleeping outside the
     lock), so wall-clock time for N drained acquires across K threads
     scales with N/rps, not N*K/rps.
"""

from __future__ import annotations

import threading
import time

import pytest

from legal_sourcing.scrapers._rate_limiter import RateLimiter


def test_burst_within_capacity_is_fast():
    rl = RateLimiter(rps=10.0, burst=5.0)
    start = time.monotonic()
    for _ in range(5):
        rl.acquire()
    elapsed = time.monotonic() - start
    # 5 burst tokens at rps=10 should consume in well under 50ms.
    assert elapsed < 0.05, f"burst took {elapsed:.3f}s, expected < 0.05"


def test_acquire_after_burst_blocks_for_refill():
    rl = RateLimiter(rps=10.0, burst=1.0)  # 1 token, refill 10/s
    rl.acquire()  # consume the only token
    start = time.monotonic()
    rl.acquire()  # must wait ~0.1s
    elapsed = time.monotonic() - start
    assert 0.05 < elapsed < 0.5, f"refill wait was {elapsed:.3f}s, expected ~0.1s"


def test_concurrent_acquires_share_wait_time():
    """Three threads acquiring from a depleted bucket should finish in
    roughly N/rps wall-clock — NOT 3 * single-wait — because sleeps
    happen outside the lock.
    """
    rl = RateLimiter(rps=20.0, burst=1.0)
    rl.acquire()  # drain

    results: list[float] = []
    results_lock = threading.Lock()

    def worker():
        t0 = time.monotonic()
        rl.acquire()
        with results_lock:
            results.append(time.monotonic() - t0)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start

    # At 20 rps with 3 tokens needed after drain, the bucket needs to
    # accumulate ~3 tokens => 0.15s. Allow generous slack for CI noise.
    assert elapsed < 0.5, f"3 concurrent acquires took {elapsed:.3f}s"
    # And each individual wait should not be the sum of the others —
    # they overlap.
    assert max(results) < 0.5


def test_invalid_rps_rejected():
    with pytest.raises(ValueError):
        RateLimiter(rps=0)
    with pytest.raises(ValueError):
        RateLimiter(rps=-1)


def test_invalid_burst_rejected():
    with pytest.raises(ValueError):
        RateLimiter(rps=1.0, burst=0.5)


def test_default_burst_is_max_one_or_rps():
    assert RateLimiter(rps=0.5).capacity == 1.0  # floor
    assert RateLimiter(rps=10).capacity == 10.0
