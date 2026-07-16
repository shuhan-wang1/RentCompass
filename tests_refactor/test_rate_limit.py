from uk_rent_agent.web.rate_limit import SlidingWindowRateLimiter


def test_sliding_window_allows_then_throttles_and_recovers():
    now = [100.0]
    limiter = SlidingWindowRateLimiter(clock=lambda: now[0])

    assert limiter.allow("alex:user", limit=2, window_seconds=10) == (True, 0)
    assert limiter.allow("alex:user", limit=2, window_seconds=10) == (True, 0)
    allowed, retry_after = limiter.allow("alex:user", limit=2, window_seconds=10)
    assert not allowed
    assert retry_after == 11

    now[0] = 110.0
    assert limiter.allow("alex:user", limit=2, window_seconds=10) == (True, 0)


def test_sliding_window_keeps_subjects_isolated():
    limiter = SlidingWindowRateLimiter(clock=lambda: 1.0)
    assert limiter.allow("alex:user-a", limit=1, window_seconds=60) == (True, 0)
    assert limiter.allow("alex:user-b", limit=1, window_seconds=60) == (True, 0)
