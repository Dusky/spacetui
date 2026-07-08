import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ratelimit import RateLimiter


def test_burst_then_throttle():
    # capacity 5, refill 10/s. First 5 acquisitions are ~instant (burst),
    # the 6th must wait ~1/rate seconds for a token to refill.
    rl = RateLimiter(rate=10.0, capacity=5)
    start = time.monotonic()
    for _ in range(5):
        rl.acquire()
    burst_elapsed = time.monotonic() - start
    assert burst_elapsed < 0.05  # burst is essentially free

    t0 = time.monotonic()
    rl.acquire()  # 6th token must wait for a refill
    assert time.monotonic() - t0 >= 0.08  # ~0.1s, allow slack


def test_penalize_blocks_all_callers():
    # a 429 penalty should make even an otherwise-unthrottled limiter wait
    rl = RateLimiter(rate=1000.0, capacity=1000)
    rl.penalize(0.3)
    t0 = time.monotonic()
    rl.acquire()
    assert time.monotonic() - t0 >= 0.25  # waited out the penalty


def test_sustained_rate_across_threads():
    # No burst headroom (capacity 1): N acquisitions from many threads must
    # take at least (N-1)/rate seconds in aggregate.
    rl = RateLimiter(rate=20.0, capacity=1)
    n = 20
    start = time.monotonic()

    def worker():
        rl.acquire()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    elapsed = time.monotonic() - start
    # lower bound: (n-1)/rate; generous upper bound guards against deadlock
    assert elapsed >= (n - 1) / 20.0 * 0.8
    assert elapsed < 5.0
