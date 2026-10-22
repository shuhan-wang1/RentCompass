"""A four-area comparison does not narrow the search area to one of the compared areas (r6-C4).

r11 acceptance, rid 05c6dbf1ce6844d9bcaab0e16b3790ed (conv 6aaff289…): after "Compare Camden,
Islington, Hackney and Bow for a 1-bed, and rank them for me — I'd commute to Camden Road
Station, N7." the stored ``view_state.criteria`` read area "Islington", areas ["Islington"],
with ``constraint_sources.area`` pointing at that message. The turn ran only
compare_or_rank_areas and calculate_commute, neither of which writes criteria: the write came
from the user-message parser, whose follow-up detector took "Islington" — the only list item
with a comma on BOTH sides — for a bare one-word reply ("Islington.").

The code already treats a comparison as an evaluation, not a search-area statement: a list of
places becomes ``areas`` only under a home/search predicate ("find / live / search in ..."),
and two- and three-area comparisons never wrote an area. A four-area comparison now behaves
the same way: it leaves the user's search area as it was.
"""
from __future__ import annotations

import copy

from core.criteria_contract import reconcile_search_criteria
from core.langgraph_agent import _apply_explicit_criteria_updates
from core.tools.search_properties import _extract_area, _extract_areas, names_follow_up_area

# The recorded user message of rid 05c6dbf1… (conversations.sqlite3, messages.content).
R11_COMPARE = ("Compare Camden, Islington, Hackney and Bow for a 1-bed, and rank them for me — "
               "I'd commute to Camden Road Station, N7.")


def test_r11_four_area_compare_writes_no_search_area():
    after = _apply_explicit_criteria_updates({}, R11_COMPARE)
    assert after.get("area") is None and not after.get("areas")
    assert "area" not in after.get("constraint_sources", {})
    assert "areas" not in after.get("constraint_sources", {})
    # The rest of the message is still the user's criteria.
    assert after["commute_destination"] == "Camden Road Station, N7"
    assert (after["bedrooms"], after["room_type"]) == (1, "whole")
    # What the panel receives after the turn (no search echo: the turn ran no search).
    view = reconcile_search_criteria(after, {})
    assert view["area"] is None and view["areas"] == []


def test_r11_compare_keeps_an_existing_search_area():
    before = {"area": "Camden", "areas": ["Camden"], "constraint_sources": {
        "area": {"source": "user_message", "value": "Camden", "text": "1-bed in Camden please"},
        "areas": {"source": "user_message", "value": ["Camden"], "text": "1-bed in Camden please"}}}
    after = _apply_explicit_criteria_updates(copy.deepcopy(before), R11_COMPARE)
    assert after["area"] == "Camden" and after["areas"] == ["Camden"]
    assert after["constraint_sources"]["area"]["text"] == "1-bed in Camden please"


def test_no_compared_area_is_a_follow_up_switch():
    assert _extract_areas(R11_COMPARE) == [] and _extract_area(R11_COMPARE) is None
    # fc_loop keeps a model-chosen area only when the user's own follow-up names it.
    for area in ("Camden", "Islington", "Hackney", "Bow"):
        assert not names_follow_up_area(R11_COMPARE, area), area


def test_two_and_three_area_compares_are_unchanged():
    for message in ("Compare Camden and Islington for a 1-bed.",
                    "Compare Camden, Islington and Hackney for a 1-bed."):
        assert _apply_explicit_criteria_updates({}, message).get("area") is None


def test_single_place_follow_ups_and_real_lists_still_parse():
    expected = {
        "and Chelsea?": ["Chelsea"], "Bow?": ["Bow"], "Islington": ["Islington"],
        "Camden, please": ["Camden"], "go back to Camden": ["Camden"],
        "Make that Poplar": ["Poplar"], "Let's try Hackney, then.": ["Hackney"],
        # A city after a bare comma qualifies the place; it does not make a list.
        "and Hackney, London?": ["Hackney"], "what about Salford, Manchester?": ["Salford"],
        # A list under a search predicate is a multi-area search, as before.
        "Search in Camden, Islington and Hackney": ["Camden", "Islington", "Hackney"],
        "Find a flat in Camden, Islington or Hackney": ["Camden", "Islington", "Hackney"],
    }
    for message, areas in expected.items():
        after = _apply_explicit_criteria_updates({}, message)
        assert after.get("areas") == areas, message
        assert after.get("area") == areas[0], message
    assert names_follow_up_area("and Hackney, London?", "Hackney")
    assert names_follow_up_area("how about Kew?", "Kew")
