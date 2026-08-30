"""Regression tests for sensitive-log redaction and SearXNG fallback."""

from __future__ import annotations

import asyncio
from collections import deque

import pandas as pd
import pytest
import requests


SENTINEL = "PRIVATE_CANARY_user@example.test_14-Secret-Street"


class _Response:
    status_code = 200

    def __init__(self, payload=None, error=None):
        self._payload = payload or {}
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def json(self):
        return self._payload


def _assert_not_logged(secret, capsys, caplog):
    captured = capsys.readouterr()
    visible = "\n".join((captured.out, captured.err, caplog.text))
    assert secret not in visible
    caplog.clear()


def _public_result(secret=SENTINEL):
    return {
        "results": [{
            "title": "Student rent guide",
            "url": f"https://example.test/{secret}",
            "content": "A public rental guide with current market information.",
            "engine": "fallback",
        }]
    }


def test_searx_empty_constrained_search_retries_without_engines(
    monkeypatch, capsys, caplog
):
    from core import web_search

    calls = []
    responses = deque([_Response({"results": []}), _Response(_public_result())])

    def fake_get(url, params=None, timeout=None):
        calls.append(dict(params or {}))
        return responses.popleft()

    monkeypatch.setattr(web_search.requests, "get", fake_get)
    client = web_search.SearXNGSearch(
        instance_url=f"https://backend.test/{SENTINEL}", timeout=1
    )

    result = client.search(f"reviews {SENTINEL}", max_results=3)

    assert len(result) == 1
    assert len(calls) == 2
    assert "engines" in calls[0]
    assert "engines" not in calls[1]
    _assert_not_logged(SENTINEL, capsys, caplog)


def test_searx_backend_failure_retries_without_leaking_exception(
    monkeypatch, capsys, caplog
):
    from core import web_search

    calls = []
    responses = deque([
        _Response(error=requests.exceptions.HTTPError(
            f"500 for https://backend.test/search?q={SENTINEL}"
        )),
        _Response(_public_result()),
    ])

    def fake_get(url, params=None, timeout=None):
        calls.append(dict(params or {}))
        return responses.popleft()

    monkeypatch.setattr(web_search.requests, "get", fake_get)
    client = web_search.SearXNGSearch(timeout=1)

    result = client.search(f"reviews {SENTINEL}")

    assert result
    assert len(calls) == 2
    assert "engines" in calls[0] and "engines" not in calls[1]
    _assert_not_logged(SENTINEL, capsys, caplog)


def test_searx_two_empty_attempts_return_honest_empty_result(
    monkeypatch, capsys, caplog
):
    from core import web_search

    calls = []
    responses = deque([
        _Response({"results": [], "unresponsive_engines": [[SENTINEL, "timeout"]]}),
        _Response({"results": [], "unresponsive_engines": [[SENTINEL, "timeout"]]}),
    ])

    def fake_get(url, params=None, timeout=None):
        calls.append(dict(params or {}))
        return responses.popleft()

    monkeypatch.setattr(web_search.requests, "get", fake_get)
    client = web_search.SearXNGSearch(timeout=1)

    assert client.search(f"reviews {SENTINEL}") == []
    assert len(calls) == 2
    _assert_not_logged(SENTINEL, capsys, caplog)


def test_get_search_snippets_empty_result_is_truthful_and_query_is_redacted(
    monkeypatch, capsys, caplog
):
    from core import web_search

    monkeypatch.setattr(web_search, "_read_search_cache", lambda key: ("miss", None))
    monkeypatch.setattr(web_search._searxng_client, "search", lambda *args, **kwargs: [])

    result = web_search.get_search_snippets(SENTINEL)

    assert result == "No search results found for this query."
    _assert_not_logged(SENTINEL, capsys, caplog)


