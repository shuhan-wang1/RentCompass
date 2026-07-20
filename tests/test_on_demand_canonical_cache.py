"""
Unit tests for the CANONICAL broad-scrape + slug-keyed listing cache.

The customer search path now caches ONE wide row-set per area (all beds, a full
price sweep) under a slug-only key, and applies the caller's bed/price band as a
LOCAL post-filter. This makes warm hits deterministic per AREA regardless of the
(model-generated, drifting) band, and collapses redundant cold scrapes.

Scraper is monkeypatched throughout — no network.
Run:  pytest tests/test_on_demand_canonical_cache.py
"""

import os
import sys

# --- Pin the real source roots ahead of tests/ (mirrors test_on_demand_listings).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest

import core.scraping.onthemarket as om_mod
from core.scraping import on_demand


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------
@pytest.fixture
def fresh_cache(tmp_path, monkeypatch):
    cache = on_demand.ListingCache(tmp_path / "listings.sqlite3")
    monkeypatch.setattr(on_demand, "_CACHE", cache)
    monkeypatch.setattr(on_demand, "ALLOW_DEMO_FALLBACK", False)
    return cache


def _canon_rows():
    """A varied CANONICAL area harvest: studio + 1/2/3-bed across a price sweep."""
    return [
        {"Address": "1 A St, Manchester, M1", "URL": "https://www.onthemarket.com/details/1/",
         "Price": "£800 pcm", "Room_Type_Category": "Studio", "geo_location": "53.4, -2.2"},
        {"Address": "2 B St, Manchester, M2", "URL": "https://www.onthemarket.com/details/2/",
         "Price": "£1000 pcm", "Room_Type_Category": "1 bed Flat", "geo_location": "53.4, -2.2"},
        {"Address": "3 C St, Manchester, M3", "URL": "https://www.onthemarket.com/details/3/",
         "Price": "£1500 pcm", "Room_Type_Category": "2 bed Flat", "geo_location": "53.4, -2.2"},
        {"Address": "4 D St, Manchester, M4", "URL": "https://www.onthemarket.com/details/4/",
         "Price": "£3000 pcm", "Room_Type_Category": "3 bed House", "geo_location": "53.4, -2.2"},
    ]


def _counting_scrape(monkeypatch, rows_factory=_canon_rows):
    calls = {"n": 0, "params": []}

    def fake_scrape(slug, radius, min_price, max_price, limit, min_bedrooms, max_bedrooms):
        calls["n"] += 1
        calls["params"].append(dict(slug=slug, min_price=min_price, max_price=max_price,
                                    limit=limit, min_bedrooms=min_bedrooms,
                                    max_bedrooms=max_bedrooms))
        return rows_factory()

    monkeypatch.setattr(om_mod, "find_rich_onthemarket", fake_scrape)
    return calls


# --------------------------------------------------------------------------
# 1) Same slug, two different bands -> exactly ONE scrape
# --------------------------------------------------------------------------
def test_same_slug_two_bands_scrapes_once(fresh_cache, monkeypatch):
    calls = _counting_scrape(monkeypatch)

    # Band A: 1-bed, £500-1200 -> only the £1000 1-bed row.
    a = on_demand.get_listings("Manchester", 1, 1, 500, 1200)
    assert a["meta"]["source"] == "scraped"
    assert [r["URL"] for r in a["rows"]] == ["https://www.onthemarket.com/details/2/"]

    # Band B: 2-bed, £500-2000 -> only the £1500 2-bed row, served WARM from the
    # same canonical entry (no second scrape).
    b = on_demand.get_listings("Manchester", 2, 2, 500, 2000)
    assert b["meta"]["source"] == "hit"
    assert [r["URL"] for r in b["rows"]] == ["https://www.onthemarket.com/details/3/"]

    assert calls["n"] == 1  # one canonical scrape served both bands

    # And the scrape used the CANONICAL wide band, not band A.
    p = calls["params"][0]
    assert (p["min_bedrooms"], p["max_bedrooms"]) == (on_demand.CANONICAL_MIN_BEDS,
                                                      on_demand.CANONICAL_MAX_BEDS)
    assert (p["min_price"], p["max_price"]) == (on_demand.CANONICAL_MIN_PRICE,
                                                on_demand.CANONICAL_MAX_PRICE)
    assert p["limit"] == on_demand.CANONICAL_SCRAPE_LIMIT


