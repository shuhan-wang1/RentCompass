"""Deadline-aware partial results + cache-namespace isolation (latency round).

Locks in the contracts added so search_properties can never be the tool that blows the
fc-loop's 20s batch budget (the H2 cold-cache failure): it honours an injected — or
self-imposed — time.monotonic() deadline, serving whatever it already has and marking the
rest INCOMPLETE (never claiming those areas are empty), and exposes cache-namespace APIs so
the eval harness can isolate each run.

All network (scrape / RAG / geocode) is mocked or disabled, so these are deterministic and
offline. Uses asyncio.run (never get_event_loop().run_until_complete).
"""

import asyncio
import os
import sys
import time
from pathlib import Path

# --- Pin the real source roots ahead of tests/ (stale shadow copies live under tests/).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest

from core.scraping import on_demand
import core.tools.search_properties as sp_mod
from core.tools.search_properties import search_properties_impl


def _run(**kwargs):
    return asyncio.run(search_properties_impl(**kwargs))


def _row(addr, price, area_hint, beds=1, geo="51.53,-0.12", url=None):
    return {
        "Address": addr, "Price": f"£{price} pcm", "Room_Type_Category": f"{beds} bed flat",
        "URL": url or f"https://www.onthemarket.com/details/{abs(hash(addr)) % 99999}/",
        "geo_location": geo, "Images": [], "Description": f"{addr} — a flat in {area_hint}.",
        "Detailed_Amenities": "",
    }


def _meta(source, count, timed_out=False):
    return {"requested_city": "london", "stale": False, "source": source,
            "count": count, "timed_out": timed_out}


def _make_fake_get_listings(cached=None, scraped=None, slow=None, slow_sleep=5.0):
    """A get_listings fake that respects cache_only + budget_s.

    * cache_only=True -> serve `cached[area]` rows (a fresh HIT), else honest empty (a MISS);
    * a scrape (cache_only=False) -> serve `scraped[area]`; a `slow` area sleeps up to its
      budget_s and, if it needed longer, returns timed_out=True (mirrors on_demand's bounded
      scrape -> the search layer marks it INCOMPLETE).
    """
    cached = cached or {}
    scraped = scraped or {}
    slow = slow or {}

    def _fake(location, *a, **k):
        if k.get("cache_only"):
            rows = cached.get(location, [])
            return {"rows": [dict(r) for r in rows],
                    "meta": _meta("hit" if rows else "none", len(rows))}
        if location in slow:
            budget = float(k.get("budget_s") or 60.0)
            time.sleep(min(slow_sleep, budget))
            if slow_sleep > budget:               # would have needed longer -> budget hit
                return {"rows": [], "meta": _meta("none", 0, timed_out=True)}
            rows = slow.get(location) or scraped.get(location, [])
            return {"rows": [dict(r) for r in rows], "meta": _meta("scraped", len(rows))}
        rows = scraped.get(location, [])
        return {"rows": [dict(r) for r in rows],
                "meta": _meta("scraped" if rows else "none", len(rows))}

    return _fake


def _areas_classifier(monkeypatch):
    """Treat every token as a residential area (nothing is a destination)."""
    monkeypatch.setattr(on_demand, "classify_place",
                        lambda n: {"kind": "area", "slug": (n or "").lower(),
                                   "city": "london", "address": None})
    monkeypatch.setattr(on_demand, "is_destination",
                        lambda k: (k.get("kind") if isinstance(k, dict) else k) in ("university", "workplace"),
                        raising=False)


@pytest.fixture
def offline(monkeypatch):
    _areas_classifier(monkeypatch)
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setenv("AREA_RECOS_ENABLED", "0")
    monkeypatch.setenv("SEARCH_GEO_VALIDATION_ENABLED", "0")