def test_commute_tools_do_not_log_addresses_or_exception_text(
    monkeypatch, capsys, caplog
):
    import core.maps_service as maps
    import core.tools.calculate_commute as commute
    import core.tools.calculate_commute_cost as cost

    def fail(*args, **kwargs):
        raise RuntimeError(f"routing failed for {SENTINEL}")

    monkeypatch.setattr(maps, "calculate_travel_details", fail)
    commute_result = commute.calculate_commute_impl(
        f"origin {SENTINEL}", f"destination {SENTINEL}"
    )
    monkeypatch.setattr(maps, "calculate_travel_basis", fail)
    cost_result = cost.calculate_commute_cost_impl(
        f"origin {SENTINEL}", f"destination {SENTINEL}",
        travel_type=f"student {SENTINEL}",
    )

    assert commute_result["success"] is False
    assert commute_result["error"] == "Commute calculation failed"
    assert cost_result["success"] is False
    assert cost_result["error"] == "Commute cost calculation failed"
    _assert_not_logged(SENTINEL, capsys, caplog)


def test_safety_and_weather_do_not_log_location_or_exception_text(
    monkeypatch, capsys, caplog
):
    import core.tools.check_safety as safety
    import core.tools.get_weather as weather

    def fail(*args, **kwargs):
        raise RuntimeError(f"provider rejected {SENTINEL}")

    monkeypatch.setattr(safety, "get_crime_data_by_location", fail)
    safety_result = safety.check_safety_impl(
        address=SENTINEL, user_query=f"安全吗 {SENTINEL}"
    )
    monkeypatch.setattr(weather.requests, "get", fail)
    weather_result = weather.get_weather_impl(SENTINEL)

    assert safety_result["error"] == "Safety data lookup failed"
    assert weather_result["error"] == "天气查询失败"
    _assert_not_logged(SENTINEL, capsys, caplog)


def test_property_details_request_fields_are_not_logged(
    monkeypatch, capsys, caplog
):
    import core.tools.get_property_details as details

    monkeypatch.setattr(details, "load_property_database", pd.DataFrame)
    result = details.get_property_details_impl(
        property_name=SENTINEL,
        property_address=f"14 Secret Street {SENTINEL}",
        property_url=f"https://listing.test/{SENTINEL}",
        question=f"Is this available? {SENTINEL}",
    )

    assert result["success"] is False
    _assert_not_logged(SENTINEL, capsys, caplog)


def test_poi_tool_does_not_log_address_coordinates_query_or_exception(
    monkeypatch, capsys, caplog
):
    import core.tools.search_nearby_pois as pois

    monkeypatch.setattr(pois, "poi_search_budget_s", lambda: 10.0)
    monkeypatch.setattr(pois, "coords_in_uk", lambda lat, lon: True)

    def fail(*args, **kwargs):
        raise RuntimeError(f"Overpass URL contains {SENTINEL}")

    monkeypatch.setattr(pois, "query_osm_pois", fail)
    result = pois.search_nearby_pois_impl(
        address=SENTINEL,
        poi_type="supermarket",
        user_query=f"nearby groceries {SENTINEL}",
        latitude=51.512345,
        longitude=-0.123456,
    )

    assert result["success"] is False
    assert result["error"] == "Nearby POI lookup failed"
    _assert_not_logged(SENTINEL, capsys, caplog)


def test_maps_service_logs_only_safe_metadata(monkeypatch, capsys, caplog):
    import core.maps_service as maps

    maps.overpass_mirror_state_reset()
    maps._penalise_mirror(
        f"https://{SENTINEL}.example/api/interpreter",
        f"request failed for {SENTINEL}",
    )
    maps._normalize_address_for_routing(f"UCL {SENTINEL}")

    monkeypatch.setattr(
        maps,
        "calculate_travel_basis",
        lambda *args, **kwargs: {
            "duration_minutes": 12,
            "source": "TfL Journey Planner",
        },
    )
    assert maps.calculate_travel_time(
        f"origin {SENTINEL}", f"destination {SENTINEL}"
    ) == 12

    monkeypatch.setattr(maps, "_read_versioned_cache", lambda *a, **k: ("miss", None))

    def fail_get(*args, **kwargs):
        raise RuntimeError(f"geocoder URL contains {SENTINEL}")

    monkeypatch.setattr(maps.requests, "get", fail_get)
    assert maps._free_geocode(f"private place {SENTINEL}") is None

    _assert_not_logged(SENTINEL, capsys, caplog)
    maps.overpass_mirror_state_reset()