# --------------------------------------------------------------------------
# 2) Fresh canonical + empty band-filter = complete-empty HIT, no re-scrape
# --------------------------------------------------------------------------
def test_fresh_canonical_empty_band_is_complete_empty_hit(fresh_cache, monkeypatch):
    calls = _counting_scrape(monkeypatch)

    # Populate the canonical entry with a matching band.
    first = on_demand.get_listings("Manchester", 1, 1, 500, 1200)
    assert first["meta"]["source"] == "scraped" and first["rows"]

    # A band no row can satisfy (5-bed): fresh canonical entry exists, so this is a
    # genuine complete-empty HIT — NOT a miss — and must NOT trigger another scrape.
    empty = on_demand.get_listings("Manchester", 5, 5, 500, 1200)
    assert empty["meta"]["source"] == "hit"
    assert empty["rows"] == [] and empty["meta"]["count"] == 0
    assert calls["n"] == 1  # no re-scrape on an empty band


# --------------------------------------------------------------------------
# 3) cache_only semantics: hit iff a FRESH canonical entry exists
# --------------------------------------------------------------------------
def test_cache_only_miss_when_absent(fresh_cache, monkeypatch):
    calls = _counting_scrape(monkeypatch)
    res = on_demand.get_listings("Manchester", 1, 1, 500, 1200, cache_only=True)
    assert res["meta"]["source"] == "none" and res["rows"] == []
    assert calls["n"] == 0  # cache-only never scrapes


def test_cache_only_hit_when_fresh_canonical_exists(fresh_cache, monkeypatch):
    calls = _counting_scrape(monkeypatch)
    on_demand.get_listings("Manchester", 1, 1, 500, 1200)  # warm the canonical entry
    assert calls["n"] == 1

    res = on_demand.get_listings("Manchester", 2, 2, 500, 2000, cache_only=True)
    assert res["meta"]["source"] == "hit"
    assert [r["URL"] for r in res["rows"]] == ["https://www.onthemarket.com/details/3/"]
    assert calls["n"] == 1  # still no scrape


def test_cache_only_hit_even_when_band_filter_empty(fresh_cache, monkeypatch):
    calls = _counting_scrape(monkeypatch)
    on_demand.get_listings("Manchester", 1, 1, 500, 1200)  # warm

    # Fresh canonical entry exists but the 5-bed band yields nothing: still a HIT
    # (fresh entry present), per the cache_only contract.
    res = on_demand.get_listings("Manchester", 5, 5, 500, 1200, cache_only=True)
    assert res["meta"]["source"] == "hit"
    assert res["rows"] == [] and res["meta"]["count"] == 0
    assert calls["n"] == 1


# --------------------------------------------------------------------------
# 4) limit is applied AFTER filtering
# --------------------------------------------------------------------------
def test_limit_applied_after_filter(fresh_cache, monkeypatch):
    _counting_scrape(monkeypatch)
    # Band 0-3 beds, £100-5000 matches all four rows; limit=2 caps the MATCHES.
    res = on_demand.get_listings("Manchester", 0, 3, 100, 5000, limit=2)
    assert res["meta"]["count"] == 2 and len(res["rows"]) == 2


# --------------------------------------------------------------------------
# 5) Old param-format keys are never matched (they age out via TTL)
# --------------------------------------------------------------------------
def test_old_format_key_is_ignored_triggers_scrape(fresh_cache, monkeypatch):
    # Seed a row under the OLD band-embedded key scheme.
    fresh_cache.set("otm|manchester|b1-1|p500-1200", _canon_rows())
    calls = _counting_scrape(monkeypatch)

    res = on_demand.get_listings("Manchester", 1, 1, 500, 1200)
    # The old key does not match the slug-only canonical key -> a real scrape ran.
    assert res["meta"]["source"] == "scraped"
    assert calls["n"] == 1


