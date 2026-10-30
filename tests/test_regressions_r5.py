"""Regression tests (T-xx / U-xx ids in each test name)."""
from __future__ import annotations

import pytest


def _state_node(accumulated: dict, message: str, history=None) -> dict:
    from core.langgraph_agent import _make_extract_preferences_node
    state = {"user_preferences": {}, "accumulated_search_criteria": dict(accumulated),
             "extracted_context": {"current_message": message,
                                   "history": history or [{"user": "Find a flat", "assistant": "ok"}]}}
    update = _make_extract_preferences_node()(state)
    return update.get("accumulated_search_criteria", state["accumulated_search_criteria"])


_CAMDEN = {"area": "Camden", "areas": ["Camden"], "bedrooms": 1, "max_budget": 2200,
           "room_type": "whole", "max_travel_time": 30}


# T-01 ── a postcode in a workplace sentence is the destination, never the home area ──
@pytest.mark.parametrize("message", [
    "I work near EC2M 7PY",
    "My office is near SW1A 1AA",
    "I work at EC2M 7PY, find me a flat",
    "I study near WC1E 6BT",
    "My job is based in EC2M 7PY",
    "我在EC2M 7PY附近上班",
    "我在WC1E 6BT上学",
    "EC2M 7PY is where I work",
])
def test_t_01_a_workplace_postcode_keeps_the_home_area(message):
    from core.langgraph_agent import _postcode_search_location
    assert _postcode_search_location(message) is None
    acc = _state_node(_CAMDEN, message)
    assert acc["area"] == "Camden" and acc["areas"] == ["Camden"]


@pytest.mark.parametrize("message,postcode", [
    ("And near SW1A 1AA postcode?", "SW1A 1AA"),
    ("What about EC1A 4HQ?", "EC1A 4HQ"),
    ("Find me a flat near SW1A 1AA", "SW1A 1AA"),
    ("I'd like to live near E1 6AN", "E1 6AN"),
    ("I work near EC2M 7PY, any flats near SW1A 1AA?", "SW1A 1AA"),
])
def test_t_01_a_postcode_named_as_where_to_look_is_still_the_area(message, postcode):
    from core.langgraph_agent import _postcode_search_location
    assert _postcode_search_location(message) == postcode
    assert _state_node(_CAMDEN, message)["area"] == postcode


# T-02 ── a home-search postcode is not also locked as the commute destination ─────────
def test_t_02_a_postcode_search_area_is_not_locked_as_the_destination(monkeypatch):
    import asyncio
    from core.scraping import on_demand
    from core.tools.search_properties import search_properties_impl
    palace = "SW1A 1AA, City of Westminster, Greater London, England, United Kingdom"

    def fake_classify(name):
        key = (name or "").strip().lower()
        if key.replace(" ", "") == "sw1a1aa":
            return {"kind": "workplace", "slug": "sw1a-1aa", "city": "london", "address": palace,
                    "source": "osm"}
        return {"kind": "area", "slug": key, "city": None, "address": None, "source": "landmark"}
    monkeypatch.setattr(on_demand, "classify_place", fake_classify)
    monkeypatch.setattr(on_demand, "is_destination",
                        lambda r: (r.get("kind") if isinstance(r, dict) else r) in ("university", "workplace"),
                        raising=False)
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the soft gate fires before any scrape")))
    res = asyncio.run(search_properties_impl(area="SW1A 1AA", current_message="And near SW1A 1AA postcode?"))
    assert res["known_criteria"]["commute_destination"] is None
    assert res["known_criteria"]["area"] == "SW1A 1AA"
    # A postcode the message names as the workplace is still locked (unchanged behaviour).
    res = asyncio.run(search_properties_impl(area="SW1A 1AA", current_message="I work at SW1A 1AA"))
    assert res["known_criteria"]["commute_destination"] == palace