def test_llm_and_support_modules_redact_raw_values(monkeypatch, capsys, caplog):
    import sys
    import types

    import core.amenity_map_generator as amenity_maps
    import core.llm_interface as llm
    import core.maps_service as maps
    bs4_stub = types.ModuleType("bs4")
    bs4_stub.BeautifulSoup = object
    monkeypatch.setitem(sys.modules, "bs4", bs4_stub)
    import core.scraping.legacy_scrapers.scrape_zoopla_listings as zoopla
    import core.scraping.normalize as normalize

    assert llm.extract_first_json(SENTINEL) is None
    llm.refine_criteria_with_answer(
        {
            "_original_query": f"near {SENTINEL}",
            "max_budget": 1400,
            "max_travel_time": 30,
        },
        SENTINEL,
    )

    generator = amenity_maps.PropertyAmenityMapGenerator()
    assert generator.parse_geo_location(f"{SENTINEL},not-a-number") is None

    def fail_overpass(*args, **kwargs):
        raise amenity_maps.OverpassError(f"provider URL contains {SENTINEL}")

    monkeypatch.setattr(amenity_maps, "overpass_request", fail_overpass)
    assert generator.query_osm_amenities_with_filter(
        51.5, -0.1, "supermarkets"
    ) == []

    def fail_geocode(*args, **kwargs):
        raise RuntimeError(f"geocoder URL contains {SENTINEL}")

    monkeypatch.setattr(maps, "geocode_address", fail_geocode)
    normalize._geocode_fill({"Address": SENTINEL})

    class FailingSession:
        def post(self, *args, **kwargs):
            raise zoopla.requests.exceptions.RequestException(
                f"session request contains {SENTINEL}"
            )

    monkeypatch.setattr(zoopla.requests, "Session", FailingSession)
    monkeypatch.setattr(zoopla.random, "randint", lambda *args: SENTINEL)
    assert zoopla.find_properties_zoopla(SENTINEL, 1, 500, 1500) == []

    _assert_not_logged(SENTINEL, capsys, caplog)


def test_area_stats_redacts_slug_and_database_error(monkeypatch, capsys, caplog):
    import sqlite3
    from pathlib import Path

    import core.area_stats as area_stats

    monkeypatch.setattr(area_stats, "_cache_path", lambda: Path(__file__))

    def fail_connect(*args, **kwargs):
        raise sqlite3.OperationalError(f"database path contains {SENTINEL}")

    monkeypatch.setattr(area_stats.sqlite3, "connect", fail_connect)
    result = area_stats.aggregate([SENTINEL])

    assert result[SENTINEL]["sample_size"] == 0
    _assert_not_logged(SENTINEL, capsys, caplog)


def test_area_recommendation_logs_redact_destination_and_errors(
    monkeypatch, capsys, caplog
):
    import core.recommend_areas as areas

    class EmptyCache:
        def get(self, key):
            return None

        def set(self, key, value):
            return None

    monkeypatch.setattr(areas, "AREA_RECOS_ENABLED", True)
    monkeypatch.setattr(areas, "_cache", lambda: EmptyCache())
    monkeypatch.setattr(
        areas,
        "classify_place",
        lambda value: {"city": "London", "address": SENTINEL},
    )
    monkeypatch.setattr(areas, "geocode_address", lambda value: None)

    assert asyncio.run(
        areas.generate_candidate_areas(
            SENTINEL, city="London", candidate_names=["Camden"]
        )
    ) == []

    monkeypatch.setattr(areas, "_looks_empty", lambda value: True)
    monkeypatch.setattr(areas, "get_search_snippets", lambda *args: "")
    assert asyncio.run(
        areas.generate_candidate_areas(
            SENTINEL,
            city="London",
            dest_coords={"lat": 51.5, "lng": -0.1},
        )
    ) == []

    assert asyncio.run(
        areas.recommend_areas(SENTINEL, city="London", force_refresh=True)
    ) == []

    async def no_candidates(*args, **kwargs):
        return []

    monkeypatch.setattr(areas, "generate_candidate_areas", no_candidates)
    assert asyncio.run(
        areas.recommend_areas(
            SENTINEL,
            city="London",
            dest_coords={"lat": 51.5, "lng": -0.1},
            force_refresh=True,
        )
    ) == []

    def fail_classify(*args, **kwargs):
        raise RuntimeError(f"classification failed for {SENTINEL}")

    monkeypatch.setattr(areas, "classify_place", fail_classify)
    assert asyncio.run(
        areas.recommend_areas(SENTINEL, force_refresh=True)
    ) == []

    _assert_not_logged(SENTINEL, capsys, caplog)


