# -*- coding: utf-8 -*-
"""Unit tests for the live-TfL transport tool (get_transport_info) and its routing.

All network access is mocked (no live TfL calls) and the persistent cache is
neutralised per-test. Covers:
  * journey + fare parsing (fare block present),
  * the Single-Fare-Finder fallback when the journey plan carries no fare
    (TfL omits fares on future-dated plans),
  * disambiguation (HTTP 300 -> best matchQuality option -> retry),
  * line status (single line, all-lines disruption summary, name filter),
  * non-London honesty ("TfL covers London only"),
  * travelcard prices from the shared 2025 table,
  * decision-layer routing (_is_transport_query / _build_transport_params),
  * the pydantic extra='ignore' schema gotcha (every param survives validation).
"""

import os
import sys

import pytest

# --- Pin the real source roots ahead of tests/ (which holds stale copies of
# `core` that would otherwise shadow the app packages under prepend mode).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "local_data_demo")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

from core.tools import get_transport_info as gti


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    """Every test runs with a cold, write-discarding cache."""
    monkeypatch.setattr(gti, "_cache_get", lambda key, ttl: None)
    monkeypatch.setattr(gti, "_cache_set", lambda key, value: None)


def _journey_response(duration=22, fare_pence=310, modes=("tube",)):
    legs = [{
        "mode": {"name": m},
        "duration": duration // len(modes),
        "routeOptions": [{"name": "Northern"}] if m == "tube" else [],
        "instruction": {"summary": f"{m} leg"},
        "departurePoint": {"commonName": "A Station"},
        "arrivalPoint": {"commonName": "B Station"},
    } for m in modes]
    j = {"duration": duration, "legs": legs}
    if fare_pence is not None:
        j["fare"] = {"totalCost": fare_pence}
    return {"journeys": [j]}


_STOPPOINTS_KGX = {"stopPoints": [
    {"id": "910GKNGX", "commonName": "London King's Cross Rail Station",
     "stopType": "NaptanRailStation", "distance": 50.0},
    {"id": "940GZZLUKSX", "commonName": "King's Cross St. Pancras Underground Station",
     "stopType": "NaptanMetroStation", "distance": 120.0},
]}

_STOPPOINTS_CYF = {"stopPoints": [
    {"id": "940GZZLUCYF", "commonName": "Canary Wharf Underground Station",
     "stopType": "NaptanMetroStation", "distance": 80.0},
]}

_LINE_STATUS_ALL = [
    {"name": "Victoria", "lineStatuses": [
        {"statusSeverityDescription": "Good Service", "statusSeverity": 10}]},
    {"name": "Central", "lineStatuses": [
        {"statusSeverityDescription": "Minor Delays", "statusSeverity": 9,
         "reason": "Central Line: Minor delays due to an earlier signal failure."}]},
    {"name": "Jubilee", "lineStatuses": [
        {"statusSeverityDescription": "Good Service", "statusSeverity": 10}]},
]

_FARETO = [{
    "header": "Single Fare Finder",
    "rows": [{
        "ticketsAvailable": [
            {"passengerType": "Adult", "ticketType": {"type": "CashSingle"},
             "ticketTime": {"type": "Anytime"}, "cost": "7.00"},
            {"passengerType": "Adult", "ticketType": {"type": "Pay as you go"},
             "ticketTime": {"type": "Peak"}, "cost": "3.60"},
            {"passengerType": "Adult", "ticketType": {"type": "Pay as you go"},
             "ticketTime": {"type": "Off Peak"}, "cost": "3.10"},
        ],
    }],
}]


def _fake_tfl(script):
    """Build a fake _tfl_get. `script` maps a path-prefix to (status, json);
    later entries win on longer prefix. Records calls on .calls."""
    calls = []

    def fake(path, params=None, timeout=15):
        calls.append((path, dict(params or {})))
        best = None
        for prefix, resp in script.items():
            if path.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
                best = (prefix, resp)
        if best is None:
            return 404, None
        status, data = best[1]
        return status, data

    fake.calls = calls
    return fake


