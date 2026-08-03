"""ISSUE #78 (C) — a bare "London" plus a commute target is not a searchable area.

Reported 2026-08-03. OnTheMarket's /to-rent/property/london/ page is real and does return
rows, but the canonical harvest is a CAPPED top-slice of it and that page is not ordered
geographically, so the sample scatters across the metro (the pool cached on the day of the
report sat in Feltham, Hounslow, Barking, Hayes, Harrow, N17, E17, Chiswick). A
"within N minutes of UCL" filter then removed nearly all of it and the user was told London
had nothing — while the same query against `bloomsbury` returned a full page.

That empty panel is what sent the reporter back to the chat box, where they hit the
re-narration defect. This is the commute-shaped twin of the price-shaped failure
_band_rescue already handles.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

# --- Pin the real source roots ahead of tests/ (stale shadow copies live under tests/).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

import core.tools.search_properties as sp  # noqa: E402
from core.scraping import on_demand  # noqa: E402
from core.scraping.on_demand import is_unsearchable_city_area  # noqa: E402


# ── the predicate ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["London", "london", "  LONDON  ", "central london",
                                  "Central London"])
def test_london_is_flagged_as_unsearchable_as_one_area(name):
    assert is_unsearchable_city_area(name) is True


@pytest.mark.parametrize("name", ["Manchester", "Edinburgh", "Leeds", "Bristol"])
def test_smaller_cities_keep_todays_behaviour(name):
    """Their whole-city page fits the scrape cap, so its top-slice IS the city. Expanding
    them would trade a working search for a slower one."""
    assert is_unsearchable_city_area(name) is False


@pytest.mark.parametrize("name", ["Bloomsbury", "Camden", "Shoreditch", "kings-cross"])
def test_real_neighbourhoods_are_never_flagged(name):
    assert is_unsearchable_city_area(name) is False


def test_blank_and_odd_inputs_are_safe():
    for bad in ("", "   ", None, {}, {"slug": "london"}, {"city": "london"}):
        expected = isinstance(bad, dict) and bool(bad)
        assert is_unsearchable_city_area(bad) is expected


def test_classify_place_dict_is_accepted():
    assert is_unsearchable_city_area({"kind": "area", "slug": "london",
                                      "city": "london"}) is True


def test_a_london_neighbourhood_dict_is_not_treated_as_the_whole_city():
    """classify_place("Camden") carries city="london". Reading `city` before `slug` (which
    is what is_city_level_area does, correctly, for ITS question) would expand every London
    neighbourhood as if the user had typed "London"."""
    assert is_unsearchable_city_area({"kind": "area", "slug": "camden",
                                      "city": "london"}) is False


# ── the wiring: the expansion actually redirects the scrape ─────────────────────
def _row(addr, price, geo):
    return {
        "Address": addr, "Price": f"£{price} pcm", "Room_Type_Category": "1 bed flat",
        "URL": f"https://www.onthemarket.com/details/{abs(hash(addr)) % 99999}/",
        "geo_location": geo, "Images": [], "Description": f"{addr} — a flat.",
        "Detailed_Amenities": "",
    }


_BLOOMSBURY_GEO = "51.5220,-0.1270"


def _london_scrape_harness(monkeypatch, *, recos):
    """Everything offline. get_listings records which areas were actually scraped; the
    all-London pool is deliberately EMPTY, mirroring what the commute filter does to it.

    The recorder collects every call, and the deadline-aware fetcher probes each area
    twice (a near-free cache-only pass, then the bounded scrape), so assertions below
    compare the SET of areas touched rather than the call sequence.
    """
    scraped = []

    def _fake_get_listings(location, *a, **k):
        scraped.append(location)
        rows = ([] if location.lower() == "london"
                else [_row(f"1 {location} Street", 1500, _BLOOMSBURY_GEO)])
        return {"rows": [dict(r) for r in rows],
                "meta": {"requested_city": "london", "stale": False,
                         "source": "scraped", "count": len(rows)}}

    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setattr(on_demand, "get_listings", _fake_get_listings)
    monkeypatch.setattr(on_demand, "classify_place",
                        lambda n: {"kind": "area", "slug": (n or "").lower(),
                                   "city": "london", "address": None})
    monkeypatch.setattr(
        on_demand, "is_destination",
        lambda k: (k.get("kind") if isinstance(k, dict) else k) in ("university", "workplace"),
        raising=False)

    async def _fake_recommend_areas(destination, **kw):
        return list(recos)

    import core.recommend_areas as ram
    monkeypatch.setattr(ram, "recommend_areas", _fake_recommend_areas)
    return scraped


def test_bare_london_plus_a_commute_target_searches_near_the_destination(monkeypatch):
    """The reported trigger: "London, <=20 min to UCL" scraped the all-London page, whose
    capped top-slice sits in Feltham/Hayes/Barking, and the commute filter emptied it."""
    scraped = _london_scrape_harness(monkeypatch, recos=[
        {"name": "Bloomsbury", "slug": "bloomsbury", "city": "london", "commute_minutes": 6},
        {"name": "King's Cross", "slug": "kings-cross", "city": "london", "commute_minutes": 12},
    ])
    res = asyncio.run(sp.search_properties_impl(
        area="London", commute_destination="UCL", max_commute_time=20,
        confirmed=True, max_budget=3000, bedrooms=1, reply_language="en"))

    assert "london" not in [s.lower() for s in scraped], \
        f"the useless all-London pool was still scraped: {scraped}"
    assert {s.lower() for s in scraped} == {"bloomsbury", "kings-cross"}
    # The form must not keep claiming "London" while every row came from Bloomsbury.
    assert [a.lower() for a in res["search_criteria"]["areas"]] == ["bloomsbury", "kings-cross"]
    assert res["search_criteria"]["area"].lower() == "bloomsbury"


def test_expansion_degrades_to_todays_behaviour_when_the_recommender_is_empty(monkeypatch):
    """No recommendations in time -> scrape the plain city slug, exactly as before. The
    fix must never turn a thin result into no result at all."""
    scraped = _london_scrape_harness(monkeypatch, recos=[])
    asyncio.run(sp.search_properties_impl(
        area="London", commute_destination="UCL", max_commute_time=20,
        confirmed=True, max_budget=3000, bedrooms=1, reply_language="en"))
    assert {s.lower() for s in scraped} == {"london"}


def test_no_commute_target_means_no_expansion(monkeypatch):
    """Without a destination there is nothing to be "near", so a bare London search stays
    a bare London search."""
    scraped = _london_scrape_harness(monkeypatch, recos=[
        {"name": "Bloomsbury", "slug": "bloomsbury", "city": "london", "commute_minutes": 6},
    ])
    asyncio.run(sp.search_properties_impl(
        area="London", no_commute=True,
        confirmed=True, max_budget=3000, bedrooms=1, reply_language="en"))
    assert {s.lower() for s in scraped} == {"london"}


def test_a_neighbourhood_with_a_commute_target_is_left_alone(monkeypatch):
    """Camden is already a searchable pool; expanding it would trade a working search for
    a slower one."""
    scraped = _london_scrape_harness(monkeypatch, recos=[
        {"name": "Bloomsbury", "slug": "bloomsbury", "city": "london", "commute_minutes": 6},
    ])
    asyncio.run(sp.search_properties_impl(
        area="Camden", commute_destination="UCL", max_commute_time=20,
        confirmed=True, max_budget=3000, bedrooms=1, reply_language="en"))
    assert {s.lower() for s in scraped} == {"camden"}