# ══════════════════════════════════════════════════════════════════════════
# 1. Deadline already passed -> all uncached areas incomplete, returns instantly.
# ══════════════════════════════════════════════════════════════════════════
def test_deadline_already_passed_marks_all_uncached_incomplete(offline, monkeypatch):
    monkeypatch.setattr(on_demand, "get_listings",
                        _make_fake_get_listings(scraped={"Camden": [_row("1 A St", 1500, "Camden")]}))
    t0 = time.monotonic()
    res = _run(area="Camden", areas=["Camden", "Islington"], no_commute=True, confirmed=True,
               max_budget=3000, bedrooms=1, reply_language="en",
               _deadline_monotonic=time.monotonic() - 1.0)   # already in the past
    wall = time.monotonic() - t0

    assert res["partial"] is True
    assert set(res["incomplete_areas"]) == {"Camden", "Islington"}   # nothing scraped
    assert res["partial_note"]                                       # non-empty note present
    note = res["partial_note"].lower()
    assert "more listings may exist" in note                         # states listings MAY exist
    assert "do not conclude" in note                                 # forbids claiming emptiness
    assert res["cache_stats"] == {"hits": 0, "misses": 2}
    assert res["status"] == "no_results"
    # Honest: the empty message is the partial note, not "couldn't find any".
    assert res["message"] == res["partial_note"]
    assert wall < 1.0, f"expected an instant return, took {wall:.2f}s"


# ══════════════════════════════════════════════════════════════════════════
# 2. Generous deadline + fake scraper -> partial=False, incomplete_areas empty.
# ══════════════════════════════════════════════════════════════════════════
def test_generous_deadline_completes_all_areas(offline, monkeypatch):
    rows = {
        "Camden": [_row("1 Camden Rd", 1500, "Camden"), _row("2 Camden Rd", 1600, "Camden")],
        "Islington": [_row("9 Upper St", 1400, "Islington")],
    }
    monkeypatch.setattr(on_demand, "get_listings", _make_fake_get_listings(scraped=rows))
    res = _run(area="Camden", areas=["Camden", "Islington"], no_commute=True, confirmed=True,
               max_budget=3000, bedrooms=1, reply_language="en",
               _deadline_monotonic=time.monotonic() + 30.0)

    assert res["status"] == "found"
    assert res["partial"] is False
    assert res["incomplete_areas"] == []
    assert res["partial_note"] == ""
    assert res["cache_stats"] == {"hits": 0, "misses": 2}   # both scraped fresh
    assert {r.get("area") for r in res["recommendations"]} == {"Camden", "Islington"}


# ══════════════════════════════════════════════════════════════════════════
# 3. One slow area -> others complete, slow one incomplete, wall time bounded.
# ══════════════════════════════════════════════════════════════════════════
def test_one_slow_area_is_incomplete_others_complete_and_wall_bounded(offline, monkeypatch):
    monkeypatch.setenv("SEARCH_RETURN_MARGIN_S", "0")   # isolate the incomplete/complete math
    monkeypatch.setenv("SEARCH_RANK_HEADROOM_S", "0.3")
    monkeypatch.setenv("SEARCH_PER_AREA_SCRAPE_EST_S", "0.2")
    fake = _make_fake_get_listings(
        scraped={"Camden": [_row("1 Camden Rd", 1500, "Camden")]},
        slow={"Islington"}, slow_sleep=5.0)          # Islington sleeps past its slice
    monkeypatch.setattr(on_demand, "get_listings", fake)

    t0 = time.monotonic()
    res = _run(area="Camden", areas=["Camden", "Islington"], no_commute=True, confirmed=True,
               max_budget=3000, bedrooms=1, reply_language="en",
               _deadline_monotonic=time.monotonic() + 1.6)
    wall = time.monotonic() - t0

    assert res["incomplete_areas"] == ["Islington"]           # slow one only
    assert res["partial"] is True
    assert res["area_status"]["Camden"] == "results"
    assert res["area_status"]["Islington"] == "incomplete"
    assert any(r.get("area") == "Camden" for r in res.get("recommendations", []))
    assert wall < 8.0, f"one slow area must not stall the tool; took {wall:.2f}s"