def _geo(lat, lng, postcode=None):
    return {"lat": lat, "lng": lng, "postcode": postcode}


# ── journey + fare parsing ────────────────────────────────────────────────────

def test_journey_with_fare_block(monkeypatch):
    geos = {"kings cross": _geo(51.5308, -0.1238, "N1 9AP"),
            "canary wharf": _geo(51.5054, -0.0235, "E14 5AB")}
    monkeypatch.setattr(gti, "_geocode", lambda loc: geos[loc.lower()])
    monkeypatch.setattr(gti, "_journey_time_params", lambda: ({}, None))
    fake = _fake_tfl({
        "/StopPoint": (200, _STOPPOINTS_KGX),   # both snaps -> same list is fine
        "/Journey/JourneyResults/": (200, _journey_response(22, 310)),
    })
    monkeypatch.setattr(gti, "_tfl_get", fake)

    out = gti.get_transport_info_impl(query_type="fare", from_location="Kings Cross",
                                      to_location="Canary Wharf")
    assert out["success"] is True
    assert out["coverage"] == "london"
    assert out["duration_minutes"] == 22
    # fares must be BOTH numeric and £-formatted (critic grounding needs both)
    assert out["fare_available"] is True
    assert out["fare_pence"] == 310
    assert out["fare_gbp"] == 3.1
    assert out["fare_display"] == "£3.10"
    # metro station preferred over the (nearer) National Rail one
    assert "940GZZLUKSX" in out["from"]["naptan"]
    assert "Underground" in out["stations_used"]


def test_fare_finder_fallback_when_plan_has_no_fare(monkeypatch):
    """Future-dated plans carry no fare block -> Single Fare Finder supplies it."""
    geos = {"kings cross": _geo(51.5308, -0.1238), "canary wharf": _geo(51.5054, -0.0235)}
    monkeypatch.setattr(gti, "_geocode", lambda loc: geos[loc.lower()])
    monkeypatch.setattr(gti, "_journey_time_params",
                        lambda: ({"date": "20260708", "time": "0900", "timeIs": "Departing"},
                                 "2026-07-08 09:00 (typical daytime service)"))
    fake = _fake_tfl({
        "/StopPoint": (200, _STOPPOINTS_KGX),
        "/Journey/JourneyResults/": (200, _journey_response(23, None, ("tube", "elizabeth-line"))),
        "/Stoppoint/940GZZLUKSX/FareTo/": (200, _FARETO),
    })
    monkeypatch.setattr(gti, "_tfl_get", fake)

    out = gti.get_transport_info_impl(query_type="fare", from_location="Kings Cross",
                                      to_location="Canary Wharf")
    assert out["success"] is True
    assert out["duration_minutes"] == 23
    assert out["fare_available"] is True
    assert out["fare_gbp"] == 3.60
    assert out["fare_display"] == "£3.60"
    assert out["fare_off_peak_gbp"] == 3.10
    assert out["fare_off_peak_display"] == "£3.10"
    assert out["planned_for"].startswith("2026-07-08 09:00")
    # the CashSingle £7.00 must NOT be picked up as the PAYG fare
    assert out["fare_gbp"] != 7.0
    # the journey request carried the daytime plan params
    journey_calls = [c for c in fake.calls if c[0].startswith("/Journey/")]
    assert journey_calls and journey_calls[0][1].get("time") == "0900"


