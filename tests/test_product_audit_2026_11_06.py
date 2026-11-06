"""Offline regressions for the November product audit.

The listings and TfL replies here are synthetic fixtures, not live API evidence.
"""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace

import pytest
import requests
from langchain_core.messages import AIMessage, ToolMessage

from core import maps_service as maps
from core.agent_loop import _TOOLMSG_CAPS, build_fc_nodes, project_search_payload, project_transport_payload
from core.candidate_validation import validate_search_payload
from core.tools import get_transport_info as transport
from tests.test_fc_loop import FakeChat, FakeProvider, FakeResult, FakeSpec, _base_state, _drive, _tc
from uk_rent_agent.config import Config


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("This audit regression must not call external HTTP")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(transport, "_cache_get", lambda *args: None)
    monkeypatch.setattr(transport, "_cache_set", lambda *args: None)


def search_fixture(count=3):
    rows = [{
        "rank": index + 1, "address": f"Audit House {index + 1}, Stratford E15",
        "price": f"£{1600 + min(index, 2) * 100}/month", "bedrooms": 1,
        "property_type": "Flat", "area": "stratford", "room_type": "whole",
        "budget_status": "✅ Within budget", "match_type": "similar_suggestion",
        "travel_time": f"{52 - index} min to UCL", "travel_time_source": "routing",
        "url": f"https://www.onthemarket.com/details/{90000000 + index}/",
        "source": "OnTheMarket (cached)", "possibly_outdated": True,
        "explanation": "A synthetic flat used only for offline regression testing. " * 8,
        "description": "Fixture description, not a current rental listing. " * 50,
        "images": ["https://example.invalid/photo.jpg"] * 10,
    } for index in range(count)]
    raw = {
        "success": True, "status": "no_exact_match_but_similar",
        "message": "No listing matched every condition.",
        "suggestion": "These alternatives do not meet the commute limit.",
        "search_criteria": {"area": "stratford", "max_budget": 1800,
                            "bedrooms": 1, "commute_destination": "UCL", "max_commute_time": 35},
        "recommendations": rows, "similar_properties": copy.deepcopy(rows),
    }
    evidence = [{"candidate_key": "url:" + row["url"].rstrip("/"),
                 "from_address": row["address"], "to_address": "UCL", "mode": "transit",
                 "evidence_status": "success", "success": True, "duration_minutes": 52 - index,
                 "source": "TfL Journey Planner"} for index, row in enumerate(rows)]
    return validate_search_payload(raw, commute_evidence=evidence)


def test_search_projection_keeps_links_even_after_optional_fields_are_dropped():
    raw = search_fixture(8)
    before = copy.deepcopy(raw)
    view = project_search_payload(raw, _TOOLMSG_CAPS["search_properties"])
    text = json.dumps(view, ensure_ascii=False)
    for row in raw["similar_properties"]:
        assert row["url"] in text
        assert row["travel_time"] in text
    assert raw == before


def test_projection_does_not_discard_new_evidence_for_a_repeated_listing():
    row = {"address": "Synthetic Court", "url": "https://example.invalid/listing/1",
           "price": "£1800/month"}
    raw = {"recommendations": [row], "excluded_candidates": [dict(
        row, travel_time="50 min to UCL", listing_conditions=[
            {"condition": "own_kitchen", "status": "not_met", "quote": "shared kitchen"}])]}
    text = json.dumps(project_search_payload(raw, 6000))
    assert "50 min to UCL" in text and "shared kitchen" in text


def test_final_model_message_keeps_every_alternative_link_and_commute():
    raw = search_fixture(8)
    provider = FakeProvider([FakeSpec("search_properties")], {"search_properties": FakeResult(True, raw)})
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("search_properties", {"user_query": "flats"}, "s1")]),
        AIMessage(content="done"),
    ])
    state = asyncio.run(_drive(build_fc_nodes(provider, agent_llm=chat), _base_state()))
    message = next(m.content for m in state["messages"] if isinstance(m, ToolMessage))
    assert len(message) <= _TOOLMSG_CAPS["search_properties"]
    assert "...[truncated]" not in message
    for row in raw["similar_properties"]:
        assert row["url"] in message
        assert row["travel_time"] in message