# ══════════════════════════════════════════════════════════════════════════
# 4. Cached areas are served even when the deadline has passed.
# ══════════════════════════════════════════════════════════════════════════
def test_cached_area_served_even_at_deadline(offline, monkeypatch):
    fake = _make_fake_get_listings(
        cached={"Camden": [_row("1 Camden Rd", 1500, "Camden")]},   # warm
        scraped={"Islington": [_row("9 Upper St", 1400, "Islington")]})  # cold, no time
    monkeypatch.setattr(on_demand, "get_listings", fake)

    res = _run(area="Camden", areas=["Camden", "Islington"], no_commute=True, confirmed=True,
               max_budget=3000, bedrooms=1, reply_language="en",
               _deadline_monotonic=time.monotonic() - 0.5)   # past deadline

    assert res["status"] == "found"                       # cached Camden still surfaced
    assert res["incomplete_areas"] == ["Islington"]       # cold area skipped -> incomplete
    assert res["cache_stats"] == {"hits": 1, "misses": 1}
    assert {r.get("area") for r in res["recommendations"]} == {"Camden"}
    assert res["area_status"] == {"Camden": "results", "Islington": "incomplete"}


# ══════════════════════════════════════════════════════════════════════════
# 5. complete-empty vs incomplete are distinct in the payload.
# ══════════════════════════════════════════════════════════════════════════
def test_complete_empty_distinct_from_incomplete(offline, monkeypatch):
    monkeypatch.setenv("SEARCH_RETURN_MARGIN_S", "0")   # isolate the incomplete/complete math
    monkeypatch.setenv("SEARCH_RANK_HEADROOM_S", "0.3")
    monkeypatch.setenv("SEARCH_PER_AREA_SCRAPE_EST_S", "0.2")
    fake = _make_fake_get_listings(
        scraped={"Camden": [_row("1 Camden Rd", 1500, "Camden")], "Islington": []},  # empty = searched
        slow={"Hackney"}, slow_sleep=5.0)                                             # timed out
    monkeypatch.setattr(on_demand, "get_listings", fake)

    res = _run(area="Camden", areas=["Camden", "Islington", "Hackney"], no_commute=True,
               confirmed=True, max_budget=3000, bedrooms=1, reply_language="en",
               _deadline_monotonic=time.monotonic() + 1.6)

    # Islington was genuinely searched-and-empty (complete); Hackney was never finished.
    assert res["area_status"]["Camden"] == "results"
    assert res["area_status"]["Islington"] == "empty"
    assert res["area_status"]["Hackney"] == "incomplete"
    assert res["incomplete_areas"] == ["Hackney"]           # NOT Islington
    assert res["partial"] is True


# ══════════════════════════════════════════════════════════════════════════
# 5b. Return margin: the tool RETURNS before the injected deadline (never races the axe).
# ══════════════════════════════════════════════════════════════════════════
def test_return_margin_returns_before_the_axe(offline, monkeypatch):
    """With the default 1.2s SEARCH_RETURN_MARGIN_S and a scraper that would run long, the
    tool finishes at least 1.0s BEFORE the injected deadline — it paces against the
    margin-shrunk effective deadline, never the caller's abandon axe. Without the margin
    the bounded scrape would consume up to (deadline − headroom) and finish <1s early."""
    monkeypatch.setenv("SEARCH_RETURN_MARGIN_S", "1.2")
    monkeypatch.setenv("SEARCH_RANK_HEADROOM_S", "0.3")
    monkeypatch.setenv("SEARCH_PER_AREA_SCRAPE_EST_S", "0.2")
    fake = _make_fake_get_listings(
        scraped={"Camden": [_row("1 Camden Rd", 1500, "Camden")]},
        slow={"Islington"}, slow_sleep=10.0)     # would sleep well past the raw deadline
    monkeypatch.setattr(on_demand, "get_listings", fake)

    D_OFFSET = 3.0
    deadline = time.monotonic() + D_OFFSET
    res = _run(area="Camden", areas=["Camden", "Islington"], no_commute=True, confirmed=True,
               max_budget=3000, bedrooms=1, reply_language="en",
               _deadline_monotonic=deadline)
    ret = time.monotonic()

    assert ret <= deadline - 1.0, (
        f"tool returned only {deadline - ret:.2f}s before the axe — margin not honored")
    # It still produced useful output rather than crashing/relying on the axe.
    assert res["status"] in ("found", "no_results")
    assert res["incomplete_areas"] == ["Islington"]   # slow area bounded out, not the tool


