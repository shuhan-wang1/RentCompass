"""Multi-area search, non-blocking destination default, OnTheMarket description
enrichment, and the web-grounded area recommender.

All network (scrape / web search / LLM / geocode) is mocked, so these are
deterministic and offline. They lock in the contracts added for:
  * areas[]  -> per-area scrape, merge, and `area` tagging on each recommendation
  * a destination named as the residence -> commute locked + area defaulted +
    recommendations attached (never the hard missing-area gate)
  * top-N listing descriptions surfaced on each recommendation
  * recommend_areas: every LLM candidate validated against real data, sorted,
    cached (a fresh hit is served instantly and marked source="cache").
"""

import asyncio
import os
import sys

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

import core.commute as commute
import core.recommend_areas as ram
from core.scraping import on_demand, onthemarket
from core.tools.search_properties import search_properties_impl


def _run(**kwargs):
    return asyncio.run(search_properties_impl(**kwargs))


# ══════════════════════════════════════════════════════════════════════════
# A. commute.py — pure primitives (no network)
# ══════════════════════════════════════════════════════════════════════════
def test_parse_geo_accepts_str_dict_tuple():
    assert commute.parse_geo("51.53, -0.12") == (51.53, -0.12)
    assert commute.parse_geo({"lat": 51.53, "lng": -0.12}) == (51.53, -0.12)
    assert commute.parse_geo([51.53, -0.12]) == (51.53, -0.12)
    assert commute.parse_geo("garbage") is None
    assert commute.parse_geo(None) is None


def test_in_london_bbox():
    assert commute.in_london({"lat": 51.52, "lng": -0.13}) is True     # central London
    assert commute.in_london({"lat": 53.48, "lng": -2.24}) is False    # Manchester
    assert commute.in_london(None) is False


def test_coord_commute_minutes_monotonic_with_distance():
    dest = {"lat": 51.52, "lng": -0.13}
    near = commute.coord_commute_minutes("51.53,-0.12", dest)
    far = commute.coord_commute_minutes("51.60,-0.30", dest)
    assert isinstance(near, int) and isinstance(far, int)
    assert far > near > 0


