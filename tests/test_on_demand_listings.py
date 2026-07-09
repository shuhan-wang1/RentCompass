"""
Unit tests for the scrape-on-demand + persistent-cache listing layer that backs
the customer-facing property search.

Run:  pytest tests/test_on_demand_listings.py
(The scraper is mocked; one clearly-marked live integration test hits the real
OnTheMarket site only when RUN_LIVE_SCRAPE=1.)
"""

import os
import sqlite3
import sys
import time

# --- Pin the real source roots ahead of tests/ (which holds stale copies of
# `core`/`rag` that would otherwise shadow the app packages under prepend mode).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "local_data_demo")):
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
# Location -> slug resolution & cross-contamination guard
# --------------------------------------------------------------------------
def test_resolve_city_and_landmark():
    assert on_demand.resolve_location("Manchester") == ("manchester", "manchester")
    assert on_demand.resolve_location("UCL") == ("bloomsbury", "london")
    # University phrasing falls through to a city-substring match.
    assert on_demand.resolve_location("University of Manchester") == ("manchester", "manchester")


def test_resolve_unknown_location_is_slugified_not_defaulted():
    slug, city = on_demand.resolve_location("Narnia")
    assert slug == "narnia" and city is None


def test_wrong_city_guard_drops_other_major_cities_only():
    # A London row is wrong for a Manchester search.
    assert on_demand._wrong_city("Baker Street, London NW1", "manchester") is True
    # A local suburb (not a major city) is kept for a London search.
    assert on_demand._wrong_city("High Street, Feltham", "london") is False
    # Unknown requested city -> never filter (trust the slug).
    assert on_demand._wrong_city("Anywhere, London", None) is False


# --------------------------------------------------------------------------
# Persistent SQLite store
# --------------------------------------------------------------------------
def test_listing_cache_persists_across_instances(tmp_path):
    path = tmp_path / "c.sqlite3"
    rows = [{"Address": "A", "URL": "u", "Price": "£1"}]
    on_demand.ListingCache(path).set("k", rows)
    got = on_demand.ListingCache(path).get("k")
    assert got is not None
    assert got[0] == rows and isinstance(got[1], float)


def _fake_rows(city="Manchester"):
    return [
        {"Address": f"1 Test St, {city}, M1", "URL": "https://www.onthemarket.com/details/1/",
         "Price": "£1000 pcm", "geo_location": "53.4, -2.2", "Room_Type_Category": "1 bed Flat"},
        {"Address": "", "URL": "", "Price": ""},  # placeholder/advert row -> dropped
    ]


@pytest.fixture
def fresh_cache(tmp_path, monkeypatch):
    cache = on_demand.ListingCache(tmp_path / "listings.sqlite3")
    monkeypatch.setattr(on_demand, "_CACHE", cache)
    monkeypatch.setattr(on_demand, "ALLOW_DEMO_FALLBACK", False)
    return cache


# --------------------------------------------------------------------------
# get_listings: miss -> scrape -> persist -> hit
# --------------------------------------------------------------------------
def test_scrape_on_miss_then_serve_from_cache(fresh_cache, monkeypatch):
    calls = {"n": 0}

    def fake_scrape(slug, radius, min_price, max_price, limit, min_bedrooms, max_bedrooms):
        calls["n"] += 1
        assert slug == "manchester" and min_bedrooms == 1 and max_bedrooms == 1
        return _fake_rows()

    monkeypatch.setattr(om_mod, "find_rich_onthemarket", fake_scrape)

    first = on_demand.get_listings("Manchester", 1, 1, 500, 1200)
    assert first["meta"]["source"] == "scraped"
    assert first["meta"]["count"] == 1  # placeholder row dropped
    assert first["rows"][0]["URL"].startswith("https://www.onthemarket.com/")

    second = on_demand.get_listings("Manchester", 1, 1, 500, 1200)
    assert second["meta"]["source"] == "hit"
    assert calls["n"] == 1  # served warm, no second scrape


def test_wrong_city_rows_filtered_out(fresh_cache, monkeypatch):
    def fake_scrape(*a, **k):
        return [
            {"Address": "Deansgate, Manchester, M3", "URL": "https://www.onthemarket.com/details/2/",
             "Price": "£1100 pcm", "geo_location": "53.47, -2.25"},
            {"Address": "Baker Street, London, NW1", "URL": "https://www.onthemarket.com/details/3/",
             "Price": "£1150 pcm", "geo_location": "51.52, -0.15"},
        ]

    monkeypatch.setattr(om_mod, "find_rich_onthemarket", fake_scrape)
    res = on_demand.get_listings("Manchester", 1, 1, 500, 1200)
    addrs = [r["Address"] for r in res["rows"]]
    assert any("Manchester" in a for a in addrs)
    assert not any("London" in a for a in addrs)  # cross-contamination removed


def test_stale_if_error_serves_old_cache_flagged(fresh_cache, monkeypatch):
    key = on_demand._query_key("manchester", 1, 1, 500, 1200)
    fresh_cache.set(key, _fake_rows()[:1])
    # Age the entry beyond the TTL.
    with sqlite3.connect(fresh_cache.path) as db:
        db.execute("UPDATE listings SET fetched = ?", (time.time() - 10_000_000,))

    monkeypatch.setattr(om_mod, "find_rich_onthemarket", lambda *a, **k: [])  # live scrape yields nothing

    res = on_demand.get_listings("Manchester", 1, 1, 500, 1200)
    assert res["meta"]["source"] == "stale-cache"
    assert res["meta"]["stale"] is True
    assert res["rows"] and res["meta"]["count"] == 1


def test_no_data_returns_honest_empty_never_demo(fresh_cache, monkeypatch):
    monkeypatch.setattr(om_mod, "find_rich_onthemarket", lambda *a, **k: [])
    res = on_demand.get_listings("Nowheresville", 1, 1, 500, 1200)
    assert res["rows"] == []
    assert res["meta"]["source"] == "none"
    assert "No live listings" in res["meta"]["message"]


def test_scrape_exception_with_no_cache_is_honest_empty(fresh_cache, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(om_mod, "find_rich_onthemarket", boom)
    res = on_demand.get_listings("Leeds", 2, 2, 500, 1500)
    assert res["rows"] == [] and res["meta"]["source"] == "none"


# --------------------------------------------------------------------------
# Live integration (opt-in) — proves real city-correctness end to end.
# --------------------------------------------------------------------------
@pytest.mark.skipif(os.getenv("RUN_LIVE_SCRAPE") != "1",
                    reason="set RUN_LIVE_SCRAPE=1 to hit the real OnTheMarket site")
def test_live_manchester_is_city_correct(tmp_path, monkeypatch):
    monkeypatch.setattr(on_demand, "_CACHE", on_demand.ListingCache(tmp_path / "live.sqlite3"))
    res = on_demand.get_listings("Manchester", 1, 1, 500, 1200)
    assert res["rows"], "expected live Manchester listings"
    for r in res["rows"]:
        assert "onthemarket.com" in r["URL"]
        assert "London" not in r["Address"]