# ══════════════════════════════════════════════════════════════════════════
# 5c. Commute honesty under degradation: unverified flagged, no commute fields, note bans claims.
# ══════════════════════════════════════════════════════════════════════════
def test_degraded_commute_unverified_strips_fields_and_bans_claims(offline, monkeypatch):
    monkeypatch.setenv("SEARCH_RETURN_MARGIN_S", "0")
    monkeypatch.setenv("SEARCH_RANK_HEADROOM_S", "5.0")   # tiny budget -> force the degraded path
    # Cached rows so Phase 1 serves listings without any scrape.
    monkeypatch.setattr(on_demand, "get_listings",
                        _make_fake_get_listings(cached={"Camden": [_row("1 Camden Rd", 1500, "Camden")]}))

    res = _run(area="Camden", commute_destination="UCL", max_commute_time=40,
               confirmed=True, max_budget=3000, bedrooms=1, reply_language="en",
               _deadline_monotonic=time.monotonic() + 2.0)   # 2.0 < 5.0 headroom -> degraded

    assert res["status"] == "found"
    assert res["commute_unverified"] is True
    note = res["commute_note"].lower()
    assert note                                            # a note always rides with results
    assert "not verified" in note
    assert "do not state" in note or "do not promise" in note
    # Listings must NOT carry any (stale/guessed) commute field the model could echo.
    assert res["recommendations"]
    for r in res["recommendations"]:
        assert "travel_time" not in r
    # The honesty note also rides in the headline, so the summary itself never implies a commute.
    assert res["commute_note"] in res["summary"]


def test_verified_commute_has_no_unverified_note_when_time_is_generous(offline, monkeypatch):
    """Control: a no-commute search with a generous deadline is NOT flagged unverified and
    carries no commute note (the fast path is behaviourally unchanged beyond earlier pacing)."""
    monkeypatch.setattr(on_demand, "get_listings",
                        _make_fake_get_listings(scraped={"Camden": [_row("1 Camden Rd", 1500, "Camden")]}))
    res = _run(area="Camden", no_commute=True, confirmed=True, max_budget=3000,
               bedrooms=1, reply_language="en",
               _deadline_monotonic=time.monotonic() + 30.0)

    assert res["status"] == "found"
    assert res["commute_unverified"] is False
    assert res["commute_note"] == ""


# ══════════════════════════════════════════════════════════════════════════
# 5d. Number honesty: the partial note forbids estimating/extrapolating figures.
# ══════════════════════════════════════════════════════════════════════════
def test_partial_note_forbids_extrapolating_numbers(offline, monkeypatch):
    fake = _make_fake_get_listings(
        cached={"Camden": [_row("1 A St", 1500, "Camden")]},        # warm -> has real figures
        scraped={"Islington": [_row("9 Upper St", 1400, "Islington")]})  # cold -> never reached
    monkeypatch.setattr(on_demand, "get_listings", fake)

    res = _run(area="Camden", areas=["Camden", "Islington"], no_commute=True, confirmed=True,
               max_budget=3000, bedrooms=1, reply_language="en",
               _deadline_monotonic=time.monotonic() - 1.0)   # Islington unsearched -> incomplete

    assert res["partial"] is True
    assert res["incomplete_areas"] == ["Islington"]
    note = res["partial_note"].lower()
    # unchanged honesty clauses …
    assert "more listings may exist" in note
    assert "do not conclude" in note
    # … plus the new number-honesty clause.
    assert "only the prices and figures" in note
    assert "do not estimate or extrapolate" in note