def test_on_demand_redacts_slug_and_exception_text(monkeypatch, capsys, caplog):
    import core.scraping.on_demand as on_demand

    class BrokenPattern:
        def finditer(self, text):
            raise RuntimeError(f"scan failed for {SENTINEL}")

    monkeypatch.setattr(on_demand, "_DEST_CANDIDATE_RE", BrokenPattern())
    assert on_demand.extract_destination_from_text(SENTINEL) is None

    def fail_scrape(*args, **kwargs):
        raise RuntimeError(f"scrape URL contains {SENTINEL}")

    monkeypatch.setattr(on_demand.onthemarket, "find_rich_onthemarket", fail_scrape)
    assert on_demand._scrape_live(SENTINEL, 0, 1, 500, 1500, 5, 1) == (
        None,
        False,
    )

    class EmptyCache:
        def get(self, key):
            return None

        def set(self, key, value):
            return None

    monkeypatch.setattr(
        on_demand, "_band_narrower_than_canonical", lambda *args: True
    )
    monkeypatch.setattr(on_demand, "_cache", lambda: EmptyCache())
    monkeypatch.setattr(on_demand, "_scrape_live", lambda *args: ([], False))
    assert on_demand._band_rescue(
        SENTINEL, "London", 0, 1, 500, 1500, 5, 1
    ) == (None, None)

    _assert_not_logged(SENTINEL, capsys, caplog)


def test_transport_and_place_reference_redact_inputs_and_errors(
    monkeypatch, capsys, caplog
):
    import requests

    import core.place_reference as places
    import core.tools.get_transport_info as transport

    def fail_request(*args, **kwargs):
        raise requests.exceptions.RequestException(
            f"request URL contains {SENTINEL}"
        )

    monkeypatch.setattr(transport.requests, "get", fail_request)
    assert transport._tfl_get(f"/Journey/{SENTINEL}", {"q": SENTINEL}) == (
        None,
        None,
    )

    def fail_geocode(*args, **kwargs):
        raise RuntimeError(f"geocoder rejected {SENTINEL}")

    monkeypatch.setattr(transport.maps_service, "geocode_address", fail_geocode)
    assert transport._geocode(SENTINEL) is None

    def fail_journey(*args, **kwargs):
        raise RuntimeError(f"journey failed for {SENTINEL}")

    monkeypatch.setattr(transport, "_do_journey", fail_journey)
    result = transport.get_transport_info_impl(
        query_type="journey",
        from_location=SENTINEL,
        to_location=SENTINEL,
        line=SENTINEL,
        user_query=SENTINEL,
    )
    assert result == {
        "success": False,
        "error": "Transport lookup failed. See tfl.gov.uk.",
    }

    monkeypatch.setattr(requests, "get", fail_request)
    assert places.nearest_stations(51.5, -0.1) is None

    _assert_not_logged(SENTINEL, capsys, caplog)