@pytest.mark.parametrize("language", ["en", "zh"])
def test_alternatives_narrative_uses_the_same_links_and_commutes_as_cards(language):
    raw = search_fixture()
    result = build_fc_nodes(FakeProvider([], {}))["format_output_fc"]({
        "tool_artifacts": [{"tool": "search_properties", "raw_data": raw}],
        "user_preferences": {}, "accumulated_search_criteria": raw["search_criteria"],
        "final_response": "Direct links are unavailable.\n| Audit House 3 | £1800/month | — |",
        "extracted_context": {"reply_language": language},
    })
    assert result["response_type"] == "search"
    assert not result["tool_data"]["eligible_recommendations"]
    cards = result["tool_data"]["recommendations"]
    assert len(cards) == 3
    for card in cards:
        assert card["url"] in result["final_response"]
        assert card["travel_time"] in result["final_response"]
        assert card["candidate_status"] == "excluded"
    assert "Direct links are unavailable" not in result["final_response"]
    assert "50 min to UCL" in result["final_response"]


@pytest.mark.parametrize("name", ["Euston Square", "Euston Square Underground Station",
                                   "Euston Road, London NW1", "London Bridge Road"])
def test_a_landmark_substring_must_not_replace_a_different_place(name):
    assert maps._normalize_address_for_routing(name) == name


def test_exact_landmark_alias_still_resolves():
    assert maps._normalize_address_for_routing("Euston") == "Euston Station, London NW1 2RT"
    assert maps._normalize_address_for_routing("UCL") == "Gower Street, London WC1E 6BT"


@pytest.mark.parametrize("name", ["Euston Square", "Euston Square Underground Station, London"])
def test_exact_station_lookup_uses_euston_square_without_geocoding(monkeypatch, name):
    def get(path, params=None, timeout=15):
        assert path.startswith("/StopPoint/Search/")
        return 200, {"matches": [
            {"id": "test-euston", "name": "Euston Underground Station"},
            {"id": "test-square", "name": "Euston Square Underground Station"},
        ]}
    monkeypatch.setattr(transport, "_tfl_get", get)
    monkeypatch.setattr(transport, "_geocode", lambda *a: pytest.fail("Exact lookup must win"))
    out = transport._resolve_station(name)
    assert out["token"] == "test-square" and out["name"] == "Euston Square Underground Station"


@pytest.mark.parametrize("status,data", [(200, {"matches": []}), (503, None)])
@pytest.mark.parametrize("name", ["Euston Square", "Euston Square Underground Station"])
def test_empty_or_unavailable_station_lookup_cannot_select_a_nearby_station(monkeypatch, status, data, name):
    def get(path, params=None, timeout=15):
        assert path.startswith("/StopPoint/Search/")
        return status, data
    monkeypatch.setattr(transport, "_tfl_get", get)
    monkeypatch.setattr(transport, "_geocode", lambda *a: {"lat": 51.528, "lng": -0.134})
    out = transport._resolve_station(name)
    assert out["token"] is None and out["needs_clarification"] is True


def test_station_resolution_ignores_old_substitution_cache_and_does_not_cache_failure(monkeypatch):
    keys, writes = [], []
    def cached(key, ttl):
        keys.append(key)
        old = transport.create_cache_key("tfl_resolve_station_v4", "euston square")
        return {"token": "test-euston", "name": "Euston"} if key == old else None
    monkeypatch.setattr(transport, "_cache_get", cached)
    monkeypatch.setattr(transport, "_cache_set", lambda *a: writes.append(a))
    monkeypatch.setattr(transport, "_tfl_get", lambda *a, **k: (503, None))
    monkeypatch.setattr(transport, "_geocode", lambda *a: {"lat": 51.528, "lng": -0.134})
    out = transport._resolve_station("Euston Square")
    assert out["token"] is None and out["needs_clarification"] is True
    assert keys == [transport.create_cache_key("tfl_resolve_station_v5", "euston square")]
    assert writes == []  # an outage must not freeze this failure for a week