def test_query_key_is_slug_only_and_versioned():
    k = on_demand._query_key("manchester")
    assert k == f"otm|manchester|{on_demand.CANONICAL_KEY_VERSION}"
    # No band info embedded.
    assert "b" + "1" not in k.split("|")[-1]


# --------------------------------------------------------------------------
# 6) set_cache_path still isolates namespaces
# --------------------------------------------------------------------------
def test_set_cache_path_isolates_namespaces(tmp_path, monkeypatch):
    monkeypatch.setattr(on_demand, "ALLOW_DEMO_FALLBACK", False)
    calls = _counting_scrape(monkeypatch)

    ns_a = tmp_path / "a" / "cache.sqlite3"
    ns_b = tmp_path / "b" / "cache.sqlite3"
    original = on_demand.get_cache_path()
    try:
        on_demand.set_cache_path(ns_a)
        on_demand.get_listings("Manchester", 1, 1, 500, 1200)  # scrape -> namespace A
        assert calls["n"] == 1

        # Namespace B is empty: a cache-only lookup MISSES (isolation holds).
        on_demand.set_cache_path(ns_b)
        miss = on_demand.get_listings("Manchester", 1, 1, 500, 1200, cache_only=True)
        assert miss["meta"]["source"] == "none"

        # Back to A: the canonical entry is still there (warm hit, no new scrape).
        on_demand.set_cache_path(ns_a)
        hit = on_demand.get_listings("Manchester", 1, 1, 500, 1200, cache_only=True)
        assert hit["meta"]["source"] == "hit" and hit["rows"]
        assert calls["n"] == 1
    finally:
        on_demand.set_cache_path(original)
        # Drop the singleton so later tests rebuild against their own namespace.
        monkeypatch.setattr(on_demand, "_CACHE", None, raising=False)


# --------------------------------------------------------------------------
# 7) Defensive band filtering (malformed / sparse rows)
# --------------------------------------------------------------------------
def test_in_band_keeps_rows_missing_fields_when_unconstrained():
    # Full canonical band: a row missing BOTH price and room-type is still included.
    row = {"Address": "x", "URL": "u"}
    assert on_demand._in_band(row, on_demand.CANONICAL_MIN_BEDS, on_demand.CANONICAL_MAX_BEDS,
                              on_demand.CANONICAL_MIN_PRICE, on_demand.CANONICAL_MAX_PRICE) is True


def test_in_band_missing_price_kept_under_real_price_constraint():
    # Beds match (1) and the price is unparseable/absent -> kept (never silently dropped).
    row = {"Room_Type_Category": "1 bed Flat", "Price": ""}
    assert on_demand._in_band(row, 1, 1, 500, 1200) is True


def test_in_band_missing_beds_kept_under_real_bed_constraint():
    # No room-type -> beds unknown; price in band -> kept.
    row = {"Price": "£1100 pcm"}
    assert on_demand._in_band(row, 1, 1, 500, 1200) is True


def test_in_band_studio_excluded_under_bed_constraint():
    # A parsed Studio (0 beds) is provably outside a 2-bed request -> excluded.
    row = {"Room_Type_Category": "Studio", "Price": "£800 pcm"}
    assert on_demand._in_band(row, 2, 2, 100, 5000) is False


def test_in_band_expensive_row_excluded_under_price_cap():
    row = {"Room_Type_Category": "1 bed Flat", "Price": "£3000 pcm"}
    assert on_demand._in_band(row, 0, 4, 500, 1200) is False


def test_get_listings_keeps_row_with_unknown_beds(fresh_cache, monkeypatch):
    # A canonical row without Room_Type_Category (beds unknown) must survive a real
    # bed constraint as long as its price is in band.
    def rows():
        return [{"Address": "Deansgate, Manchester, M3",
                 "URL": "https://www.onthemarket.com/details/9/",
                 "Price": "£1100 pcm"}]  # no Room_Type_Category
    _counting_scrape(monkeypatch, rows_factory=rows)

    res = on_demand.get_listings("Manchester", 1, 1, 500, 1200)
    assert res["meta"]["count"] == 1
    assert res["rows"][0]["URL"] == "https://www.onthemarket.com/details/9/"