def test_generate_map_route_redacts_address_and_provider_errors(
    monkeypatch, capsys, caplog, tmp_path
):
    monkeypatch.setenv("AUTH_DB_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv(
        "CONVERSATION_DB_PATH", str(tmp_path / "conversations.sqlite3")
    )
    monkeypatch.setenv("USE_MCP_TOOLS", "0")

    import app as appmod
    from core.amenity_map_generator import PropertyAmenityMapGenerator
    from core.maps_service import OverpassError

    monkeypatch.setattr(
        PropertyAmenityMapGenerator,
        "parse_geo_location",
        lambda self, value: (51.5, -0.1),
    )

    def fail_amenities(self, lat, lon):
        raise OverpassError(f"provider URL contains {SENTINEL}")

    monkeypatch.setattr(
        PropertyAmenityMapGenerator, "fetch_all_amenities", fail_amenities
    )
    monkeypatch.setattr(
        PropertyAmenityMapGenerator,
        "generate_map_html",
        lambda self, *args, **kwargs: "<html>map</html>",
    )
    payload = {"address": SENTINEL, "geo_location": "51.5,-0.1"}
    with appmod.app.test_request_context(
        "/api/generate_map", method="POST", json=payload
    ):
        response = appmod.generate_property_map()
    assert response[1] == 200

    def fail_parse(self, value):
        raise RuntimeError(f"bad coordinates for {SENTINEL}")

    monkeypatch.setattr(PropertyAmenityMapGenerator, "parse_geo_location", fail_parse)
    with appmod.app.test_request_context(
        "/api/generate_map", method="POST", json=payload
    ):
        response = appmod.generate_property_map()
    assert response[1] == 500

    with appmod.app.test_request_context("/api/private"):
        response = appmod._handle_uncaught(RuntimeError(f"request failed: {SENTINEL}"))
    assert response[1] == 500

    _assert_not_logged(SENTINEL, capsys, caplog)