def test_exact_hub_lookup_cannot_use_a_nearby_station_with_a_different_name(monkeypatch):
    radii = []
    def get(path, params=None, timeout=15):
        if path.startswith("/StopPoint/Search/"):
            return 200, {"matches": [{"id": "HUBTEST", "name": "Euston Square", "lat": 51.52, "lon": -0.13}]}
        assert path == "/StopPoint"
        radii.append(params["radius"])
        return 200, {"stopPoints": [{"id": "test-euston", "commonName": "Euston Underground Station",
                                      "stopType": "NaptanMetroStation", "distance": 50}]}
    monkeypatch.setattr(transport, "_tfl_get", get)
    monkeypatch.setattr(transport, "_geocode", lambda *a: {"lat": 51.52, "lng": -0.13})
    out = transport._resolve_station("Euston Square")
    assert out["token"] is None and out["needs_clarification"] is True
    assert radii == [300, 1500]


@pytest.mark.parametrize("name", ["Euston Square", "Euston Square Underground Station"])
def test_inexact_station_search_requests_clarification_without_geographic_substitution(monkeypatch, name):
    calls = []
    def get(path, params=None, timeout=15):
        calls.append(path)
        if path.startswith("/StopPoint/Search/"):
            return 200, {"matches": [{"id": "test-euston", "name": "Euston Underground Station"}]}
        pytest.fail("An inexact named-station lookup must not plan or price another station")
    monkeypatch.setattr(transport, "_tfl_get", get)
    monkeypatch.setattr(transport, "_geocode", lambda *a: {
        "lat": 51.528, "lng": -0.134, "postcode": "NW1 2RT"})
    out = transport._do_journey(name, name, want_fare=True)
    assert out["success"] is False
    assert out["needs_clarification"] is True
    assert name in out["error"]
    assert "fare_gbp" not in out and "duration_minutes" not in out
    assert all(path.startswith("/StopPoint/Search/") for path in calls)


def test_journey_disambiguation_cannot_replace_an_exact_station(monkeypatch):
    monkeypatch.setattr(transport, "_resolve_station", lambda text: {
        "input": text, "in_london": True, "station": True, "token": "test-square",
        "name": "Euston Square Underground Station", "resolution": "exact_station"})
    calls = []
    def get(path, params=None, timeout=15):
        calls.append(path)
        return 300, {"toLocationDisambiguation": {"disambiguationOptions": [
            {"parameterValue": "test-euston", "matchQuality": 999,
             "place": {"commonName": "Euston Underground Station"}}]}}
    monkeypatch.setattr(transport, "_tfl_get", get)
    out = transport._do_journey("Euston Square", "Euston Square", want_fare=True)
    assert out["success"] is False and out["needs_clarification"] is True
    assert len(calls) == 1 and "fare_gbp" not in out


def test_station_clarification_precedes_large_option_lists_in_the_model_view():
    raw = {"success": False, "station_options": [{"name": "Other station"}] * 100,
           "needs_clarification": True, "error": "Please confirm Euston Square; no exact match was verified."}
    view = project_transport_payload(raw)
    assert list(view)[:3] == ["success", "needs_clarification", "error"]


