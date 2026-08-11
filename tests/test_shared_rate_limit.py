import threading

from uk_rent_agent.web.rate_limit import SlidingWindowRateLimiter


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_quota_is_shared_across_process_instances_and_restarts(tmp_path):
    clock = Clock()
    path = tmp_path / "rate_limits.sqlite3"
    blue = SlidingWindowRateLimiter(clock=clock, db_path=path)
    green = SlidingWindowRateLimiter(clock=clock, db_path=path)

    assert blue.allow("/api/alex:ip:203.0.113.7", limit=2, window_seconds=60)[0]
    assert green.allow("/api/alex:ip:203.0.113.7", limit=2, window_seconds=60)[0]
    assert blue.allow("/api/alex:ip:203.0.113.7", limit=2, window_seconds=60) == (False, 61)

    restarted = SlidingWindowRateLimiter(clock=clock, db_path=path)
    assert restarted.allow(
        "/api/alex:ip:203.0.113.7", limit=2, window_seconds=60
    )[0] is False
    clock.now += 61
    assert restarted.allow(
        "/api/alex:ip:203.0.113.7", limit=2, window_seconds=60
    )[0] is True


def test_concurrent_pools_cannot_double_the_quota(tmp_path):
    clock = Clock()
    path = tmp_path / "rate_limits.sqlite3"
    pools = [
        SlidingWindowRateLimiter(clock=clock, db_path=path),
        SlidingWindowRateLimiter(clock=clock, db_path=path),
    ]
    barrier = threading.Barrier(20)
    decisions = []
    lock = threading.Lock()

    def call(index):
        barrier.wait()
        allowed, _ = pools[index % 2].allow(
            "/api/alex:ip:198.51.100.4", limit=5, window_seconds=60
        )
        with lock:
            decisions.append(allowed)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(decisions) == 20
    assert sum(decisions) == 5


def test_shared_ledger_never_stores_raw_subject(tmp_path):
    path = tmp_path / "rate_limits.sqlite3"
    limiter = SlidingWindowRateLimiter(db_path=path)
    raw = "/api/alex:ip:203.0.113.99"
    assert limiter.allow(raw, limit=2, window_seconds=60)[0]
    assert raw.encode() not in path.read_bytes()
