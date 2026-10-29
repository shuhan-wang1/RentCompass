"""Tool-result and transport regressions (R1-xx)."""
from __future__ import annotations

import json

import pytest


# R1-08 ── the cheapest itinerary's own fare reaches the model ────────────────────────
def _leg(line, src, dst, minutes, mode="tube"):
    return {"mode": mode, "lines": [line], "duration_minutes": minutes,
            "summary": f"{line} line to {dst}", "from": f"{src} Underground Station",
            "to": f"{dst} Underground Station"}


def _journey_payload():
    fastest_legs = [_leg("Metropolitan", "King's Cross St. Pancras", "Baker Street", 6),
                    _leg("Bakerloo", "Baker Street", "Paddington", 4),
                    _leg("Walk", "Paddington Underground", "London Paddington Rail Station", 10, "walking"),
                    _leg("Heathrow Express", "London Paddington", "Heathrow Terminals 2 & 3 Rail Station",
                         15, "national-rail")]
    cheapest_legs = [_leg("Victoria", "King's Cross St. Pancras", "Euston", 2),
                     _leg("Northern", "Euston", "Tottenham Court Road", 4),
                     _leg("Elizabeth", "Tottenham Court Road", "Heathrow Terminals 2 & 3 Rail Station",
                          34, "elizabeth-line")]
    summary_fast = " -> ".join(f"{l['mode']} {l['lines'][0]} to {l['to']} ({l['duration_minutes']} min)"
                               for l in fastest_legs)
    summary_cheap = " -> ".join(f"{l['mode']} {l['lines'][0]} to {l['to']} ({l['duration_minutes']} min)"
                                for l in cheapest_legs)
    fare = lambda pence, iid: {"fare_available": True, "fare_basis": "journey_itinerary",
                               "fare_pence": pence, "fare_gbp": pence / 100,
                               "fare_display": f"£{pence / 100:.2f}", "fare_itinerary_id": iid}
    return {
        "success": True, "query_type": "journey", "coverage": "london",
        "from": {"input": "King's Cross St Pancras", "resolved_station": "King's Cross St. Pancras Underground Station",
                 "naptan": "940GZZLUKSX"},
        "to": {"input": "Heathrow Airport", "resolved_station": "Heathrow Terminals 2 & 3 Underground Station",
               "naptan": "940GZZLUHRC"},
        "duration_minutes": 41, "changes_count": 2, "route_summary": summary_fast,
        "route_legs": fastest_legs, "selected_itinerary_id": "tfl:fast0001",
        "fastest_journey": {"itinerary_id": "tfl:fast0001", "duration_minutes": 41, "changes_count": 2,
                            "route_summary": summary_fast, "route_legs": fastest_legs,
                            "source": "TfL Unified API Journey Planner", **fare(2910, "tfl:fast0001")},
        "cheapest_journey": {"itinerary_id": "tfl:cheap0001", "duration_minutes": 48, "changes_count": 2,
                             "route_summary": summary_cheap, "route_legs": cheapest_legs,
                             "source": "TfL Unified API Journey Planner", **fare(1550, "tfl:cheap0001")},
        "source": "TfL Unified API Journey Planner", "from_cache": True, "cache_status": "fresh",
        "stations_used": "King's Cross St. Pancras Underground Station -> Heathrow Terminals 2 & 3 "
                         "Underground Station",
        **fare(2910, "tfl:fast0001"), "fare_matches_itinerary": True,
        "fare_note": "Adult pay-as-you-go single (contactless / Oyster). For unlimited travel see a "
                     "weekly/monthly Travelcard.",
    }


def test_transport_view_keeps_the_cheapest_fare_inside_the_model_cap():
    from core.agent_loop import _TOOLMSG_CAP_DEFAULT, _TOOLMSG_CAPS, project_transport_payload
    raw = _journey_payload()
    cap = _TOOLMSG_CAPS.get("get_transport_info", _TOOLMSG_CAP_DEFAULT)
    view = json.dumps({"success": True, "data": project_transport_payload(raw)}, ensure_ascii=False)
    assert '"fare_display": "£15.50"' in view[:cap]
    assert '"fare_display": "£29.10"' in view[:cap]
    # The raw artifact is not changed by the projection.
    assert raw["fastest_journey"]["route_legs"] == raw["route_legs"]


def test_transport_view_keeps_distinct_legs():
    from core.agent_loop import project_transport_payload
    raw = _journey_payload()
    view = project_transport_payload(raw)
    # The top-level legs are a copy of the fastest itinerary's; listed once.
    assert view["fastest_journey"]["route_legs"] == raw["route_legs"]
    assert view["route_legs"] == "same as fastest_journey.route_legs"
    assert view["cheapest_journey"]["route_legs"] == raw["cheapest_journey"]["route_legs"]
    assert set(view) == set(raw)