def test_exact_requested_route_keeps_station_pair_fare_caveat(monkeypatch):
    calls = []
    def get(path, params=None, timeout=15):
        calls.append((path, params))
        if path.startswith("/StopPoint/Search/"):
            return 200, {"matches": [
                {"id": "test-stratford", "name": "Stratford Underground Station"},
                {"id": "test-square", "name": "Euston Square Underground Station"}]}
        assert path == "/Journey/JourneyResults/test-stratford/to/test-square"
        return 200, {"journeys": [{"duration": 27, "legs": []}]}
    monkeypatch.setattr(transport, "_tfl_get", get)
    def fare(start, end):
        assert (start, end) == ("test-stratford", "test-square")
        return {"peak_gbp": 3.6}
    monkeypatch.setattr(transport, "_station_fare", fare)
    out = transport.get_transport_info_impl(
        query_type="fare", from_location="Stratford station", to_location="Euston Square",
        date="2026-11-09", time="08:30")
    assert out["success"] and out["to"]["resolved_station"] == "Euston Square Underground Station"
    assert out["duration_minutes"] == 27  # synthetic fixture, not a verified live duration
    assert calls[-1][1] == {"date": "20261109", "time": "0830", "timeIs": "Departing"}
    assert out["fare_basis"] == "station_pair" and out["fare_matches_itinerary"] is False
    assert out["fare_itinerary_id"] is None and "not a fare confirmed" in out["fare_note"]


@pytest.mark.parametrize("setting", ["REQUIRE_AUTH", "ALLOW_LEGACY_CLIENT_USER_ID"])
def test_identity_boundary_configuration_rejects_misspelled_booleans(monkeypatch, setting):
    monkeypatch.setenv(setting, "treu")
    with pytest.raises(ValueError, match=setting):
        Config.from_env()


@pytest.mark.parametrize("value,expected", [("true", True), ("1", True), ("yes", True),
                                           ("false", False), ("0", False), ("off", False)])
def test_identity_boundary_configuration_accepts_documented_boolean_spellings(monkeypatch, value, expected):
    monkeypatch.setenv("REQUIRE_AUTH", value)
    monkeypatch.setenv("ALLOW_LEGACY_CLIENT_USER_ID", value)
    config = Config.from_env()
    assert config.require_auth is expected and config.allow_legacy_client_user_id is expected


def test_legacy_client_identity_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_LEGACY_CLIENT_USER_ID", raising=False)
    assert Config.from_env().allow_legacy_client_user_id is False


@pytest.mark.parametrize("authenticated,legacy", [(False, False), (True, False), (True, True)])
def test_http_conversations_ignore_forged_body_header_and_query_id(tmp_path, monkeypatch, authenticated, legacy):
    import app as runtime
    from uk_rent_agent.web.auth_store import AuthStore
    from uk_rent_agent.web.conversation_store import ConversationStore

    store = ConversationStore(tmp_path / "conversations.sqlite3")
    monkeypatch.setattr(runtime, "conversation_store", store)
    monkeypatch.setattr(runtime, "auth_store", AuthStore(tmp_path / "auth.sqlite3"))
    monkeypatch.setattr(runtime, "_runtime_config", replace(
        runtime._runtime_config, require_auth=authenticated, allow_legacy_client_user_id=legacy))
    monkeypatch.setattr(runtime._api_rate_limiter, "allow", lambda *a, **k: (True, 0))
    a, b = runtime.app.test_client(), runtime.app.test_client()
    try:
        if authenticated:
            for client, username in ((a, "audit-user-a"), (b, "audit-user-b")):
                assert client.post("/api/auth/register", json={
                    "username": username, "password": "synthetic-password"}).status_code == 200
        own_a = a.post("/api/conversations", json={"title": "Synthetic marker A"}).json["conversation"]
        with a.session_transaction() as cookie:
            forged_id = cookie["auth_user_id" if authenticated else "user_id"]
        own_b = b.post("/api/conversations?user_id=" + forged_id,
                       headers={"X-User-Id": forged_id},
                       json={"user_id": forged_id, "title": "Synthetic marker B"}).json["conversation"]
        with b.session_transaction() as cookie:
            assert cookie["auth_user_id" if authenticated else "user_id"] != forged_id
        visible = b.get("/api/conversations?user_id=" + forged_id, headers={"X-User-Id": forged_id})
        assert [c["id"] for c in visible.json["conversations"]] == [own_b["id"]]
        assert b.get(f"/api/conversations/{own_a['id']}/messages?user_id={forged_id}",
                     headers={"X-User-Id": forged_id}).status_code == 404
        assert [c["id"] for c in a.get("/api/conversations").json["conversations"]] == [own_a["id"]]
    finally:
        store.close()
