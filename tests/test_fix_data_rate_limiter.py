"""
Shared detail-GET rate limiter + per-thread sessions (Fix 4).

The enrichment layer fires several fetch_listing_details calls concurrently. The
module-level limiter must guarantee >= _MIN_DETAIL_INTERVAL_S between detail GETs
across ALL threads (not per-thread sleeps that overlap), and each thread must get
its OWN requests.Session.
"""

import threading
import time
import types

import core.scraping.onthemarket as om


# --------------------------------------------------------------------------
# Deterministic spacing with a mocked clock.
# --------------------------------------------------------------------------
def test_rate_limiter_spaces_calls_with_mock_clock(monkeypatch):
    class Clock:
        def __init__(self):
            self.t = 1000.0

        def monotonic(self):
            return self.t

        def sleep(self, s):
            assert s >= 0
            self.t += s

        def time(self):
            return self.t

    clock = Clock()
    fake_time = types.SimpleNamespace(
        monotonic=clock.monotonic, sleep=clock.sleep, time=clock.time)
    monkeypatch.setattr(om, "time", fake_time)
    monkeypatch.setattr(om, "_LAST_DETAIL_TS", 0.0)
    monkeypatch.setattr(om, "_MIN_DETAIL_INTERVAL_S", 1.2)

    releases = []
    for _ in range(5):
        om._rate_limited_detail_wait()
        releases.append(clock.t)

    # First call fires immediately; each subsequent one is spaced >= 1.2s.
    for prev, cur in zip(releases, releases[1:]):
        assert round(cur - prev, 6) >= 1.2


# --------------------------------------------------------------------------
# Real concurrency: N threads calling the limiter are paced in aggregate.
# --------------------------------------------------------------------------
def test_rate_limiter_paces_concurrent_threads(monkeypatch):
    monkeypatch.setattr(om, "_LAST_DETAIL_TS", 0.0)
    monkeypatch.setattr(om, "_MIN_DETAIL_INTERVAL_S", 0.05)  # keep the test fast

    stamps = []
    stamps_lock = threading.Lock()

    def worker():
        om._rate_limited_detail_wait()
        with stamps_lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stamps.sort()
    # Aggregate pacing: 6 GETs each spaced >= interval means the whole batch spans
    # at least (n-1) * interval. This is robust to per-thread scheduling jitter
    # (which can only ADD to the span), unlike asserting each individual gap.
    assert len(stamps) == 6
    assert stamps[-1] - stamps[0] >= 5 * 0.05 - 0.005


# --------------------------------------------------------------------------
# Per-thread sessions: distinct Session object per thread, stable within one.
# --------------------------------------------------------------------------
def test_desc_session_is_per_thread(monkeypatch):
    # Fresh thread-local so leftover sessions from other tests don't interfere.
    monkeypatch.setattr(om, "_DESC_THREAD_LOCAL", threading.local())

    main_a = om._desc_session()
    main_b = om._desc_session()
    assert main_a is main_b  # stable within a thread

    other = {}

    def worker():
        other["session"] = om._desc_session()

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert other["session"] is not main_a  # different thread -> different session
