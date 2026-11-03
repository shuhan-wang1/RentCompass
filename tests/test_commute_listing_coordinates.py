"""A listing's commute starts at the listing's own coordinates.

A search for a studio within 40 minutes of UCL answered with a bare "cannot verify the
commute condition" for 7 of 10 listings. Every one of those rows carried
the portal's coordinates, but the post-search check routed from the display address, and
addresses such as "L-Suite at Gravity Co., The Weymouth Marylebone, 50-60 Weymouth Street
W1G" do not geocode. The search-stage annotation had the same gap. The model's own
calculate_commute call already used listing coordinates; the two automatic paths did not.

Also pinned here: a cache entry written before the paging fix (page 1 only, coverage with no
``channels``) is refreshed after the short TTL instead of being reused for half a day.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

import core.maps_service as maps  # noqa: E402
import core.scraping.on_demand as on_demand  # noqa: E402
import core.tools.search_properties as sp  # noqa: E402
from core.candidate_validation import (  # noqa: E402
    collect_commute_evidence,
    validate_search_payload_with_provider,
)
from core.commute_basis import describe_measured  # noqa: E402
from core.mcp_runtime import runtime_arguments_from_meta, split_mcp_arguments  # noqa: E402
from core.tools.search_nearby_pois import listing_origin_coordinates  # noqa: E402

UNGEOCODABLE = "L-Suite at Gravity Co., The Weymouth Marylebone, 50-60 Weymouth Street W1G"
UCL = "University College London"
WEYMOUTH = {"lat": 51.520312, "lng": -0.147921}


# ─── reading a row's coordinates ─────────────────────────────────────

@pytest.mark.parametrize("row, expected", [
    ({"geo_location": "51.520312, -0.147921"}, WEYMOUTH),
    ({"Geo_Location": "51.5203121, -0.1479209"}, WEYMOUTH),
    ({"geo_location": {"lat": 51.520312, "lon": -0.147921}}, WEYMOUTH),
    ({"latitude": 51.520312, "longitude": -0.147921}, WEYMOUTH),
    ({"lat": "51.520312", "lng": "-0.147921"}, WEYMOUTH),
])
def test_a_rows_own_position_is_read_in_every_scraper_shape(row, expected):
    assert listing_origin_coordinates(row) == expected


@pytest.mark.parametrize("row", [
    {},
    {"geo_location": ""},
    {"geo_location": "0, 0"},                        # a parse accident, not a UK listing
    {"geo_location": "-0.147921, 51.520312"},        # swapped
    {"geo_location": "nan, nan"},
    {"geo_location": "inf, -0.14"},
    {"latitude": True, "longitude": False},
    {"latitude": 40.71, "longitude": -74.0},         # not in the UK
    "51.52, -0.14",
])
def test_anything_but_a_sane_uk_position_is_rejected(row):
    assert listing_origin_coordinates(row) is None


# ─── the post-search validator ───────────────────────────────────────

class _Recorder:
    """A provider that records each call and answers 19 minutes from coordinates only."""

    def __init__(self):
        self.calls = []

    async def execute_tool(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if kwargs.get("_from_coordinates"):
            return {"success": True, "data": {
                "success": True, "from_address": kwargs["from_address"],
                "to_address": kwargs["to_address"], "duration_minutes": 19,
                "route_source": "TfL Journey Planner", "origin_source": "listing_coordinates"}}
        return {"success": False, "error": "an address could not be geocoded"}


def test_the_validator_routes_from_the_listings_coordinates():
    provider = _Recorder()
    rows = [{"address": UNGEOCODABLE, "geo_location": "51.520312, -0.147921"}]
    evidence = asyncio.run(collect_commute_evidence(provider, rows, UCL))
    (name, kwargs), = provider.calls
    assert name == "calculate_commute"
    assert kwargs["from_address"] == UNGEOCODABLE and kwargs["to_address"] == UCL
    assert kwargs["_from_coordinates"] == WEYMOUTH
    assert evidence[0]["evidence_status"] == "success" and evidence[0]["duration_minutes"] == 19
    # Provenance: the evidence row says the origin was the listing's position.
    assert evidence[0]["origin_source"] == "listing_coordinates"
    assert evidence[0]["origin_coordinates"] == WEYMOUTH


@pytest.mark.parametrize("row", [
    {"address": UNGEOCODABLE},
    {"address": UNGEOCODABLE, "geo_location": "0, 0"},
])
def test_without_sane_coordinates_the_call_is_address_only_as_before(row):
    provider = _Recorder()
    evidence = asyncio.run(collect_commute_evidence(provider, [row], UCL))
    (_name, kwargs), = provider.calls
    assert "_from_coordinates" not in kwargs
    assert "origin_source" not in evidence[0]
    assert evidence[0]["evidence_status"] == "failed"


def _payload(rows):
    return {"status": "found",
            "search_criteria": {"max_commute_time": 40, "commute_destination": UCL},
            "recommendations": rows}


def _registry_with_real_commute_tool(tmp_path, monkeypatch, seen):
    """The shipped calculate_commute tool behind a real ToolRegistry, so the private
    ``_from_coordinates`` hint has to survive Tool.execute's schema validation."""
    from core.tool_system import ToolRegistry
    from uk_rent_agent.tools.idempotency import IdempotencyStore
    from core.tools.calculate_commute import calculate_commute_tool

    def details(origin, destination, mode="transit", *, origin_coordinates=None):
        seen.append(origin_coordinates)
        if not origin_coordinates:
            return None  # the display address does not geocode
        out = {"route_legs": [], "route_summary": "Bus 73", "source": "TfL Journey Planner",
               "estimated_duration_minutes": None, "changes_count": 0}
        out.update(describe_measured(22))
        return out

    monkeypatch.setattr(maps, "calculate_travel_details", details)
    registry = ToolRegistry(IdempotencyStore(tmp_path / "idem.sqlite3"))
    registry.register(calculate_commute_tool)
    return registry


