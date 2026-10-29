"""Criteria-store regressions (R1-xx).

Each test reproduces a turn sequence run against the served loop, at the
unit level: the accumulated criteria before the turn, the user's message, and what the
criteria store must hold afterwards.
"""
from __future__ import annotations

import pytest

from core.langgraph_agent import _apply_explicit_criteria_updates

CW = "Canary Wharf, London E14 5AB"
WITH_CW = {"area": "Bethnal Green", "bedrooms": 1, "max_budget": 2200,
           "commute_destination": CW, "destination": CW}


# R1-01 ── a corrected workplace replaces the destination ─────────────────────────────
@pytest.mark.parametrize("message, destination", [
    ("Actually I work in King's Cross not Canary Wharf.", "King's Cross"),
    ("Actually, I work in Shoreditch rather than Canary Wharf.", "Shoreditch"),
    ("I work at Bank instead of Canary Wharf now.", "Bank"),
    ("Actually I work at King's Cross, not Canary Wharf.", "King's Cross"),
])
def test_workplace_correction_replaces_the_old_destination(message, destination):
    result = _apply_explicit_criteria_updates(dict(WITH_CW), message)
    assert result["commute_destination"] == destination
    assert result["destination"] == destination


def test_workplace_named_first_and_rejected_place_second_keeps_the_first():
    # The place after "not" is the rejected one, whichever was stored before.
    start = dict(WITH_CW, commute_destination="King's Cross", destination="King's Cross")
    result = _apply_explicit_criteria_updates(start, "My office is in Canary Wharf, not King's Cross.")
    assert result["commute_destination"] == CW


def test_place_names_containing_not_are_not_cut():
    result = _apply_explicit_criteria_updates({}, "I work in Notting Hill.")
    assert result["commute_destination"] == "Notting Hill"


# R1-02 ── a retraction in its own message removes the stored requirement ─────────────
@pytest.mark.parametrize("start, message", [
    ({"property_features": ["cats allowed"], "soft_preferences": ["pets must be allowed (has a cat)"]},
     "Sorry, no cat, I was thinking of someone else."),
    ({"property_features": ["pets allowed"], "soft_preferences": []}, "No dog after all."),
    ({"property_features": ["pets allowed"], "soft_preferences": ["has a dog, pets must be allowed"]},
     "抱歉，我没有狗，不需要宠物条件。"),
    ({"property_features": ["pets allowed"], "soft_preferences": []}, "不要求养宠物了。"),
])
def test_pet_retraction_in_its_own_message_clears_filter_and_preference(start, message):
    result = _apply_explicit_criteria_updates(dict(start, area="Hackney"), message)
    assert result["property_features"] == []
    assert not [p for p in result.get("soft_preferences") or [] if "pet" in p or "cat" in p or "狗" in p]


def test_bare_no_pets_building_rule_is_not_a_withdrawal():
    start = {"area": "Hackney", "property_features": ["pets allowed"]}
    result = _apply_explicit_criteria_updates(dict(start), "No pets in the building please, I am allergic.")
    assert result["property_features"] == ["pets allowed"]


@pytest.mark.parametrize("message", [
    "Never mind the gym, forget that requirement.",
    "Never mind the gym.",
    "Skip the gym please.",
])
def test_never_mind_the_amenity_removes_the_soft_preference(message):
    start = {"area": "Hackney", "soft_preferences": ["Would like gym", "Would like balcony"]}
    result = _apply_explicit_criteria_updates(dict(start), message)
    assert result["soft_preferences"] == ["Would like balcony"]


def test_never_mind_the_lift_removes_the_hard_feature():
    result = _apply_explicit_criteria_updates({"area": "Hackney", "property_features": ["lift"]},
                                              "Never mind the lift.")
    assert result["property_features"] == []


def test_a_later_search_cannot_echo_a_retracted_feature_back():
    from core.tools.search_properties import ground_hard_constraints
    start = {"area": "Bethnal Green", "property_features": ["cats allowed"]}
    acc = _apply_explicit_criteria_updates(dict(start), "Sorry, no cat, I was thinking of someone else.")
    params, _ = ground_hard_constraints({"area": "Bethnal Green", "property_features": ["cats allowed"]},
                                        acc, "Search again with my current requirements.")
    assert not params.get("property_features")