# ══════════════════════════════════════════════════════════════════════════
# 5e. Cold embedding-store guard: the ~18-20s model load never blocks a tight deadline.
# ══════════════════════════════════════════════════════════════════════════
class _FakeStore:
    """Stands in for PropertyEmbeddingStore. `is_ready()` reports warmth WITHOUT loading;
    build_index simulates the blocking cold model load (init_sleep) on first real use."""
    def __init__(self, ready, init_sleep=0.0):
        self._ready = ready
        self._init_sleep = init_sleep
        self.rows = []
        self.build_index_called = False

    def is_ready(self):
        return self._ready

    def build_index(self, rows):
        self.build_index_called = True
        if not self._ready:                 # simulate the one-time cold model load
            time.sleep(self._init_sleep)
            self._ready = True
        self.rows = list(rows or [])

    def search(self, q, top_k=10):
        return list(self.rows)[:top_k]


class _FakeCoord:
    def __init__(self, store):
        self.property_store = store

    def enhanced_search(self, q, crit):
        return list(self.property_store.rows), "", {}


@pytest.fixture
def warm_guard():
    """Save/restore the process-wide coordinator singleton + prewarm flag so an injected
    fake store never leaks into other tests."""
    saved_coord = sp_mod._RAG_COORDINATOR
    saved_prewarm = sp_mod._PREWARM_STARTED
    try:
        yield sp_mod
    finally:
        sp_mod._RAG_COORDINATOR = saved_coord
        sp_mod._PREWARM_STARTED = saved_prewarm


def test_embedding_store_ready_probe_never_triggers_init(warm_guard):
    sp = warm_guard
    sp._RAG_COORDINATOR = None
    assert sp._embedding_store_ready() is False                 # not built -> cold
    sp.set_rag_coordinator(_FakeCoord(_FakeStore(ready=False)))
    assert sp._embedding_store_ready() is False                 # built but model not loaded
    sp.set_rag_coordinator(_FakeCoord(_FakeStore(ready=True)))
    assert sp._embedding_store_ready() is True                  # warm
    sp.set_rag_coordinator(sp._DeterministicRAGCoordinator())
    assert sp._embedding_store_ready() is True                  # no model to load -> ready


def test_cold_store_tight_deadline_degrades_to_price_sorted(offline, warm_guard, monkeypatch):
    """Store not warm + a deadline that can't fit the cold load -> degrade to the existing
    deterministic price-sorted path: the blocking init is NEVER paid, listings still return
    fast, and the degradation honesty (commute unverified) rides along."""
    store = _FakeStore(ready=False, init_sleep=5.0)
    warm_guard.set_rag_coordinator(_FakeCoord(store))
    monkeypatch.setenv("SEARCH_RETURN_MARGIN_S", "0")
    monkeypatch.setenv("SEARCH_EMBED_INIT_EST_S", "20.0")
    monkeypatch.setenv("SEARCH_RANK_HEADROOM_S", "1.5")
    rows = [_row("A high", 2000, "Camden"), _row("B low", 1000, "Camden"),
            _row("C mid", 1500, "Camden")]
    monkeypatch.setattr(on_demand, "get_listings",
                        _make_fake_get_listings(cached={"Camden": rows}))   # warm cache, no scrape

    t0 = time.monotonic()
    res = _run(area="Camden", commute_destination="UCL", max_commute_time=40, confirmed=True,
               max_budget=3000, bedrooms=1, reply_language="en",
               _deadline_monotonic=time.monotonic() + 3.0)   # 3s: > headroom, but < 20s init est
    wall = time.monotonic() - t0

    assert res["status"] == "found"
    assert store.build_index_called is False        # semantic ranking skipped -> no cold load
    assert store.is_ready() is False                # init never triggered
    assert wall < 4.0, f"paid the blocking init ({wall:.2f}s)"
    # deterministic price-ascending order
    prices = [int(r["price"].replace("£", "").replace("/month", "")) for r in res["recommendations"]]
    assert prices == sorted(prices) and prices[0] == 1000
    # degraded => commute honesty, no commute fields on listings
    assert res["commute_unverified"] is True
    assert res["commute_note"]
    for r in res["recommendations"]:
        assert "travel_time" not in r