def test_a_listing_whose_address_does_not_geocode_is_verified_from_its_coordinates(
        tmp_path, monkeypatch):
    seen = []
    registry = _registry_with_real_commute_tool(tmp_path, monkeypatch, seen)
    rows = [{"address": UNGEOCODABLE, "price": 1550, "geo_location": "51.520312, -0.147921"}]
    validated, evidence = asyncio.run(validate_search_payload_with_provider(registry, _payload(rows)))
    assert seen == [WEYMOUTH]
    assert evidence[0]["evidence_status"] == "success"
    assert evidence[0]["raw_data"]["origin_source"] == "listing_coordinates"
    v = validated["candidate_validation"]
    assert len(v["eligible"]) == 1 and not v["unknown"] and not v["excluded"]


def test_no_coordinates_and_no_geocode_stays_unknown(tmp_path, monkeypatch):
    """Fail-closed: a listing that cannot be routed is never assumed to meet the limit."""
    seen = []
    registry = _registry_with_real_commute_tool(tmp_path, monkeypatch, seen)
    rows = [{"address": UNGEOCODABLE, "price": 1550}]
    validated, evidence = asyncio.run(validate_search_payload_with_provider(registry, _payload(rows)))
    assert seen and all(origin is None for origin in seen)  # the tool may retry once
    assert evidence[0]["evidence_status"] == "failed"
    v = validated["candidate_validation"]
    assert not v["eligible"] and len(v["unknown"]) == 1


def test_a_measured_time_over_the_limit_from_coordinates_excludes_the_listing(
        tmp_path, monkeypatch):
    seen = []
    registry = _registry_with_real_commute_tool(tmp_path, monkeypatch, seen)
    rows = [{"address": UNGEOCODABLE, "price": 1550, "geo_location": "51.520312, -0.147921"}]
    payload = _payload(rows)
    payload["search_criteria"]["max_commute_time"] = 20
    validated, _evidence = asyncio.run(validate_search_payload_with_provider(registry, payload))
    v = validated["candidate_validation"]
    assert len(v["excluded"]) == 1 and not v["eligible"]


# ─── the transports the hint has to cross ────────────────────────────

def test_the_hint_crosses_mcp_as_metadata_for_routing_tools_only():
    public, meta = split_mcp_arguments({
        "from_address": UNGEOCODABLE, "to_address": UCL, "_from_coordinates": WEYMOUTH})
    assert public == {"from_address": UNGEOCODABLE, "to_address": UCL}
    assert runtime_arguments_from_meta("calculate_commute", meta) == {"_from_coordinates": WEYMOUTH}
    assert runtime_arguments_from_meta("calculate_commute_cost", meta) == {
        "_from_coordinates": WEYMOUTH}
    assert runtime_arguments_from_meta("search_properties", meta) == {}


