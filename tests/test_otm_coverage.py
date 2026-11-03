"""OnTheMarket coverage: the 2026-11-02 search that showed £2,100+ serviced flats for "near UCL".

The all-London page holds 35,751 rows and the scraper read about 30 of them, because its page
parameter was dead, it asked for every property type when the user wanted one, and rows past
the first page never reached ranking. These tests pin each link in that chain.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

import core.recommend_areas as areas  # noqa: E402
import core.scraping.on_demand as on_demand  # noqa: E402
import core.scraping.onthemarket as om  # noqa: E402
import core.scraping.otm_access as access  # noqa: E402
import core.tools.search_properties as sp  # noqa: E402
from core.ranking import rank_and_diversify  # noqa: E402

# The rules of the live file that decide these paths, as fetched on 2026-11-02.
ROBOTS = """User-agent: *
Allow: /content/
Disallow: */flat/
Disallow: */apartment/
Allow: *-bed-proper
Allow: *-bed-flats-
Allow: *-bed-houses
Disallow: *-bed-
Allow: */studio/
Allow: */studio-west/
Disallow: */studio-
Disallow: /student/*-bed-flats-
Disallow: /student/*-bed-houses
Disallow: *max-bedrooms=
Disallow: *sort-field=**
Disallow: *max-price=
Disallow: *min-bedrooms=
Disallow: *prop-types=
Disallow: *radius=
Disallow: *min-price=
"""


def listing(n, price="£1,500 pcm (£346 pw)", beds=1, kind="Flat", title="1 bedroom flat to rent"):
    return {"id": n, "details-url": f"/details/{n}/", "address": f"{n} Example Street, London WC1N",
            "property-title": title, "price": price, "bedrooms": beds,
            "humanised-property-type": kind, "location": {"lat": 51.52, "lon": -0.12}, "features": []}


def page(rows=(), total=None, following=None):
    results = {"list": list(rows)}
    if total is not None:
        results["totalResults"] = total
    if following is not None:
        results["paginationControls"] = {"next": {"next-link": "/x/?page=2"} if following else None}
    state = {"props": {"pageProps": {"initialReduxState": {"results": results}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(state)}</script>'


class Response:
    def __init__(self, text="", status_code=200):
        self.text, self.status_code, self.headers = text, status_code, {}


class Session:
    """Answers by URL and query, and records every request."""

    def __init__(self, pages):
        self.pages, self.calls, self.headers = pages, [], {}

    def get(self, url, params=None, timeout=None, allow_redirects=True):
        target = url.replace(om.BASE, "") + (f"?page={params['page']}" if params else "")
        self.calls.append(target)
        answer = self.pages.get(target, Response(status_code=404))
        return answer if isinstance(answer, Response) else Response(answer)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OTM_ACCESS_STATE_PATH", str(tmp_path / "access.sqlite3"))
    monkeypatch.setattr(access, "MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(on_demand, "_CACHE", on_demand.ListingCache(tmp_path / "listings.sqlite3"))
    monkeypatch.setattr(on_demand, "ALLOW_DEMO_FALLBACK", False)


def serve(monkeypatch, pages):
    session = Session(pages)
    monkeypatch.setattr(om, "_new_session", lambda: session)
    return session


# ── which URLs may be requested ───────────────────────────────────────────────
@pytest.mark.parametrize("channel", sorted(om.CHANNEL_PATHS))
def test_every_channel_passes_the_allowlist_and_robots(channel):
    path = om.CHANNEL_PATHS[channel].format(slug="bloomsbury")
    access.validate_url(om.BASE + path, {"page": 2})
    assert access._robots_allow(ROBOTS, path)
    assert access._robots_allow(ROBOTS, path + "?page=2")


@pytest.mark.parametrize("path,params", [
    ("/to-rent/property/camden/", {"page-number": 2}),   # the dead parameter
    ("/to-rent/property/camden/", {"max-price": 1500}),
    ("/to-rent/property/camden/", {"min-bedrooms": 1}),
    ("/to-rent/property/camden/", {"sort-field": "price"}),
    ("/to-rent/property/camden/", {"page": 2, "radius": 1}),
    ("/to-rent/flat/camden/", {}),
    ("/to-rent/apartment/camden/", {}),
    ("/to-rent/6-bed-property/camden/", {}),
    ("/for-sale/property/camden/", {}),
])
def test_requests_robots_forbids_are_refused(path, params):
    with pytest.raises(access.OTMSourceError):
        access.validate_url(om.BASE + path, params)


# ── paging ────────────────────────────────────────────────────────────────────
def test_pages_use_the_sites_parameter_and_reach_new_rows(monkeypatch):
    path = "/to-rent/property/camden/"
    session = serve(monkeypatch, {
        path: page([listing(i) for i in range(1, 31)], total=45, following=True),
        path + "?page=2": page([listing(i) for i in range(31, 46)], total=45, following=False)})
    rows = om.find_rich_onthemarket("camden", 1, 0, 10**6, 120, 0, 9)
    assert len(rows) == 45
    assert session.calls == [path, path + "?page=2"]
    assert rows.coverage["exhaustive"] is True
    assert rows.coverage["source_total"] == {"property": 45}


def test_a_page_that_repeats_the_last_one_is_reported_not_treated_as_the_end(monkeypatch, caplog):
    """This is what the dead parameter looked like: 200, full page, nothing new."""
    path = "/to-rent/property/camden/"
    same = page([listing(i) for i in range(1, 31)], total=215, following=True)
    session = serve(monkeypatch, {path: same, path + "?page=2": same})
    with caplog.at_level(logging.WARNING, logger=om.logger.name):
        rows = om.find_rich_onthemarket("camden", 1, 0, 10**6, 120, 0, 9)
    assert len(rows) == 30 and len(session.calls) == 2
    assert rows.coverage["exhaustive"] is False
    assert "pagination_not_advancing" in caplog.text


def test_page_depth_comes_from_the_new_variable_and_ignores_the_old_one(monkeypatch):
    monkeypatch.setenv("OTM_SEARCH_MAX_PAGES", "1")
    assert om._page_depth() == om.DEFAULT_PAGE_DEPTH
    monkeypatch.setenv("OTM_SEARCH_PAGE_DEPTH", "99")
    assert om._page_depth() == om.MAX_PAGE_DEPTH
    monkeypatch.setenv("OTM_SEARCH_PAGE_DEPTH", "junk")
    assert om._page_depth() == om.DEFAULT_PAGE_DEPTH


def test_the_deep_budget_keeps_the_pages_already_read(monkeypatch):
    path = "/to-rent/studio/camden/"
    session = serve(monkeypatch, {path: page([listing(1)], following=True)})
    rows = om.find_rich_onthemarket("camden", 1, 0, 10**6, 120, 0, 9, budget_s=5,
                                    deep_budget_s=0, channels=("studio",))
    assert len(rows) == 1 and session.calls == [path]
    assert rows.coverage["stopped_early"] is True
    assert rows.coverage["exhaustive"] is False


def test_the_first_page_runs_even_with_no_deep_budget_left(monkeypatch):
    path = "/to-rent/property/camden/"
    serve(monkeypatch, {path: page([listing(1)], following=False)})
    assert len(om.find_rich_onthemarket("camden", 1, 0, 10**6, 120, 0, 9, deep_budget_s=0)) == 1


# ── channels ──────────────────────────────────────────────────────────────────
def test_channels_take_turns_and_share_one_row_set(monkeypatch):
    share, student = "/to-rent/house-flat-share/camden/", "/student/property/camden/"
    session = serve(monkeypatch, {
        share: page([listing(1), listing(2)], following=True),
        student: page([listing(2), listing(3)], following=False),
        share + "?page=2": page([listing(4)], following=False)})
    rows = om.find_rich_onthemarket("camden", 1, 0, 10**6, 120, 0, 9,
                                    channels=("house-flat-share", "student"))
    assert session.calls == [share, student, share + "?page=2"]
    assert [row["URL"].rstrip("/").rsplit("/", 1)[1] for row in rows] == ["1", "2", "3", "4"]


def test_one_listing_reached_through_two_channels_is_kept_once(monkeypatch):
    """The student channel links the same details page with '?channel=student'."""
    studio, student = "/to-rent/studio/camden/", "/student/property/camden/"
    via_student = dict(listing(2), **{"details-url": "/details/2/?channel=student"})
    serve(monkeypatch, {
        studio: page([listing(1), listing(2)], following=False),
        student: page([via_student, listing(3)], following=False)})
    rows = om.find_rich_onthemarket("camden", 1, 0, 10**6, 120, 0, 9,
                                    channels=("studio", "student"))
    assert [row["URL"].split("?")[0].rstrip("/").rsplit("/", 1)[1] for row in rows] == ["1", "2", "3"]


def test_a_channel_the_site_lacks_falls_back_to_all_property(monkeypatch):
    path = "/to-rent/property/smalltown/"
    session = serve(monkeypatch, {path: page([listing(1)], following=False)})
    rows = om.find_rich_onthemarket("smalltown", 1, 0, 10**6, 120, 0, 9, channels=("studio",))
    assert len(rows) == 1
    assert session.calls == ["/to-rent/studio/smalltown/", path]
    assert rows.coverage["channels"] == ["studio", "property"]


def test_an_unknown_place_still_fails_on_the_all_property_page(monkeypatch):
    serve(monkeypatch, {})
    with pytest.raises(access.OTMSourceError) as failure:
        om.find_rich_onthemarket("nowhere", 1, 0, 10**6, 120, 0, 9, channels=("studio",))
    assert failure.value.status_code == 404


@pytest.mark.parametrize("code,status", [(403, "blocked"), (429, "rate_limited")])
def test_a_refusal_is_never_mistaken_for_a_missing_channel(monkeypatch, code, status):
    session = serve(monkeypatch, {"/to-rent/studio/camden/": Response(status_code=code)})
    with pytest.raises(access.OTMSourceError) as failure:
        om.find_rich_onthemarket("camden", 1, 0, 10**6, 120, 0, 9, channels=("studio",))
    assert failure.value.source_status == status
    assert len(session.calls) == 1


# ── prices ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,beds,expected", [
    ("£2,650 pcm (£612 pw)", 1, "£2,650 pcm"),
    ("£358 pw", None, "£1,551 pcm"),
    ("£252 pw", 1, "£1,092 pcm"),
    ("£297 pppw", 2, "£2,574 pcm"),
    ("£254 pppw", 5, "£5,503 pcm"),
    ("£250 pppw", None, "£1,083 pcm"),
    ("POA", 1, "POA"),
    (None, 1, ""),
])
def test_rent_is_always_a_monthly_figure_for_the_whole_let(raw, beds, expected):
    assert om._pcm_price(raw, beds) == expected


def test_a_weekly_rent_is_never_read_as_a_monthly_one():
    row = om._map_listing(listing(1, price="£358 pw", title="Studio to rent", kind="Studio"), "student")
    assert on_demand._row_price(row) == 1551


def test_how_the_rent_was_quoted_stays_on_the_row():
    row = om._map_listing(listing(1, price="£297 pppw", beds=2), "student")
    assert "student let" in row["Payment_Rules"]
    assert "£297 per person per week" in row["Payment_Rules"]
    assert "£1,287 pcm each" in row["Payment_Rules"]
    assert "£2,574 pcm for all 2 bedrooms" in row["Payment_Rules"]
    assert om._map_listing(listing(2))["Payment_Rules"] == ""


# ── plans and the cache ───────────────────────────────────────────────────────
@pytest.mark.parametrize("beds,room_type,plan,channels", [
    ((0, 0), None, "studio", ("studio",)),
    ((0, 6), "studio", "studio", ("studio",)),
    ((1, 1), None, "1-bed", ("1-bed-property",)),
    ((5, 5), None, "5-bed", ("5-bed-property",)),
    ((6, 6), None, "property", ("property",)),
    ((0, 6), None, "property", ("property",)),
    ((1, 6), None, "property", ("property",)),
    ((0, 6), "whole", "property", ("property",)),
    ((0, 6), "shared", "rooms", on_demand.ROOM_CHANNELS),
    ((1, 1), "ensuite", "rooms", on_demand.ROOM_CHANNELS),
])
def test_a_request_maps_to_the_channels_that_can_answer_it(beds, room_type, plan, channels):
    assert on_demand.search_plan(*beds, room_type) == plan
    assert on_demand.plan_channels(plan) == channels


def test_the_all_property_key_is_unchanged_so_a_deploy_keeps_its_warm_cache():
    assert on_demand._query_key("camden") == f"otm|camden|{on_demand.CANONICAL_KEY_VERSION}"
    assert on_demand._query_key("camden", "studio") != on_demand._query_key("camden")


def test_each_plan_is_scraped_and_cached_on_its_own(monkeypatch):
    studio, one_bed = "/to-rent/studio/camden/", "/to-rent/1-bed-property/camden/"
    session = serve(monkeypatch, {
        studio: page([listing(1, beds=None, kind="Studio", title="Studio to rent")], following=False),
        one_bed: page([listing(2)], following=False)})
    assert len(on_demand.get_listings("Camden", 0, 0, 100, 5000)["rows"]) == 1
    assert len(on_demand.get_listings("Camden", 1, 1, 100, 5000)["rows"]) == 1
    assert session.calls == [studio, one_bed]


def test_a_new_budget_on_the_same_plan_still_reuses_the_sample(monkeypatch):
    path = "/to-rent/studio/camden/"
    session = serve(monkeypatch, {path: page([listing(1, beds=None, kind="Studio", title="Studio to rent")],
                                             following=False)})
    on_demand.get_listings("Camden", 0, 0, 100, 2000)
    again = on_demand.get_listings("Camden", 0, 0, 100, 1800)
    assert again["meta"]["source"] == "hit" and session.calls == [path]


def test_room_type_selects_the_room_channels(monkeypatch):
    session = serve(monkeypatch, {
        "/to-rent/house-flat-share/camden/": page([listing(1, kind="House share")], following=False),
        "/student/property/camden/": page([listing(2, kind="House share")], following=False),
        "/to-rent/property/camden/": page([listing(3)], following=False)})
    result = on_demand.get_listings("Camden", 0, 6, 100, 5000, room_type="shared")
    assert len(result["rows"]) == 3 and len(session.calls) == 3
    assert result["meta"]["plan"] == "rooms"


def test_an_outage_falls_back_to_the_areas_all_property_sample(monkeypatch):
    cache = on_demand._cache()
    cache.set(on_demand._query_key("camden"), [{
        "Address": "1 Old Street, London", "Price": "£1,000 pcm", "Room_Type_Category": "Studio",
        "URL": "https://www.onthemarket.com/details/9/"}])
    serve(monkeypatch, {"/to-rent/studio/camden/": Response(status_code=503)})
    result = on_demand.get_listings("Camden", 0, 0, 100, 5000)
    assert [row["URL"] for row in result["rows"]] == ["https://www.onthemarket.com/details/9/"]
    assert result["meta"]["source"] == "stale-cache"


def test_a_sample_cut_short_is_refreshed_sooner(monkeypatch):
    stale = (["row"], time.time() - 2 * 3600)
    assert on_demand._is_fresh_entry(stale) is True
    assert on_demand._is_fresh_entry(stale, {"stopped_early": True}) is False


def test_a_search_waits_for_a_slot_rather_than_marking_the_area_incomplete(monkeypatch):
    class Busy:
        def __init__(self):
            self.tries = 0

        def acquire(self, *, blocking):
            self.tries += 1
            return self.tries > 2

        def release(self):
            pass

    monkeypatch.setattr(on_demand, "_SCRAPE_SLOTS", Busy())
    monkeypatch.setattr(on_demand.onthemarket, "find_rich_onthemarket", lambda *a, **k: [])
    assert on_demand._scrape_live("camden", 0, 2, 100, 5000, 15, 5) == ([], False)


def test_one_search_can_fill_every_area_slot():
    assert on_demand.MAX_IN_FLIGHT_SCRAPES >= int(os.getenv("SEARCH_MAX_AREAS", "4"))


# ── area fallback and the recommender ─────────────────────────────────────────
@pytest.mark.parametrize("destination,slug", [
    ("UCL", "bloomsbury"),
    ("University College London, Gower Street, London WC1E 6BT", "bloomsbury"),
    ("Imperial College London", "south-kensington"),
    ("Oxford Street", None),         # the curated answer is the whole city
    ("London", None),
    ("Acme Widgets HQ", None),
    ("", None),
    (None, None),
])
def test_curated_destination_area(destination, slug):
    assert on_demand.curated_destination_area(destination) == slug


def _recommender_harness(monkeypatch, tmp_path, snippets="", reply=None):
    monkeypatch.setattr(areas, "_CACHE", areas.AreaRecoCache(tmp_path / "reco.sqlite3"), raising=False)
    monkeypatch.setattr(areas, "_cache", lambda: areas._CACHE)
    monkeypatch.setattr(areas, "get_search_snippets", lambda *a, **k: snippets)
    monkeypatch.setattr(areas, "_call_deepseek", lambda *a, **k: reply)
    return lambda: areas._CACHE.get(areas._dest_key(
        "UCL", "london", max_commute=40, excludes=set(), limit=4,
        dest_coords={"lat": 51.5, "lng": -0.1}))


def _recommend():
    return asyncio.run(areas.recommend_areas(
        "UCL", city="london", max_commute_time=40, dest_coords={"lat": 51.5, "lng": -0.1}))


def test_a_web_search_outage_is_not_remembered_as_an_empty_answer(monkeypatch, tmp_path):
    """An empty written during an outage outlived it by hours: search had recovered, and
    every search still went to the bare city until the entry expired."""
    cached = _recommender_harness(monkeypatch, tmp_path, snippets="")
    assert _recommend() == []
    assert cached() is None


def test_a_model_failure_is_not_remembered_either(monkeypatch, tmp_path):
    cached = _recommender_harness(monkeypatch, tmp_path, snippets="real text about areas", reply=None)
    assert _recommend() == []
    assert cached() is None


def test_a_real_empty_answer_is_still_cached(monkeypatch, tmp_path):
    cached = _recommender_harness(monkeypatch, tmp_path, snippets="real text about areas",
                                  reply='{"areas": []}')
    assert _recommend() == []
    assert cached() is not None


# ── from harvest to the page ──────────────────────────────────────────────────
def test_semantic_recall_keeps_every_row_of_the_search():
    from rag.rag_coordinator import RAGCoordinator

    class Store:
        properties = [{}] * 90

        def search(self, query, top_k):
            self.asked = top_k
            return []

    coordinator = RAGCoordinator.__new__(RAGCoordinator)
    coordinator.property_store = Store()
    coordinator.enhanced_search("Find flat near Camden", {"destination": "Camden"})
    assert coordinator.property_store.asked == 90


def test_the_commute_pool_holds_the_cheapest_rows_not_only_the_best_worded():
    ranked = [{"id": i, "parsed_price": 3000 - i} for i in range(60)]
    pool = sp._commute_candidate_pool(ranked)
    assert len(pool) == 15
    assert [row["id"] for row in pool[:8]] == list(range(8))
    assert {row["id"] for row in pool[8:]} == set(range(53, 60))


def test_a_short_list_passes_through_the_commute_pool_unchanged():
    ranked = [{"id": i} for i in range(5)]
    assert sp._commute_candidate_pool(ranked) == ranked


def test_with_no_budget_a_cheaper_listing_beats_an_equal_one():
    rows = [{"Address": f"{name} Road", "price": price, "similarity_score": 0.5}
            for name, price in (("Costly", 2400), ("Middle", 1900), ("Cheap", 1200))]
    assert [row["price"] for row in rank_and_diversify(rows)] == [1200, 1900, 2400]


def test_rows_with_no_price_do_not_break_the_no_budget_anchor():
    assert len(rank_and_diversify([{"Address": "A", "price": None}, {"Address": "B"}])) == 2


def test_a_stuck_future_cannot_hold_the_search_past_its_bound():
    async def run():
        loop = asyncio.get_running_loop()
        done, stuck, failed = loop.create_future(), loop.create_future(), loop.create_future()
        done.set_result(12)
        failed.set_exception(ValueError("no route"))
        return await sp._settle_within([done, stuck, failed], 0.01)

    twelve, unfinished, failure = asyncio.run(run())
    assert twelve == 12 and unfinished is None and isinstance(failure, ValueError)


class _Retrieval:
    """A warm ranker, so the search reaches its commute stage instead of degrading."""

    class property_store:
        @staticmethod
        def build_index(rows):
            _Retrieval.rows = rows

        @staticmethod
        def is_ready():
            return True

    def enhanced_search(self, query, criteria):
        for row in _Retrieval.rows:
            row.setdefault("parsed_price", 1500)
        return list(_Retrieval.rows), [], []


def _no_recommender(monkeypatch):
    async def nothing(destination, **kwargs):
        return []

    monkeypatch.setattr(areas, "recommend_areas", nothing)
    monkeypatch.setattr(sp, "_RAG_COORDINATOR", _Retrieval())
    monkeypatch.setattr(sp, "_get_rag_coordinator", lambda: sp._RAG_COORDINATOR)


def test_a_commute_provider_outage_is_not_reported_as_no_listings(monkeypatch):
    """Throttled geocoding left every row with no commute at all, and the filter read
    "unknown" as "too far": the user was told a full area had nothing to rent."""
    rows = [{"Address": f"{i} Gower Street, London", "Price": "£1,500 pcm", "URL": f"https://x/{i}",
             "Room_Type_Category": "Studio", "geo_location": "51.5246, -0.1340", "Description": "A studio",
             "Images": [], "Detailed_Amenities": ""} for i in range(3)]
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: {
        "rows": [dict(r) for r in rows],
        "meta": {"requested_city": "london", "stale": False, "source": "scraped", "count": 3}})
    monkeypatch.setattr(on_demand, "classify_place", lambda n: {
        "kind": "area", "slug": "bloomsbury", "city": "london", "address": None})
    import core.maps_service as maps
    _no_recommender(monkeypatch)
    monkeypatch.setattr(maps, "calculate_travel_time", lambda *a, **k: None)
    monkeypatch.setattr(maps, "geocode_address", lambda *a, **k: None)
    res = asyncio.run(sp.search_properties_impl(
        area="Bloomsbury", commute_destination="UCL", max_commute_time=40, confirmed=True,
        room_type="studio", reply_language="en", _deadline_monotonic=time.monotonic() + 10))
    assert res["status"] == "found" and len(res["recommendations"]) == 3
    assert res["commute_unverified"] is True and res["commute_note"]
    assert all(row.get("travel_time") is None for row in res["recommendations"])


def test_a_slow_routing_provider_leaves_coordinate_estimates(monkeypatch):
    """The commute stage used to wait for every route with no bound at all."""
    rows = [{"Address": f"{i} Gower Street, London", "Price": "£1,500 pcm", "URL": f"https://x/{i}",
             "Room_Type_Category": "Studio", "geo_location": "51.5246, -0.1340", "Description": "A studio",
             "Images": [], "Detailed_Amenities": ""} for i in range(3)]
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: {
        "rows": [dict(r) for r in rows],
        "meta": {"requested_city": "london", "stale": False, "source": "scraped", "count": 3}})
    monkeypatch.setattr(on_demand, "classify_place", lambda n: {
        "kind": "area", "slug": "bloomsbury", "city": "london", "address": None})
    import core.maps_service as maps
    _no_recommender(monkeypatch)
    released = threading.Event()
    monkeypatch.setattr(maps, "calculate_travel_time", lambda *a, **k: released.wait(30) or 20)
    monkeypatch.setattr(maps, "geocode_address", lambda *a, **k: {"lat": 51.5246, "lng": -0.1340})

    async def search():
        started = time.monotonic()
        try:
            found = await sp.search_properties_impl(
                area="Bloomsbury", commute_destination="UCL", max_commute_time=40, confirmed=True,
                room_type="studio", reply_language="en", _deadline_monotonic=time.monotonic() + 7)
            return found, time.monotonic() - started
        finally:
            # asyncio.run waits for executor threads, so measure inside and free them here.
            released.set()

    res, took = asyncio.run(search())
    assert took < 7
    assert res["status"] == "found" and len(res["recommendations"]) == 3
    assert {row["travel_time_source"] for row in res["recommendations"]} == {"estimate"}


def _search_harness(monkeypatch, rows):
    seen = []

    def fake_get_listings(location, *a, **k):
        seen.append(k)
        return {"rows": [dict(r) for r in rows], "meta": {
            "requested_city": "london", "stale": False, "source": "scraped", "count": len(rows)}}

    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setattr(on_demand, "get_listings", fake_get_listings)
    monkeypatch.setattr(on_demand, "classify_place", lambda n: {
        "kind": "area", "slug": (n or "").lower(), "city": "london", "address": None})
    return seen


def test_the_search_asks_for_the_right_channels_and_a_wide_pool(monkeypatch):
    seen = _search_harness(monkeypatch, [])
    asyncio.run(sp.search_properties_impl(
        area="Camden", no_commute=True, confirmed=True, room_type="studio", reply_language="en"))
    assert seen and all(call["room_type"] == "studio" for call in seen)
    assert all(call["limit"] >= 100 for call in seen)
    assert "deep_budget_s" in seen[-1]


def test_the_pool_is_shared_between_areas(monkeypatch):
    seen = _search_harness(monkeypatch, [])
    asyncio.run(sp.search_properties_impl(
        areas=["Camden", "Islington", "Hackney", "Bow"], no_commute=True, confirmed=True,
        room_type="studio", reply_language="en"))
    assert seen and all(call["limit"] == 30 for call in seen)


def _room(n, kind="1 bed House share", text="A double room."):
    return {"Address": f"{n} Guilford Street, London", "Price": "£1,200 pcm", "URL": f"https://x/{n}/",
            "Room_Type_Category": kind, "geo_location": "51.5229, -0.1228", "Description": text,
            "Images": [], "Detailed_Amenities": ""}


def test_a_room_can_be_confirmed_en_suite_from_its_detail_page(monkeypatch):
    _search_harness(monkeypatch, [_room(1), _room(2)])
    monkeypatch.setattr(om, "fetch_listing_details", lambda url, **k: {
        "description": "Double bedroom with a kitchenette and en-suite." if url.endswith("1/")
        else "Shared bathroom.", "available_from": ""})
    res = asyncio.run(sp.search_properties_impl(
        area="Bloomsbury", no_commute=True, confirmed=True, room_type="ensuite", reply_language="en"))
    assert [row["url"] for row in res["recommendations"]] == ["https://x/1/"]


def test_a_family_flat_with_an_en_suite_bedroom_is_not_an_en_suite_room():
    flat = {"Room_Type_Category": "2 bed Flat", "Detailed_Amenities": "Two double bedrooms, one en-suite"}
    share = {"Room_Type_Category": "2 bed Flat share", "Detailed_Amenities": "En-suite room"}
    assert sp._matches_room_type(flat, "ensuite") is False
    assert sp._matches_room_type(share, "ensuite") is True