def test_commute_minutes_non_london_uses_coord_estimate(monkeypatch):
    # Outside London: no TfL; must fall back to the coordinate estimate and NOT call
    # calculate_travel_time first.
    import core.maps_service as maps
    monkeypatch.setattr(maps, "calculate_travel_time",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call TfL")))
    mins = commute.commute_minutes(
        "Manchester City Centre", {"lat": 53.48, "lng": -2.24},
        origin_address="Salford", origin_geo="53.49,-2.27", london=False)
    assert isinstance(mins, int) and mins > 0


# ══════════════════════════════════════════════════════════════════════════
# B. recommend_areas — validation + sorting + caching (mocked deps)
# ══════════════════════════════════════════════════════════════════════════
_CLASSIFY = {
    "camden": {"kind": "area", "slug": "camden", "city": "london", "address": None},
    "islington": {"kind": "area", "slug": "islington", "city": "london", "address": None},
    "imperial college": {"kind": "university", "slug": "imperial", "city": "london",
                         "address": "Imperial College London"},
    "stratford": {"kind": "area", "slug": "stratford", "city": "london", "address": None},
}
_COMMUTES = {"Camden": 12, "Islington": 20, "Stratford": 55}


def _fake_classify(name):
    return _CLASSIFY.get((name or "").strip().lower(),
                         {"kind": "area", "slug": (name or "").strip().lower().replace(" ", "-"),
                          "city": "london", "address": None})


def _reco_mocks(monkeypatch, tmp_path, snippets="Camden, Islington, Imperial College and Stratford."):
    """Wire recommend_areas' module deps to deterministic offline fakes + a temp cache."""
    monkeypatch.setattr(ram, "_CACHE", ram.AreaRecoCache(str(tmp_path / "reco.sqlite3")))
    monkeypatch.setattr(ram, "get_search_snippets", lambda q, max_results=5: snippets)
    monkeypatch.setattr(
        ram, "_call_deepseek",
        lambda *a, **k: ('{"areas":[{"name":"Camden","reason":"nice"},'
                         '{"name":"Imperial College","reason":"uni"},'
                         '{"name":"Stratford","reason":"far"},'
                         '{"name":"Islington","reason":"lively"}]}'))
    monkeypatch.setattr(ram, "classify_place", _fake_classify)
    monkeypatch.setattr(ram, "resolve_location", lambda n: (_fake_classify(n)["slug"], "london"))
    monkeypatch.setattr(ram, "geocode_address", lambda addr: {"lat": 51.53, "lng": -0.12})
    monkeypatch.setattr(
        ram, "commute_minutes",
        lambda dest, dc, *, origin_address=None, origin_geo=None, london=None:
            _COMMUTES.get(origin_address, 30))


def test_recommend_areas_validates_sorts_and_grounds(monkeypatch, tmp_path):
    _reco_mocks(monkeypatch, tmp_path)
    recos = asyncio.run(ram.recommend_areas(
        "UCL", city="london", dest_coords={"lat": 51.52, "lng": -0.13},
        max_commute_time=45, limit=4))
    names = [r["name"] for r in recos]
    # Imperial College dropped (it's a destination); Stratford dropped (55 > 45 cap).
    assert names == ["Camden", "Islington"]         # sorted by commute asc (12, 20)
    assert [r["commute_minutes"] for r in recos] == [12, 20]
    assert all(r["source"] == "web+validated" for r in recos)
    assert all(r["slug"] and r["city"] == "london" and len(r["centroid"]) == 2 for r in recos)


def test_recommend_areas_excludes_default_area(monkeypatch, tmp_path):
    _reco_mocks(monkeypatch, tmp_path)
    recos = asyncio.run(ram.recommend_areas(
        "UCL", city="london", dest_coords={"lat": 51.52, "lng": -0.13},
        max_commute_time=45, exclude_slugs={"camden"}, limit=4))
    assert [r["name"] for r in recos] == ["Islington"]   # Camden excluded


def test_recommend_areas_cache_hit_is_instant(monkeypatch, tmp_path):
    _reco_mocks(monkeypatch, tmp_path)
    first = asyncio.run(ram.recommend_areas("UCL", city="london",
                        dest_coords={"lat": 51.52, "lng": -0.13}, max_commute_time=45))
    assert first and all(r["source"] == "web+validated" for r in first)
    # Second call must hit the cache: no web/LLM allowed.
    monkeypatch.setattr(ram, "get_search_snippets",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cache miss")))
    second = asyncio.run(ram.recommend_areas("UCL", city="london",
                         dest_coords={"lat": 51.52, "lng": -0.13}, max_commute_time=45))
    assert [r["name"] for r in second] == [r["name"] for r in first]
    assert all(r["source"] == "cache" for r in second)


def test_recommend_areas_empty_web_grounds_to_empty(monkeypatch, tmp_path):
    # SearXNG down / no hits -> ground nothing (no hallucination), return [].
    _reco_mocks(monkeypatch, tmp_path, snippets="")
    recos = asyncio.run(ram.recommend_areas("UCL", city="london",
                        dest_coords={"lat": 51.52, "lng": -0.13}, max_commute_time=45))
    assert recos == []


def test_recommend_areas_disabled_flag(monkeypatch, tmp_path):
    _reco_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr(ram, "AREA_RECOS_ENABLED", False)
    assert asyncio.run(ram.recommend_areas("UCL", city="london")) == []


# ══════════════════════════════════════════════════════════════════════════
# C/D/E. search_properties_impl — multi-area merge, descriptions, default area
# ══════════════════════════════════════════════════════════════════════════
def _row(addr, price, area_hint, beds=1, geo="51.53,-0.12", url=None):
    return {
        "Address": addr, "Price": f"£{price} pcm", "Room_Type_Category": f"{beds} bed flat",
        "URL": url or f"https://www.onthemarket.com/details/{abs(hash(addr)) % 99999}/",
        "geo_location": geo, "Images": [], "Description": f"{addr} — a flat in {area_hint}.",
        "Detailed_Amenities": "",
    }


def _fake_get_listings(rows_by_area):
    def _fake(location, *a, **k):
        rows = rows_by_area.get(location, [])
        return {"rows": [dict(r) for r in rows],
                "meta": {"requested_city": "london", "stale": False,
                         "source": "scraped", "count": len(rows)}}
    return _fake


def _all_areas_classifier(monkeypatch):
    """Treat every token as a residential area (nothing is a destination)."""
    monkeypatch.setattr(on_demand, "classify_place",
                        lambda n: {"kind": "area", "slug": (n or "").lower(),
                                   "city": "london", "address": None})
    monkeypatch.setattr(on_demand, "is_destination",
                        lambda k: (k.get("kind") if isinstance(k, dict) else k) in ("university", "workplace"),
                        raising=False)


def test_multi_area_merge_and_tagging(monkeypatch):
    _all_areas_classifier(monkeypatch)
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")   # isolate the merge/tag logic
    rows = {
        "Camden": [_row("1 Camden Rd", 1500, "Camden"), _row("2 Camden Rd", 1600, "Camden")],
        "Islington": [_row("9 Upper St", 1400, "Islington")],
    }
    monkeypatch.setattr(on_demand, "get_listings", _fake_get_listings(rows))
    res = _run(area="Camden", areas=["Camden", "Islington"],
               no_commute=True, confirmed=True, max_budget=3000, bedrooms=1)
    assert res["status"] == "found"
    recs = res["recommendations"]
    assert {r.get("area") for r in recs} == {"Camden", "Islington"}   # merged + tagged
    assert res["search_criteria"]["areas"] == ["Camden", "Islington"]
    assert res["known_criteria"]["areas"] == ["Camden", "Islington"]


# Geos pinned near each area's curated centroid so per-area geo validation keeps them
# (Camden 51.5390,-0.1426 ; Islington 51.5380,-0.1027) without needing a geocode mock.
_CAMDEN_GEO = "51.5390,-0.1426"
_ISLINGTON_GEO = "51.5380,-0.1027"


def test_multi_area_primary_pool_does_not_starve_secondary(monkeypatch):
    """Bug 1 regression: when the PRIMARY area alone returns enough listings to fill the
    candidate pool (>=15), the secondary area must STILL appear. Previously the single
    (primary) semantic query + global ``candidates[:15]`` truncation dropped every
    non-primary listing; per-area recall + round-robin merge fixes it."""
    _all_areas_classifier(monkeypatch)
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setenv("AREA_RECOS_ENABLED", "0")
    rows = {
        "Camden": [_row(f"{i} Camden Rd", 1500 + i * 10, "Camden", geo=_CAMDEN_GEO)
                   for i in range(15)],
        "Islington": [_row(f"{i} Upper St", 1400 + i * 10, "Islington", geo=_ISLINGTON_GEO)
                      for i in range(5)],
    }
    monkeypatch.setattr(on_demand, "get_listings", _fake_get_listings(rows))
    res = _run(area="Camden", areas=["Camden", "Islington"],
               no_commute=True, confirmed=True, max_budget=3000, bedrooms=1)
    assert res["status"] == "found"
    recs = res["recommendations"]
    areas_present = {r.get("area") for r in recs}
    # BOTH areas represented — the secondary is never fully starved by the primary.
    assert areas_present == {"Camden", "Islington"}, areas_present
    assert sum(1 for r in recs if r.get("area") == "Islington") > 0


def test_multi_area_summary_names_all_searched_areas(monkeypatch):
    """Bug 4 regression: the reply summary must enumerate EVERY searched area with its
    per-area count, not only the primary one."""
    _all_areas_classifier(monkeypatch)
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setenv("AREA_RECOS_ENABLED", "0")
    rows = {
        "Camden": [_row(f"{i} Camden Rd", 1500 + i * 10, "Camden", geo=_CAMDEN_GEO)
                   for i in range(15)],
        "Islington": [_row(f"{i} Upper St", 1400 + i * 10, "Islington", geo=_ISLINGTON_GEO)
                      for i in range(5)],
    }
    monkeypatch.setattr(on_demand, "get_listings", _fake_get_listings(rows))
    res = _run(area="Camden", areas=["Camden", "Islington"],
               no_commute=True, confirmed=True, max_budget=3000, bedrooms=1,
               reply_language="en")
    summary = res["summary"]
    assert "Camden" in summary and "Islington" in summary, summary
    assert "by area" in summary                     # per-area breakdown present
    assert "Islington: 5" in summary, summary       # secondary count reported


def test_multi_area_summary_reports_zero_result_area(monkeypatch):
    """An area that returned 0 listings is still named in the summary (per-area, honest)
    rather than being silently omitted."""
    _all_areas_classifier(monkeypatch)
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setenv("AREA_RECOS_ENABLED", "0")
    rows = {
        "Camden": [_row(f"{i} Camden Rd", 1500 + i * 10, "Camden", geo=_CAMDEN_GEO)
                   for i in range(8)],
        "Islington": [],   # nothing available in the secondary area
    }
    monkeypatch.setattr(on_demand, "get_listings", _fake_get_listings(rows))
    res = _run(area="Camden", areas=["Camden", "Islington"],
               no_commute=True, confirmed=True, max_budget=3000, bedrooms=1,
               reply_language="en")
    assert res["status"] == "found"
    summary = res["summary"]
    assert "Islington: 0" in summary, summary       # zero-result area named with 0
    assert {r.get("area") for r in res["recommendations"]} == {"Camden"}


def test_description_enrichment_on_shortlist(monkeypatch):
    _all_areas_classifier(monkeypatch)
    rows = {"Camden": [_row("1 Camden Rd", 1500, "Camden", url="https://x/1"),
                       _row("2 Camden Rd", 1600, "Camden", url="https://x/2")]}
    monkeypatch.setattr(on_demand, "get_listings", _fake_get_listings(rows))
    monkeypatch.setattr(onthemarket, "fetch_listing_description",
                        lambda url, **k: f"FULL DESC for {url}" if url else None)
    res = _run(area="Camden", no_commute=True, confirmed=True, max_budget=3000, bedrooms=1)
    recs = res["recommendations"]
    assert recs and all(r["description"].startswith("FULL DESC for ") for r in recs)


def test_destination_default_area_searches_and_attaches_recos(monkeypatch):
    # area="UCL" (a destination) with confirmed=True (soft gate bypassed): the tool must
    # DEFAULT the area to UCL's own slug, run the search, and attach area_recommendations.
    _UCL = {"kind": "university", "slug": "bloomsbury", "city": "london",
            "address": "UCL, Gower St, London WC1E 6BT"}
    monkeypatch.setattr(on_demand, "classify_place",
                        lambda n: _UCL if (n or "").strip().lower() in ("ucl", "bloomsbury")
                        else {"kind": "area", "slug": (n or "").lower(), "city": "london", "address": None})
    monkeypatch.setattr(on_demand, "is_destination",
                        lambda k: (k.get("kind") if isinstance(k, dict) else k) in ("university", "workplace"),
                        raising=False)
    monkeypatch.setattr(on_demand, "get_listings",
                        _fake_get_listings({"bloomsbury": [_row("5 Gower St", 1800, "Bloomsbury")]}))
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    # mock the recommender + maps so nothing hits the network
    import core.maps_service as maps
    monkeypatch.setattr(maps, "geocode_address", lambda a: {"lat": 51.52, "lng": -0.13})
    monkeypatch.setattr(maps, "calculate_travel_time", lambda *a, **k: 15)
    fake_recos = [{"name": "Camden Town", "slug": "camden-town", "city": "london",
                   "centroid": [51.54, -0.14], "commute_minutes": 12, "reason": "close",
                   "source": "web+validated"}]

    async def _fake_recommend(dest, **k):
        return list(fake_recos)
    monkeypatch.setattr(ram, "recommend_areas", _fake_recommend)

    res = _run(area="UCL", confirmed=True, max_budget=3000, bedrooms=1)
    assert res["status"] == "found"                       # searched, not blocked
    assert res["known_criteria"]["area"] == "bloomsbury"  # defaulted to destination's own area
    assert res["known_criteria"]["commute_destination"] == _UCL["address"]
    assert res["area_recommendations"] == fake_recos      # recommendations attached