def test_transport_view_fits_the_cap_whole():
    from core.agent_loop import _TOOLMSG_CAP_DEFAULT, _TOOLMSG_CAPS, project_transport_payload
    cap = _TOOLMSG_CAPS.get("get_transport_info", _TOOLMSG_CAP_DEFAULT)
    view = json.dumps({"success": True, "data": project_transport_payload(_journey_payload())},
                      ensure_ascii=False)
    assert len(view) <= cap


def test_fallback_evidence_lists_the_cheapest_itinerary_fare():
    from core import candidate_validation as cv
    state = {"tool_artifacts": [{"tool": "get_transport_info", "success": True,
                                 "raw_data": _journey_payload()}],
             "extracted_context": {"reply_language": "en"}}
    out = cv.render_structured_evidence_fallback(state)
    assert "£29.10" in out and "£15.50" in out


# R1-09 ── weather resolves London districts, streets and postcodes ───────────────────
class _Resp:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


_CURRENT = {"current": {"time": "2026-10-29T12:00", "temperature_2m": 11, "weather_code": 3,
                        "wind_speed_10m": 14.5}}


@pytest.fixture
def weather_offline(monkeypatch):
    import requests
    from core import maps_service as maps
    from core.tools import get_weather as weather

    def forbidden(*args, **kwargs):
        raise AssertionError("no real HTTP in weather regressions")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(maps, "geocode_address", lambda *a, **k: None)
    return weather, maps


def _weather_get(candidates, fetched):
    def get(url, params, **kwargs):
        fetched.append((url, dict(params)))
        if "geocoding-api" in url:
            return _Resp({"results": candidates})
        return _Resp(_CURRENT)
    return get


@pytest.mark.parametrize("location, queried", [
    ("Angel, London", "Angel"),
    ("Stratford E15, London", "Stratford"),
    ("Stratford, London", "Stratford"),
    ("Newham, London", "Newham"),
])
def test_a_london_district_without_london_in_its_admin_fields_resolves(
        weather_offline, monkeypatch, location, queried):
    weather, _ = weather_offline
    fetched = []
    candidates = [
        {"name": "Stratford", "latitude": 52.19, "longitude": -1.71, "country_code": "GB",
         "admin1": "England", "admin2": "Warwickshire"},          # Stratford-upon-Avon area
        {"name": queried, "latitude": 51.54, "longitude": -0.01, "country_code": "GB",
         "admin1": "England", "admin2": "Newham"},
    ]
    monkeypatch.setattr(weather.requests, "get", _weather_get(candidates, fetched))
    result = weather.get_weather_impl(location)
    assert result["success"], result
    assert (result["latitude"], result["longitude"]) == (51.54, -0.01)
    assert fetched[0][1]["name"] == queried
    assert result["wind_speed"] == 14.5 and result["wind_speed_unit"] == "km/h"


def test_a_london_street_falls_back_to_the_address_geocoder(weather_offline, monkeypatch):
    weather, maps = weather_offline
    asked = []
    monkeypatch.setattr(weather.requests, "get", _weather_get([], []))
    monkeypatch.setattr(maps, "geocode_address",
                        lambda query, **k: asked.append(query) or {"lat": 51.565, "lng": -0.147})
    result = weather.get_weather_impl("Highgate West Hill, London N6")
    assert result["success"] and asked == ["Highgate West Hill, London N6"]
    assert (result["latitude"], result["longitude"]) == (51.565, -0.147)


def test_a_postcode_qualified_street_strips_the_postcode(weather_offline, monkeypatch):
    weather, _ = weather_offline
    fetched = []
    candidates = [{"name": "City Road", "latitude": 51.53, "longitude": -0.09, "country_code": "GB",
                   "admin1": "England", "admin2": "Islington"}]
    monkeypatch.setattr(weather.requests, "get", _weather_get(candidates, fetched))
    result = weather.get_weather_impl("City Road, London EC1V")
    assert result["success"] and fetched[0][1]["name"] == "City Road"


def test_a_geocoder_answer_outside_london_is_not_accepted(weather_offline, monkeypatch):
    weather, maps = weather_offline
    monkeypatch.setattr(weather.requests, "get", _weather_get([], []))
    monkeypatch.setattr(maps, "geocode_address", lambda *a, **k: {"lat": 53.48, "lng": -2.24})
    result = weather.get_weather_impl("Nowhere Street, London")
    assert result["success"] is False and result["error_code"] == "location_unresolved"