@pytest.mark.parametrize("bad", [
    {"lat": float("nan"), "lng": 0.1}, {"lat": "51.5", "lng": "-0.1"}, {"lat": 91, "lng": 0},
    "51.5,-0.1", None,
])
def test_a_malformed_hint_never_reaches_tool_code(bad):
    public, meta = split_mcp_arguments({"from_address": "x", "_from_coordinates": bad})
    assert public == {"from_address": "x"}
    assert runtime_arguments_from_meta("calculate_commute", meta) == {}


def test_a_sealed_specialist_call_drops_the_hint_instead_of_refusing_the_commute(tmp_path):
    """The specialist boundary admits no post-seal key but the deadline. Refusing the whole
    call would turn every routable listing into "unknown"; the hint is dropped and the call
    routes from the address, exactly as before."""
    from core.agent_loop import build_fc_nodes
    from core.tools.search_properties import search_properties_tool
    from tests.test_manager_v1_specialist_dispatch import (
        _execute, _registry, _schema, _state, _tc, _tool)

    seen = []

    async def search_properties(**_kwargs):
        return {"status": "found",
                "search_criteria": {"max_commute_time": 30,
                                    "commute_destination": "Synthetic Campus"},
                "recommendations": [{"address": "Flat 4, 21 Hartland Road, London NW1 8DB",
                                     "price": 900, "geo_location": "51.5432, -0.1460"}]}

    async def commute(**kwargs):
        seen.append(kwargs)
        return {"duration_minutes": 21}

    registry = _registry(tmp_path, [
        _tool("search_properties", search_properties,
              parameters=search_properties_tool.parameters),
        _tool("calculate_commute", commute, parameters=_schema("from_address", "to_address", "mode")),
    ])
    nodes = build_fc_nodes(registry, specialist_dispatch=True)
    state = _execute(nodes, _state([_tc("search_properties", {"area": "Synthetic"}, "c1")],
                                   message="Find a home within 30 minutes of Synthetic Campus"))
    assert len(seen) == 1 and "_from_coordinates" not in seen[0]
    evidence, = state["commute_evidence"]
    assert evidence["evidence_status"] == "success"
    # Routed from the address, so the row must not claim the listing's coordinates.
    assert "origin_source" not in evidence


# ─── the search-stage annotation ─────────────────────────────────────

def test_the_search_annotation_routes_from_coordinates_and_labels_the_figure_measured(
        monkeypatch):
    from tests.test_otm_coverage import _no_recommender

    rows = [{"Address": UNGEOCODABLE, "Price": "£1,550 pcm", "URL": "https://x/1",
             "Room_Type_Category": "Studio", "geo_location": "51.520312, -0.147921",
             "Description": "A studio", "Images": [], "Detailed_Amenities": ""}]
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: {
        "rows": [dict(r) for r in rows],
        "meta": {"requested_city": "london", "stale": False, "source": "scraped", "count": 1}})
    monkeypatch.setattr(on_demand, "classify_place", lambda n: {
        "kind": "area", "slug": "marylebone", "city": "london", "address": None})
    _no_recommender(monkeypatch)
    routed, looked_up = [], []

    def travel_time(origin, destination, mode="transit", *, origin_coordinates=None):
        routed.append(origin_coordinates)
        return 22 if origin_coordinates else None

    def basis(origin, destination, mode="transit", *, origin_coordinates=None):
        looked_up.append(origin_coordinates)
        if not origin_coordinates:
            return None
        out = {"source": "TfL Journey Planner"}
        out.update(describe_measured(22))
        return out

    monkeypatch.setattr(maps, "calculate_travel_time", travel_time)
    monkeypatch.setattr(maps, "travel_basis_if_known", basis)
    monkeypatch.setattr(maps, "geocode_address", lambda *a, **k: {"lat": 51.5246, "lng": -0.1340})
    res = asyncio.run(sp.search_properties_impl(
        area="Marylebone", commute_destination=UCL, max_commute_time=40, confirmed=True,
        room_type="studio", reply_language="en", _deadline_monotonic=time.monotonic() + 10))
    assert routed == [WEYMOUTH] and WEYMOUTH in looked_up
    row, = res["recommendations"]
    # Labelled as a measured journey, not as a straight-line estimate.
    assert row["travel_time_source"] == "routing"
    assert row["travel_time"] == f"22 min to {UCL}"