def test_cold_store_generous_deadline_proceeds_with_ranking(offline, warm_guard, monkeypatch):
    """Store not warm but the deadline comfortably fits the cold load -> ranking proceeds
    (build_index runs, store warms)."""
    store = _FakeStore(ready=False, init_sleep=0.05)
    warm_guard.set_rag_coordinator(_FakeCoord(store))
    monkeypatch.setenv("SEARCH_RETURN_MARGIN_S", "0")
    monkeypatch.setenv("SEARCH_EMBED_INIT_EST_S", "20.0")
    monkeypatch.setattr(on_demand, "get_listings",
                        _make_fake_get_listings(cached={"Camden": [_row("A", 1500, "Camden")]}))

    res = _run(area="Camden", no_commute=True, confirmed=True, max_budget=3000, bedrooms=1,
               reply_language="en", _deadline_monotonic=time.monotonic() + 30.0)

    assert res["status"] == "found"
    assert store.build_index_called is True         # semantic ranking ran
    assert store.is_ready() is True                 # store warmed by the run


def test_warm_store_ranks_even_under_tight_deadline(offline, warm_guard, monkeypatch):
    """A warm store is never degraded by the cold-store guard: ranking proceeds normally
    even under a tight-ish deadline (there is no blocking load to avoid)."""
    store = _FakeStore(ready=True)
    warm_guard.set_rag_coordinator(_FakeCoord(store))
    monkeypatch.setenv("SEARCH_RETURN_MARGIN_S", "0")
    monkeypatch.setattr(on_demand, "get_listings",
                        _make_fake_get_listings(cached={"Camden": [_row("A", 1500, "Camden")]}))

    res = _run(area="Camden", no_commute=True, confirmed=True, max_budget=3000, bedrooms=1,
               reply_language="en", _deadline_monotonic=time.monotonic() + 3.0)

    assert res["status"] == "found"
    assert store.build_index_called is True


# ══════════════════════════════════════════════════════════════════════════
# 5f. Background pre-warm: starts once, idempotent, env-disablable.
# ══════════════════════════════════════════════════════════════════════════
def test_prewarm_starts_once_and_is_idempotent(warm_guard, monkeypatch):
    sp = warm_guard
    sp._PREWARM_STARTED = False
    monkeypatch.delenv("SEARCH_EMBED_PREWARM", raising=False)
    started = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            self.target, self.name, self.daemon = target, name, daemon

        def start(self):
            started.append(self)

    monkeypatch.setattr(sp.threading, "Thread", _FakeThread)

    assert sp.start_embedding_prewarm() is True        # THIS call started it
    assert sp.start_embedding_prewarm() is False       # idempotent — not started again
    assert len(started) == 1
    assert started[0].target is sp._prewarm_embedding_store
    assert started[0].daemon is True


def test_prewarm_disabled_by_env(warm_guard, monkeypatch):
    sp = warm_guard
    sp._PREWARM_STARTED = False
    monkeypatch.setenv("SEARCH_EMBED_PREWARM", "0")

    def _boom(**k):
        raise AssertionError("prewarm thread must not be spawned when disabled")

    monkeypatch.setattr(sp.threading, "Thread", _boom)
    assert sp.start_embedding_prewarm() is False
    assert sp._PREWARM_STARTED is False                # stayed unset so a later enable still works