def test_disambiguation_picks_best_match_and_retries(monkeypatch):
    """HTTP 300 -> retry once with the highest-matchQuality candidates."""
    monkeypatch.setattr(gti, "_geocode", lambda loc: None)  # force StopPoint/Search path
    monkeypatch.setattr(gti, "_journey_time_params", lambda: ({}, None))
    disamb = {
        "fromLocationDisambiguation": {"disambiguationOptions": [
            {"parameterValue": "1000110", "matchQuality": 500,
             "place": {"commonName": "Kings Cross Road"}},
            {"parameterValue": "940GZZLUKSX", "matchQuality": 900,
             "place": {"commonName": "King's Cross St. Pancras Underground Station"}},
        ]},
        "toLocationDisambiguation": {"disambiguationOptions": [
            {"parameterValue": "940GZZLUCYF", "matchQuality": 950,
             "place": {"commonName": "Canary Wharf Underground Station"}},
        ]},
    }
    state = {"n": 0}

    def fake(path, params=None, timeout=15):
        if path.startswith("/StopPoint/Search/"):
            name = "kings" if "kings" in path.lower() else "canary"
            return 200, {"matches": [{"id": name.upper(), "name": f"{name} match"}]}
        if path.startswith("/Journey/"):
            state["n"] += 1
            if state["n"] == 1:
                return 300, disamb
            return 200, _journey_response(25, 310)
        return 404, None

    monkeypatch.setattr(gti, "_tfl_get", fake)
    out = gti.get_transport_info_impl(query_type="journey", from_location="Kings Cross",
                                      to_location="Canary Wharf")
    assert out["success"] is True
    assert state["n"] == 2  # exactly one retry
    # the best-quality candidate (not the first) was chosen and surfaced
    assert out["from"]["naptan"] == "940GZZLUKSX"
    assert "King's Cross St. Pancras" in out["stations_used"]
    assert out["duration_minutes"] == 25


# ── line status ───────────────────────────────────────────────────────────────

def test_line_status_single_line(monkeypatch):
    fake = _fake_tfl({"/Line/victoria/Status": (200, [_LINE_STATUS_ALL[0]])})
    monkeypatch.setattr(gti, "_tfl_get", fake)
    out = gti.get_transport_info_impl(query_type="line_status", line="Victoria")
    assert out["success"] is True
    assert out["summary"] == "Victoria line: Good Service"
    assert out["any_disruption"] is False
    assert fake.calls[0][0] == "/Line/victoria/Status"


def test_line_status_all_lines_reports_disruptions_first(monkeypatch):
    fake = _fake_tfl({"/Line/Mode/": (200, _LINE_STATUS_ALL)})
    monkeypatch.setattr(gti, "_tfl_get", fake)
    out = gti.get_transport_info_impl(query_type="line_status")
    assert out["any_disruption"] is True
    assert "Central (Minor Delays)" in out["summary"]
    assert out["lines"][0]["name"] == "Central"  # disrupted lines sorted first
    assert out["lines"][0]["reason"].startswith("Central Line")


def test_line_status_unknown_name_filters_all_lines(monkeypatch):
    fake = _fake_tfl({"/Line/Mode/": (200, _LINE_STATUS_ALL)})
    monkeypatch.setattr(gti, "_tfl_get", fake)
    out = gti.get_transport_info_impl(query_type="line_status", line="central")
    # 'central' IS a known id -> direct endpoint; use a genuinely unknown name:
    out = gti.get_transport_info_impl(query_type="line_status", line="Jubileee typo")
    assert out["success"] is True  # falls back to all-lines (unfiltered)


# ── non-London honesty ────────────────────────────────────────────────────────

def test_manchester_is_honestly_out_of_coverage(monkeypatch):
    monkeypatch.setattr(gti, "_geocode",
                        lambda loc: _geo(53.4775, -2.2311, "M1 2PB"))  # Manchester
    fake = _fake_tfl({})
    monkeypatch.setattr(gti, "_tfl_get", fake)
    out = gti.get_transport_info_impl(query_type="fare",
                                      from_location="Manchester Piccadilly",
                                      to_location="Manchester Victoria")
    assert out["success"] is True
    assert out["coverage"] == "outside_london"
    assert "only covers London" in out["message"]
    # No journey call was ever made (we never fabricate a fare)
    assert not any(c[0].startswith("/Journey/") for c in fake.calls)
    assert "fare_gbp" not in out


