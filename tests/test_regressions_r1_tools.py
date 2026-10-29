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
