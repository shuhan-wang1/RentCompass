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


# R1-03 ── a budget removal instruction clears max_budget ─────────────────────────────
WITH_BUDGET = {"area": "Hackney", "max_budget": 2000, "budget_period": "month", "bedrooms": 2}


@pytest.mark.parametrize("message", [
    "Forget everything about the budget.",
    "Forget all about the budget.",
    "Get rid of the budget.",
    "Remove my rent limit.",
    "Scrap the max budget.",
    "Can you forget the budget please?",
    "Clear the budget and search again.",
])
def test_budget_removal_instruction_clears_the_ceiling(message):
    from core.tools.search_properties import ground_hard_constraints
    acc = _apply_explicit_criteria_updates(dict(WITH_BUDGET), message)
    assert acc.get("max_budget") is None
    # The next search cannot be handed the old ceiling by the model either.
    params, _ = ground_hard_constraints({"area": "Hackney", "max_budget": 2000}, acc, "Search again.")
    assert "max_budget" not in params


@pytest.mark.parametrize("message", [
    "Should I drop my budget?",
    "What if I removed the budget?",
    "What is my budget?",
    "Is the budget too low?",
    "Keep the budget at £2,000.",
])
def test_questions_about_the_budget_keep_it(message):
    assert _apply_explicit_criteria_updates(dict(WITH_BUDGET), message)["max_budget"] == 2000


# R1-04 ── a city word inside an institution's name does not switch the home area ─────
GATED_KX = {"area": "King's Cross", "areas": ["King's Cross"], "max_budget": 2300, "bedrooms": 1,
            "constraint_sources": {
                "area": {"source": "user_message", "value": "King's Cross", "text": "国王十字附近的一居室"},
                "areas": {"source": "user_message", "value": ["King's Cross"], "text": "国王十字附近的一居室"}}}


@pytest.mark.parametrize("message", [
    "从第一个到伦敦大学学院要多久?",
    "从第一个到伦敦国王学院要多久？",
    "去伦敦政治经济学院通勤多久",
])
def test_institution_named_after_the_city_keeps_the_gated_area(message):
    from core.tools.search_properties import _extract_area, _extract_areas, ground_hard_constraints
    assert _extract_area(message) is None and _extract_areas(message) == []
    acc = _apply_explicit_criteria_updates(dict(GATED_KX), message)
    assert acc["area"] == "King's Cross" and acc["areas"] == ["King's Cross"]
    # A model that read "伦敦" as the area is put back on the stated one.
    params, _ = ground_hard_constraints({"area": "London", "areas": ["London"]}, acc, message)
    assert params["area"] == "King's Cross" and params["areas"] == ["King's Cross"]


@pytest.mark.parametrize("message", ["我在伦敦读大学，想在伦敦找房", "帮我在伦敦找一居室"])
def test_a_plain_city_switch_still_applies(message):
    from core.tools.search_properties import _extract_area
    assert _extract_area(message) == "London"


# R1-13 ── minutes to an amenity are not a commute limit ──────────────────────────────
@pytest.mark.parametrize("message", [
    "Please remember that I need to be within 10 minutes' walk of a supermarket, and that I "
    "prefer quiet streets.",
    "I want to be 5 minutes from a park.",
    "Within a 10-minute walk of the station please.",
    "10 minutes to Tesco would be great.",
    "离超市步行10分钟以内。",
])
def test_minutes_to_an_amenity_do_not_set_max_travel_time(message):
    from core.tools.search_properties import _extract_commute_minutes
    assert _extract_commute_minutes(message) is None
    assert _apply_explicit_criteria_updates({"area": "Camden"}, message).get("max_travel_time") is None


@pytest.mark.parametrize("message, minutes", [
    ("10 min walk to the tube please, and a commute under 40 minutes to Bank", 40),
    ("Commute under 30 minutes.", 30),
    ("I work at Bank, max 45 minutes.", 45),
    ("通勤35分钟以内。", 35),
    ("A 15 minute walk to work is ideal.", 15),
])
def test_a_commute_limit_beside_an_amenity_still_counts(message, minutes):
    from core.tools.search_properties import _extract_commute_minutes
    assert _extract_commute_minutes(message) == minutes


# R1-18 ── "2 bedrooms again minimum" is a minimum ───────────────────────────────────
@pytest.mark.parametrize("message, expected", [
    ("I want 2 bedrooms again minimum.", (2, 6)),
    ("2 bedrooms at minimum.", (2, 6)),
    ("I want 2 bedrooms minimum.", (2, 6)),
    ("I want 2 bedrooms again.", (2, 2)),
])
def test_bedroom_minimum_with_a_filler_word(message, expected):
    from core.tools.search_properties import _extract_bedrooms
    assert _extract_bedrooms(message) == expected
    acc = _apply_explicit_criteria_updates({"area": "Hackney", "bedrooms": 1}, message)
    assert acc["bedrooms"] == 2 and acc["bedrooms_minimum"] is (expected[1] != expected[0])