def test_a_row_without_coordinates_keeps_the_two_argument_call():
    """Address-only rows call the producer exactly as before, so its contract is unchanged."""
    calls = []

    def producer(origin, destination, mode="transit"):
        calls.append((origin, destination))
        return 30

    original = maps.calculate_travel_time
    maps.calculate_travel_time = producer
    try:
        assert sp._listing_travel_time({"Address": "12 Rennie St"}, UCL) == 30
    finally:
        maps.calculate_travel_time = original
    assert calls == [("12 Rennie St", UCL)]


def test_the_basis_cache_keys_a_coordinate_route_apart_from_an_address_route(monkeypatch):
    stored = {}
    monkeypatch.setattr(maps, "get_from_cache", lambda key: stored.get(key))
    monkeypatch.setattr(maps, "set_to_cache", lambda key, value, *a, **k: stored.__setitem__(key, value))

    def details(origin, destination, mode="transit", *, origin_coordinates=None):
        out = {"source": "TfL Journey Planner", "route_legs": []}
        out.update(describe_measured(22 if origin_coordinates else 35))
        return out

    monkeypatch.setattr(maps, "calculate_travel_details", details)
    assert maps.calculate_travel_time(UNGEOCODABLE, UCL, origin_coordinates=WEYMOUTH) == 22
    assert maps.travel_basis_if_known(UNGEOCODABLE, UCL, origin_coordinates=WEYMOUTH)[
        "duration_minutes"] == 22
    # The address-only pair is a different journey and is not answered from that entry.
    assert maps.travel_basis_if_known(UNGEOCODABLE, UCL) is None
    assert maps.calculate_travel_time(UNGEOCODABLE, UCL) == 35


# ─── cache entries written before the paging fix ─────────────────────

_TWO_HOURS_AGO = time.time() - 2 * 3600


def test_an_entry_from_before_the_paging_fix_gets_the_short_ttl():
    entry = (["row"], _TWO_HOURS_AGO)
    pre_upgrade = {"scope": "bounded_area_sample", "exhaustive": False, "pages": 1}
    assert on_demand._is_fresh_entry(entry, pre_upgrade) is False


def test_an_entry_from_the_upgraded_scraper_keeps_the_full_ttl():
    entry = (["row"], _TWO_HOURS_AGO)
    upgraded = {"scope": "bounded_area_sample", "exhaustive": False, "channels": ["property"],
                "stopped_early": False}
    assert on_demand._is_fresh_entry(entry, upgraded) is True
    assert on_demand._is_fresh_entry(entry, {**upgraded, "stopped_early": True}) is False
    # An hour-old pre-upgrade entry is still inside the short TTL.
    assert on_demand._is_fresh_entry((["row"], time.time() - 600), {"scope": "x"}) is True


def test_a_search_rereads_an_area_cached_by_the_old_scraper(monkeypatch):
    cache = on_demand._cache()
    key = on_demand._query_key("fitzrovia")
    cache.set(key, [{"Address": "1 Old Street, London", "Price": "£1,000 pcm",
                     "URL": "https://www.onthemarket.com/details/1/"}],
              metadata={"coverage": {"scope": "bounded_area_sample", "exhaustive": False}})
    with cache._lock, cache._connect() as db:
        db.execute("UPDATE listings SET fetched = ? WHERE key = ?", (_TWO_HOURS_AGO, key))
    scraped = []

    def scrape(slug, *args, **kwargs):
        scraped.append(slug)
        return on_demand._ScrapeOutcome([{"Address": "2 New Street, London", "Price": "£1,100 pcm",
                                          "URL": "https://www.onthemarket.com/details/2/"}], False)

    monkeypatch.setattr(on_demand, "_scrape_live", scrape)
    result = on_demand.get_listings("Fitzrovia", 0, 5, 100, 5000)
    assert scraped == ["fitzrovia"]
    assert result["meta"]["source"] == "scraped"