def test_global_tool_exception_boundary_redacts_output_and_logs(
    capsys, caplog
):
    from core.tool_system import Tool

    calls = []

    def fail(secret):
        calls.append(secret)
        raise RuntimeError(f"tool provider failed for {secret}")

    tool = Tool(
        name="sentinel_read",
        description="sentinel failure test",
        func=fail,
        parameters={
            "type": "object",
            "properties": {"secret": {"type": "string"}},
            "required": ["secret"],
        },
        max_retries=0,
        retry_on_error=False,
    )

    result = asyncio.run(tool.execute(secret=SENTINEL))
    validation_tool = Tool(
        name="sentinel_validation",
        description="sentinel validation test",
        func=lambda count: count,
        parameters={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    )
    validation_result = asyncio.run(validation_tool.execute(count=SENTINEL))

    assert result.success is False
    assert result.error == "Tool execution failed"
    assert calls == [SENTINEL]
    assert validation_result.success is False
    assert validation_result.error == "ValidationError: invalid parameters"
    assert SENTINEL not in str(validation_result)
    _assert_not_logged(SENTINEL, capsys, caplog)


def test_transport_cost_exception_output_is_redacted(monkeypatch, capsys, caplog):
    import core.tools.check_transport_cost as transport_cost

    def fail_fare(*args, **kwargs):
        raise ValueError(f"bad zones from {SENTINEL}")

    monkeypatch.setattr(transport_cost, "get_zonal_fare", fail_fare)
    result = asyncio.run(
        transport_cost.check_transport_cost_impl(
            start_zone=1,
            end_zone=2,
            travel_type="student",
        )
    )

    assert result == {
        "success": False,
        "error": "Fare lookup failed. Please check tfl.gov.uk/fares.",
    }
    assert SENTINEL not in str(result)
    _assert_not_logged(SENTINEL, capsys, caplog)


def test_search_rag_fallback_logs_only_exception_types(monkeypatch, capsys, caplog):
    import rag.rag_coordinator as rag_module
    import core.tools.search_properties as search

    class FailingRagCoordinator:
        def __init__(self):
            raise RuntimeError(f"model path contains {SENTINEL}")

    monkeypatch.setattr(search, "_RAG_COORDINATOR", None)
    monkeypatch.setattr(rag_module, "RAGCoordinator", FailingRagCoordinator)
    coordinator = search._get_rag_coordinator()
    assert coordinator is not None

    def fail_build():
        raise RuntimeError(f"prewarm failed for {SENTINEL}")

    monkeypatch.setattr(search, "_get_rag_coordinator", fail_build)
    search._prewarm_embedding_store()

    class FailingThread:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(f"thread failed for {SENTINEL}")

    monkeypatch.setattr(search, "_PREWARM_STARTED", False)
    monkeypatch.setenv("SEARCH_EMBED_PREWARM", "1")
    monkeypatch.setattr(search.threading, "Thread", FailingThread)
    assert search.start_embedding_prewarm() is False

    _assert_not_logged(SENTINEL, capsys, caplog)


def test_memory_and_mcp_exception_sinks_are_redacted(monkeypatch, capsys, caplog):
    import threading

    import core.mcp_client as mcp
    import rag.agent_memory as memory

    class BrokenCollection:
        def get(self, *args, **kwargs):
            raise RuntimeError(f"memory database contains {SENTINEL}")

        def query(self, *args, **kwargs):
            return {"documents": [], "ids": []}

    mem = memory.AgentMemory.__new__(memory.AgentMemory)
    mem._lock = threading.RLock()
    mem.col = BrokenCollection()
    report = mem.forget_fact(SENTINEL, ["budget"])
    assert report["complete"] is False

    def fail_llm(*args, **kwargs):
        raise RuntimeError(f"memory prompt contains {SENTINEL}")

    monkeypatch.setattr(memory, "call_ollama", fail_llm)
    mem._consolidate([SENTINEL], SENTINEL, SENTINEL)

    class QuietThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

    client = mcp.MCPToolClient("python", [])
    client._ready.set()
    client._connect_error = RuntimeError(f"MCP endpoint contains {SENTINEL}")
    monkeypatch.setattr(mcp.threading, "Thread", QuietThread)
    assert client.start() is client
    fallback = asyncio.run(
        client._fallback(
            "safe_tool", {"secret": SENTINEL}, f"transport failed for {SENTINEL}"
        )
    )
    assert fallback.error == "MCP unavailable and no fallback registry"

    _assert_not_logged(SENTINEL, capsys, caplog)

def test_provider_zoopla_and_area_compare_errors_are_redacted(monkeypatch, capsys, caplog):
    import core.scraping.provider as provider
    import core.scraping.zoopla as zoopla
    import core.tools.compare_or_rank_areas as compare

    assert provider._run_source(
        SENTINEL, {}, 1.5, 500, 1500, 1
    ) == []

    def fail_source(*args, **kwargs):
        raise RuntimeError(f"scraper URL contains {SENTINEL}")

    monkeypatch.setattr(provider, "_run_source", fail_source)
    assert provider.scrape_all(
        tasks=[{
            "name": SENTINEL,
            "radius": 1.5,
            "min_price": 500,
            "max_price": 1500,
        }],
        sources=["onthemarket"],
        limit_per_task=1,
    ) == []

    def fail_scrape(*args, **kwargs):
        raise RuntimeError(f"whole scrape failed for {SENTINEL}")

    monkeypatch.setattr(provider, "scrape_all", fail_scrape)
    provider.get_properties(force_refresh=True, allow_scrape=True)

    monkeypatch.setattr(provider, "scrape_all", lambda **kwargs: [{"URL": "safe"}])

    def fail_write(*args, **kwargs):
        raise OSError(f"cache path contains {SENTINEL}")

    monkeypatch.setattr(provider, "write_csv", fail_write)
    result = provider.get_properties(force_refresh=True, allow_scrape=True)
    assert result == [{"URL": "safe"}]

    def fail_legacy(*args, **kwargs):
        raise ImportError(f"legacy module path contains {SENTINEL}")

    monkeypatch.setattr(zoopla, "load_legacy", fail_legacy)
    assert zoopla.find_rich_zoopla(SENTINEL, 1.5, 500, 1500) == []

    async def fail_candidates(*args, **kwargs):
        raise RuntimeError(f"candidate generation failed for {SENTINEL}")

    monkeypatch.setattr(compare, "generate_candidate_areas", fail_candidates)
    assert asyncio.run(
        compare._resolve_candidates(SENTINEL, SENTINEL, 30, [SENTINEL])
    ) == []

    _assert_not_logged(SENTINEL, capsys, caplog)