def test_outward_only_postcode_is_stripped_for_geocoding(monkeypatch):
    """Scraped listing addresses end in outward-only postcodes ("..., WC1X") which
    Nominatim rejects; the resolver must retry without the trailing outward code."""
    seen = []

    def fake_geo(loc):
        seen.append(loc)
        if loc.rstrip().endswith(("WC1X", "WC1E")):
            return None
        return _geo(51.5281, -0.1190, "WC1X 8DP")

    monkeypatch.setattr(gti, "_geocode", fake_geo)
    monkeypatch.setattr(gti, "_journey_time_params", lambda: ({}, None))
    fake = _fake_tfl({
        "/StopPoint": (200, _STOPPOINTS_KGX),
        "/Journey/JourneyResults/": (200, _journey_response(9, 310)),
    })
    monkeypatch.setattr(gti, "_tfl_get", fake)
    out = gti.get_transport_info_impl(query_type="journey",
                                      from_location="Grays Inn Road, London, WC1X",
                                      to_location="Gower Street, London, WC1E")
    assert out["success"] is True
    assert out["duration_minutes"] == 9
    assert "Grays Inn Road, London" in seen  # the stripped retry happened


def test_no_nearby_station_falls_back_to_coordinates(monkeypatch):
    monkeypatch.setattr(gti, "_geocode", lambda loc: _geo(51.50, -0.10, "SE1 1AA"))
    monkeypatch.setattr(gti, "_journey_time_params", lambda: ({}, None))
    fake = _fake_tfl({
        "/StopPoint": (200, {"stopPoints": []}),
        "/Journey/JourneyResults/": (200, _journey_response(30, None)),
        # FareTo must NOT be called for coordinate tokens
    })
    monkeypatch.setattr(gti, "_tfl_get", fake)
    out = gti.get_transport_info_impl(query_type="journey", from_location="somewhere SE1",
                                      to_location="elsewhere SE1")
    assert out["success"] is True
    assert out["fare_available"] is False
    assert "tfl.gov.uk" in out["fare_note"]
    assert not any(c[0].startswith("/Stoppoint/") for c in fake.calls)


# ── travelcard (static shared table, no network) ─────────────────────────────

def test_travelcard_student_zone2():
    out = gti.get_transport_info_impl(query_type="travelcard", end_zone=2,
                                      travel_type="student")
    assert out["success"] is True
    assert out["zones"] == "Zone 1-2"
    assert out["monthly_gbp"] == 114.80
    assert out["monthly_display"] == "£114.80"
    assert out["weekly_display"] == "£29.80"
    assert "Student" in out["user_type"]


def test_travelcard_without_zone_asks_for_it(monkeypatch):
    monkeypatch.setattr(gti, "_geocode", lambda loc: None)
    out = gti.get_transport_info_impl(query_type="travelcard")
    assert out["success"] is False
    assert "zone" in out["error"].lower()


# ── auto query-type inference ─────────────────────────────────────────────────

@pytest.mark.parametrize("q,frm,to,line,expected", [
    ("are there delays on the victoria line?", "", "", "victoria", "line_status"),
    ("what would a monthly travelcard cost?", "", "", "", "travelcard"),
    ("how much is the tube from A to B?", "A", "B", "", "fare"),
    ("how do I get from the flat to UCL?", "the flat", "UCL", "", "journey"),
])
def test_infer_query_type(q, frm, to, line, expected):
    assert gti._infer_query_type(q, frm, to, line) == expected


# ── pydantic schema gotcha ────────────────────────────────────────────────────

def test_all_read_params_are_declared_and_survive_validation():
    """Tool.execute builds a pydantic model with extra='ignore': every kwarg the
    impl reads MUST be declared in the schema or it is silently dropped."""
    tool = gti.get_transport_info_tool
    payload = {"query_type": "fare", "from_location": "A", "to_location": "B",
               "line": "victoria", "end_zone": 3, "travel_type": "student",
               "user_query": "how much?"}
    assert set(payload) <= set(tool.parameters["properties"])
    dumped = tool.input_model.model_validate(payload).model_dump(exclude_none=True)
    assert dumped == payload


