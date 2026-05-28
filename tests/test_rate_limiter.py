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


def test_effective_rps_with_no_ramp_returns_target():
    rl = RateLimiter(rps=15.0)
    assert rl.effective_rps() == 15.0


def test_effective_rps_ramps_linearly():
    rl = RateLimiter(rps=15.0, initial_rps=3.0, ramp_seconds=10.0, burst=1.0)
    start = rl._start_time
    # At t=0, effective rate = initial.
    assert rl.effective_rps(start) == pytest.approx(3.0)
    # Midpoint: halfway between initial and target.
    assert rl.effective_rps(start + 5.0) == pytest.approx(9.0)
    # End of ramp: target.
    assert rl.effective_rps(start + 10.0) == pytest.approx(15.0)
    # Past the ramp: still target.
    assert rl.effective_rps(start + 60.0) == pytest.approx(15.0)


def test_ramp_slows_initial_acquires():
    """Under ramp, drained-bucket waits are longer at the start than at
    the end of the ramp."""
    rl = RateLimiter(rps=50.0, initial_rps=5.0, ramp_seconds=2.0, burst=1.0)
    rl.acquire()  # drain the initial token
    # At t≈0, refill rate is ~5/s -> ~0.2s to refill one token.
    t0 = time.monotonic()
    rl.acquire()
    initial_wait = time.monotonic() - t0
    assert 0.1 < initial_wait < 0.4, f"initial wait was {initial_wait:.3f}s"


def test_invalid_ramp_args_rejected():
    with pytest.raises(ValueError):
        RateLimiter(rps=10, initial_rps=0)
    with pytest.raises(ValueError):
        RateLimiter(rps=10, initial_rps=-1)
    with pytest.raises(ValueError):
        RateLimiter(rps=10, ramp_seconds=-1)