# ══════════════════════════════════════════════════════════════════════════
# 6. Cache-namespace API: swap, isolation of a held instance, getter reflects swap.
# ══════════════════════════════════════════════════════════════════════════
def test_set_cache_path_swaps_namespace_and_isolates_old_instance(tmp_path):
    p1 = tmp_path / "ns1.sqlite3"
    p2 = tmp_path / "ns2.sqlite3"
    saved = on_demand.get_cache_path()
    saved_singleton = on_demand._CACHE
    try:
        on_demand.set_cache_path(p1)
        assert on_demand.get_cache_path() == Path(p1)
        c1 = on_demand._cache()                       # instance bound to ns1
        assert c1.path == Path(p1)
        held = c1                                     # simulate an in-flight/abandoned thread's ref

        returned_old = on_demand.set_cache_path(p2)
        assert Path(returned_old) == Path(p1)         # returns the OLD path
        assert on_demand.get_cache_path() == Path(p2)  # getter reflects the swap
        c2 = on_demand._cache()
        assert c2.path == Path(p2)
        assert c2 is not held                         # singleton was reset

        # The held (old) instance keeps writing to ns1 — never the new namespace.
        held.set("k", [{"URL": "u", "Address": "a", "Price": "£1"}])
        assert on_demand._cache().get("k") is None    # new namespace does not see it
        assert on_demand.ListingCache(p1).get("k") is not None  # old file has it
    finally:
        on_demand.set_cache_path(saved)
        on_demand._CACHE = saved_singleton


def test_get_cache_path_default_is_the_module_path():
    # Fresh process default: the active namespace is the import-time CACHE_PATH.
    assert on_demand.get_cache_path() == Path(on_demand._CACHE_PATH)


# ══════════════════════════════════════════════════════════════════════════
# 7. _deadline_monotonic is NOT model-visible, but DOES reach the function.
# ══════════════════════════════════════════════════════════════════════════
def test_deadline_absent_from_model_visible_schema():
    from core.tools.search_properties import search_properties_tool
    from core.tool_system import to_function_calling_format

    props = search_properties_tool.parameters["properties"]
    assert "_deadline_monotonic" not in props
    fc = to_function_calling_format(search_properties_tool.to_spec())
    assert "_deadline_monotonic" not in fc["function"]["parameters"]["properties"]
    # to_llm_format text likewise must not advertise it.
    assert "_deadline_monotonic" not in search_properties_tool.to_llm_format()


def test_injected_underscore_param_reaches_func_via_execute():
    """The pydantic input model drops unknown keys, so an injected `_deadline_monotonic` must
    be forwarded by Tool.execute — verify it lands on a func that declares it."""
    from core.tool_system import Tool
    captured = {}

    async def fake_impl(area=None, _deadline_monotonic=None, **kw):
        captured["area"] = area
        captured["_deadline_monotonic"] = _deadline_monotonic
        return {"success": True}

    t = Tool(name="probe", description="d", func=fake_impl,
             parameters={"type": "object", "properties": {"area": {"type": "string"}}, "required": []})
    res = asyncio.run(t.execute(area="Camden", _deadline_monotonic=123.0))
    assert res.success
    assert captured == {"area": "Camden", "_deadline_monotonic": 123.0}
    # And it never leaked into the model-visible schema.
    assert "_deadline_monotonic" not in t.parameters["properties"]


def test_injected_param_not_forwarded_to_func_that_rejects_it():
    """A func without the param and without **kwargs must not receive it (no TypeError)."""
    from core.tool_system import Tool

    async def fake_impl(area=None):
        return {"success": True, "area": area}

    t = Tool(name="probe2", description="d", func=fake_impl,
             parameters={"type": "object", "properties": {"area": {"type": "string"}}, "required": []})
    res = asyncio.run(t.execute(area="X", _deadline_monotonic=1.0))
    assert res.success and res.data["area"] == "X"