# ── decision-layer routing (needs langgraph) ─────────────────────────────────

@pytest.fixture(scope="module")
def lga():
    pytest.importorskip("langgraph")
    import importlib
    return importlib.import_module("core.langgraph_agent")


@pytest.mark.parametrize("q,expected", [
    ("how much is the tube from Kings Cross to Canary Wharf?", True),
    ("how much would the tube cost from there to UCL and how long does it take?", True),
    ("what would a monthly travelcard cost?", True),
    ("how do I get from the flat to UCL?", True),
    ("are there delays on the Victoria line?", True),
    ("tube fare in Manchester", True),
    # negatives: existing tools keep their queries
    ("what's the monthly commute cost from SW8 1RZ to UCL?", False),
    ("find me a flat near UCL under £1500", False),
    ("which of these is closest to the university?", False),
    ("how do i get a guarantor?", False),
    ("is the second one safe?", False),
])
def test_is_transport_query(lga, q, expected):
    assert lga._is_transport_query(q.lower()) is expected


def test_transport_params_deictic_start_resolves_to_last_result(lga):
    results = [{"name": "Nine Elms Flat",
                "address": "Flat 4, Nine Elms Lane, London SW8 1RZ",
                "price": "£1400/month"}]
    ec = {"current_message": "how much would the tube cost from there to UCL "
                             "and how long does it take?",
          "last_results": results}
    p = lga._build_transport_params(ec["current_message"], ec, {})
    assert p["tool"] == "get_transport_info"
    assert p["params"]["from_location"] == "Flat 4, Nine Elms Lane, London SW8 1RZ"
    assert p["params"]["to_location"].startswith("University College London")
    assert p["params"]["user_query"]  # required for query-type inference


def test_transport_params_station_to_station_and_known_destination(lga):
    ec = {"current_message": "how much is the tube from Kings Cross to Canary Wharf?"}
    p = lga._build_transport_params(ec["current_message"], ec, {})
    assert p["params"]["from_location"] == "Kings Cross"
    assert p["params"]["to_location"].startswith("Canary Wharf")


def test_transport_params_line_status(lga):
    ec = {"current_message": "are there delays on the Victoria line?"}
    p = lga._build_transport_params(ec["current_message"], ec, {})
    assert p["params"]["line"].lower() == "victoria"
    assert "from_location" not in p["params"] or p["params"].get("from_location")


def test_transport_params_travelcard_zone_and_student(lga):
    ec = {"current_message": "what would a monthly zone 1-3 travelcard cost for a student?"}
    p = lga._build_transport_params(ec["current_message"], ec, {})
    assert p["params"]["end_zone"] == 3
    assert p["params"]["travel_type"] == "student"


def test_check_transport_cost_without_zone_redirects_to_live_tool(lga):
    p = lga._build_tool_params("check_transport_cost",
                               "how much is transport to uni?",
                               {"current_message": "how much is transport to uni?"},
                               None, {})
    assert p["tool"] == "get_transport_info"


def test_capabilities_note_mentions_tfl_and_keeps_ons_limit(lga):
    assert "TfL" in lga.CAPABILITIES_NOTE
    assert "line status" in lga.CAPABILITIES_NOTE
    assert "ONS" in lga.CAPABILITIES_NOTE      # the honest limitation stays
    assert "ons.gov.uk" in lga.CAPABILITIES_NOTE
    # and it is actually injected into direct answers
    state = {"user_query": "what can you not do?", "extracted_context": {},
             "user_preferences": {}, "accumulated_search_criteria": {},
             "tool_decision": {"tool": "direct_answer"}}
    prompt = lga._build_generation_prompt(state)
    assert "What I cannot do" in prompt
    assert "TfL" in prompt