# U-01 ── a TfL interchange hub id is never handed to the journey planner as a stop ───
def _no_cache(monkeypatch, gti):
    monkeypatch.setattr(gti, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(gti, "_cache_set", lambda *a, **k: None)


@pytest.mark.parametrize("location", ["Bank", "Bank station", "Bank Underground Station"])
def test_u_01_a_hub_match_prefers_the_stations_own_naptan_id(monkeypatch, location):
    from core.tools import get_transport_info as gti
    _no_cache(monkeypatch, gti)
    monkeypatch.setattr(gti, "_geocode", lambda text: (_ for _ in ()).throw(
        AssertionError("an exact station match needs no geocode")))

    def fake_tfl(path, params=None, **_k):
        assert path.startswith("/StopPoint/Search/")
        return 200, {"matches": [
            {"id": "HUBBAN", "name": "Bank", "lat": 51.5133, "lon": -0.0886, "topMostParentId": "HUBBAN"},
            {"id": "940GZZLUBNK", "name": "Bank Underground Station", "lat": 51.5133, "lon": -0.0890}]}
    monkeypatch.setattr(gti, "_tfl_get", fake_tfl)
    resolved = gti._resolve_station(location)
    assert resolved["token"] == "940GZZLUBNK" and resolved["name"] == "Bank Underground Station"


@pytest.mark.parametrize("location", ["Clapham Junction", "Clapham Junction Rail Station"])
def test_u_01_a_hub_only_match_resolves_to_the_station_at_the_hub(monkeypatch, location):
    from core.tools import get_transport_info as gti
    _no_cache(monkeypatch, gti)
    monkeypatch.setattr(gti, "_geocode", lambda text: (_ for _ in ()).throw(
        AssertionError("the hub's own station needs no geocode")))
    calls = []

    def fake_tfl(path, params=None, **_k):
        calls.append(path)
        if path.startswith("/StopPoint/Search/"):
            return 200, {"matches": [{"id": "HUBCLJ", "name": "Clapham Junction", "lat": 51.4642,
                                      "lon": -0.1704, "topMostParentId": "HUBCLJ"}]}
        assert path == "/StopPoint" and params["radius"] == 300
        return 200, {"stopPoints": [
            {"id": "HUBCLJ", "commonName": "Clapham Junction", "stopType": "TransportInterchange", "distance": 5},
            {"id": "910GCLPHMJC", "commonName": "Clapham Junction Rail Station",
             "stopType": "NaptanRailStation", "distance": 40},
            {"id": "940GZZLUCPC", "commonName": "Clapham Common Underground Station",
             "stopType": "NaptanMetroStation", "distance": 290}]}
    monkeypatch.setattr(gti, "_tfl_get", fake_tfl)
    resolved = gti._resolve_station(location)
    assert resolved["token"] == "910GCLPHMJC" and resolved["name"] == "Clapham Junction Rail Station"
    assert not resolved["token"].startswith("HUB")


def test_u_01_a_hub_with_no_station_of_its_name_falls_back_to_the_geocoder(monkeypatch):
    from core.tools import get_transport_info as gti
    _no_cache(monkeypatch, gti)
    monkeypatch.setattr(gti, "_geocode", lambda text: {"lat": 51.5033, "lng": -0.1145, "postcode": "SE1 8SW"})

    def fake_tfl(path, params=None, **_k):
        if path.startswith("/StopPoint/Search/"):
            return 200, {"matches": [{"id": "HUBWAT", "name": "Waterloo", "lat": 51.5033, "lon": -0.1145}]}
        if params and params.get("radius") == 300:
            return 200, {"stopPoints": [{"id": "HUBWAT", "commonName": "Waterloo", "stopType": "TransportInterchange"}]}
        return 200, {"stopPoints": [{"id": "940GZZLUWLO", "commonName": "Waterloo Underground Station",
                                     "stopType": "NaptanMetroStation", "distance": 60}]}
    monkeypatch.setattr(gti, "_tfl_get", fake_tfl)
    resolved = gti._resolve_station("Waterloo")
    assert resolved["token"] == "940GZZLUWLO"


def test_u_01_the_name_search_fallback_skips_hub_ids(monkeypatch):
    from core.tools import get_transport_info as gti
    _no_cache(monkeypatch, gti)
    monkeypatch.setattr(gti, "_geocode", lambda text: None)

    def fake_tfl(path, params=None, **_k):
        return 200, {"matches": [{"id": "HUBBAN", "name": "Bank-ish"},
                                 {"id": "940GZZLUBNK", "name": "Bank Underground Station"}]}
    monkeypatch.setattr(gti, "_tfl_get", fake_tfl)
    assert gti._resolve_station("Bankish")["token"] == "940GZZLUBNK"


# U-02 ── "<Area> was better. Go back." switches back to that area ─────────────────
@pytest.mark.parametrize("message,area", [
    ("No wait, Islington was better. Go back.", "Islington"),
    ("Islington is better", "Islington"),
    ("Actually Angel looks much better.", "Angel"),
    ("go back to Islington", "Islington"),
    ("back to Islington", "Islington"),
    ("还是Islington好", "Islington"),
    ("回到Islington", "Islington"),
])
def test_u_02_an_area_preference_statement_switches_back(message, area):
    acc = _state_node(_CAMDEN, message)
    assert acc["area"] == area and acc["areas"] == [area]
    assert acc["bedrooms"] == 1 and acc["max_budget"] == 2200


@pytest.mark.parametrize("message", [
    "Is Islington better?",
    "Islington is better than Camden for nightlife",
    "The second one is better",
    "Islington was better for transport, but stay with Camden",
])
def test_u_02_a_question_or_a_comparison_fact_keeps_the_area(message):
    assert _state_node(_CAMDEN, message)["area"] == "Camden"


# U-03 ── a comparison of places or shown listings changes no criteria ─────────────
@pytest.mark.parametrize("message", [
    "Compare the cheapest listing from Bethnal Green with the cheapest in Angel you showed earlier.",
    "compare the first result there with the first result in Hackney",
    "Which is cheaper, Hackney or Islington?",
    "Hackney vs Islington",
    "比较一下Hackney和Islington的房子",
])
def test_u_03_a_comparison_keeps_the_criteria(message):
    acc = _state_node(_CAMDEN, message)
    assert acc["area"] == "Camden" and acc["areas"] == ["Camden"]
    assert acc["bedrooms"] == 1 and acc["max_budget"] == 2200


def test_u_03_a_search_request_with_a_comparison_still_switches():
    assert _state_node(_CAMDEN, "Switch to Hackney and compare it with Camden")["area"] == "Hackney"
