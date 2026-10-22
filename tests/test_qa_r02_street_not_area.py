"""A street, station or address that starts with a known place is not an area switch (R2-05).

Live (3/3): after a Camden search listing "Gloucester Avenue, London, NW1", "What supermarkets
and parks are near Gloucester Avenue, London NW1?" ran no tool, set the conversation's area to
the CITY of Gloucester (constraint source: user_message) and answered "Excluded: Gloucester
Avenue, London, NW1: area camden is outside the requested areas". "I work near Liverpool
Street" read as an area switch to Liverpool. Pre-existing: identical at d3b4f8f.
"""
from __future__ import annotations

import pytest

from core.langgraph_agent import _apply_explicit_criteria_updates
from core.refine_results import parse_refinement
from core.tools.search_properties import _extract_area, _extract_areas, most_specific_known_area

_CAMDEN = {"area": "Camden", "areas": ["Camden"], "max_budget": 2500, "bedrooms": 1}


@pytest.mark.parametrize("message", [
    "What supermarkets and parks are near Gloucester Avenue, London NW1?",
    "I work near Liverpool Street",
    "Is it near Camden Road station?",
    "Show me what's around London Bridge",
    "What's near 10 Downing Street, Westminster?",
    "Any gyms near Finsbury Park Road?",
])
def test_r2_05_a_street_or_station_is_not_an_area(message):
    assert _extract_area(message) is None
    assert _extract_areas(message) == []
    updated = _apply_explicit_criteria_updates(dict(_CAMDEN), message)
    assert updated.get("area") == "Camden" and updated.get("areas") == ["Camden"]


def test_r2_05_the_poi_resolver_does_not_pick_the_city_a_street_starts_with():
    assert most_specific_known_area(
        "What supermarkets and parks are near Gloucester Avenue, London NW1?") is None
    assert most_specific_known_area("cafes near Liverpool Street") is None


@pytest.mark.parametrize("message,area", [
    ("Find me a 1-bed flat in Camden under £2,500/month", "Camden"),
    ("Search in Camden near Gloucester Avenue", "Camden"),
    ("Find flats near King's Cross station", "King's Cross"),   # a London area's own station
    ("Find somewhere near King's Cross St Pancras", "King's Cross"),
    ("flats in Liverpool", "Liverpool"),
    ("Let's try Manchester", "Manchester"),
    ("move to Finsbury Park", "Finsbury Park"),
    ("Gloucester", "Gloucester"),
])
def test_r2_05_real_area_requests_still_switch(message, area):
    assert _extract_area(message) == area


def test_r2_05_a_nearby_places_question_is_never_a_refinement():
    shown = [{"address": "Gloucester Avenue, London, NW1", "area": "Camden", "price": 2150},
             {"address": "Highgate West Hill, London, N6", "area": "Camden", "price": 2000}]
    assert parse_refinement(
        "What supermarkets and parks are near Gloucester Avenue, London NW1?", shown) is None
