"""Round-2 blind-evaluation criteria failures (R2-12 .. R2-17)."""
from __future__ import annotations

import json
import logging

import pytest

from core.langgraph_agent import _apply_explicit_criteria_updates, extract_preferences_from_message
from core.tools.search_properties import (_extract_area, _property_feature_evidence,
                                          area_list_edit, names_follow_up_area)

_BASE = {"area": "Camden", "areas": ["Camden"], "max_budget": 2400, "bedrooms": 1}


# ── R2-12: deterministic RecursionError (502, 0 LLM calls, 12/12) ─────────────────────
@pytest.mark.parametrize("message,feature", [
    ("Please remember that my favourite area is Angel and I need step-free access.", "step-free"),
    ("My favourite area is Angel and I need a lift.", "lift"),
    ("favourite borough is Camden and I need a lift", "lift"),
])
def test_r2_12_a_reaffirmed_feature_requirement_does_not_recurse(message, feature):
    updated = _apply_explicit_criteria_updates(dict(_BASE), message)
    assert feature in (updated.get("property_features") or [])
    assert updated["area"] == "Camden"


def test_r2_12_turn_failures_log_their_exception_type():
    from uk_rent_agent.observability import JsonFormatter
    record = logging.LogRecord("app", logging.ERROR, __file__, 1, "agent.turn.execution_failed",
                               None, None)
    record.error_type = "RecursionError"
    assert json.loads(JsonFormatter().format(record))["error_type"] == "RecursionError"


# ── R2-13: "Switch to X, drop Y" / "Go back to X" ───────────────────────────────────
def test_r2_13_switch_and_drop_then_go_back():
    first = _apply_explicit_criteria_updates(
        dict(_BASE), "1-bed flat in Camden under £2,400, no commute")
    second = _apply_explicit_criteria_updates(first, "Switch to Clapham, drop Camden.")
    assert second["area"] == "Clapham" and second["areas"] == ["Clapham"]
    third = _apply_explicit_criteria_updates(
        second, "Go back to Camden and make it 2 beds with budget £2,400.")
    assert third["area"] == "Camden" and third["areas"] == ["Camden"]
    assert area_list_edit("Switch to Clapham, drop Camden.", ["Camden"]) == ["Clapham"]


# ── R2-14: covered-but-untabled areas and short follow-ups ───────────────────────────
@pytest.mark.parametrize("message,area", [
    ("Switch to Angel instead.", "Angel"),
    ("Actually make that Angel and budget £1,800.", "Angel"),
    ("how about Bow?", "Bow"),
    ("what about Bloomsbury?", "Bloomsbury"),
    ("Switch to Euston", "Euston"),
    ("Holborn instead please", "Holborn"),
    ("and Chelsea?", "Chelsea"),
    ("and Newcastle?", "Newcastle"),
    ("Let's try Canning Town", "Canning Town"),
])
def test_r2_14_an_explicit_follow_up_switches_area(message, area):
    assert _extract_area(message) == area
    updated = _apply_explicit_criteria_updates(dict(_BASE), message)
    assert updated["area"] == area


@pytest.mark.parametrize("message", [
    "Angel investors are nice", "I'd love a bow window", "Is Holborn safe?",
    "the angel of the north is far",
])
def test_r2_14_common_words_are_not_area_switches(message):
    assert _extract_area(message) is None


def test_r2_14_a_model_area_the_user_named_is_recognised():
    assert names_follow_up_area("how about Kew?", "Kew")
    assert names_follow_up_area("and Newcastle?", "Newcastle upon Tyne")
    assert not names_follow_up_area("Drop Camden from the list", "Camden")


def test_r2_14_injection_keeps_the_user_named_area(monkeypatch):
    """"how about Kew?" (a place no table knows): the model's area reaches the tool instead of
    the accumulated one."""
    from tests.test_overnight_null_injection import _run, _strict_null_args
    from tests.test_agent_contract_guards import _state

    monkeypatch.setenv("DEEPSEEK_STRICT", "1")
    message = "how about Kew?"
    state = _state(message)
    state["accumulated_search_criteria"] = {"area": "Hackney", "areas": ["Hackney"],
                                            "max_budget": 1500, "no_commute": True}
    args = _strict_null_args("search_properties", {"user_query": "studio in Kew under £1500",
                                                   "area": "Kew", "areas": ["Kew"]})
    provider = _run(state, [("search_properties", args)], ["search_properties"])
    (_, params), = provider.calls
    assert params["area"] == "Kew" and params["areas"] == ["Kew"]
    assert params.get("max_budget") == 1500


# ── R2-15: retractions ─────────────────────────────────────────────────────────────
_WITH_LIMITS = {"area": "Camden", "areas": ["Camden"], "max_budget": 2600,
                "commute_destination": "UCL", "destination": "UCL", "max_travel_time": 30,
                "no_commute": False}


@pytest.mark.parametrize("message", ["Never mind about commute.", "Forget the commute.",
                                     "不考虑通勤了。"])
def test_r2_15_commute_retraction_clears_the_commute_filter(message):
    updated = _apply_explicit_criteria_updates(dict(_WITH_LIMITS), message)
    assert updated.get("no_commute") is True and not updated.get("max_travel_time")


@pytest.mark.parametrize("message", ["Cancel the budget limit.", "取消预算限制。",
                                     "Never mind the budget, no maximum."])
def test_r2_15_budget_retraction_clears_the_ceiling(message):
    updated = _apply_explicit_criteria_updates(dict(_WITH_LIMITS), message)
    assert updated.get("max_budget") is None
    assert updated.get("max_travel_time") == 30


def test_r2_15_no_maximum_commute_is_not_a_budget_retraction():
    updated = _apply_explicit_criteria_updates(
        dict(_WITH_LIMITS), "Keep the budget, no maximum commute please")
    assert updated.get("max_budget") == 2600


# ── R2-16: furnished as a requirement ───────────────────────────────────────────────
@pytest.mark.parametrize("message", ["Furnished only please - I don't want anything unfurnished.",
                                     "It must be furnished."])
def test_r2_16_furnished_only_is_a_required_feature(message):
    updated = _apply_explicit_criteria_updates(
        dict(_BASE, soft_preferences=["furnished"]), message)
    assert "furnished" in updated["property_features"]
    assert not any("furnish" in v for v in updated.get("soft_preferences") or [])


@pytest.mark.parametrize("message", ["Is it furnished?", "I want an unfurnished flat"])
def test_r2_16_questions_and_unfurnished_are_not_a_furnished_requirement(message):
    updated = _apply_explicit_criteria_updates(dict(_BASE), message)
    assert "furnished" not in (updated.get("property_features") or [])


def test_r2_16_furnishing_needs_structured_evidence():
    assert _property_feature_evidence({"Detailed_Amenities": ["Gym", "Concierge", "Balcony"]},
                                      "furnished") == "unverified"
    assert _property_feature_evidence({"Detailed_Amenities": ["Furnished", "Gym"]},
                                      "furnished") == "verified"
    assert _property_feature_evidence({"Detailed_Amenities": "Unfurnished"},
                                      "furnished") == "absent"
    assert _property_feature_evidence({"Detailed_Amenities": "Furnished or unfurnished"},
                                      "furnished") == "unverified"


def test_r2_16_an_unverified_furnished_listing_is_not_a_verified_match():
    from core.candidate_validation import validate_candidates
    row = {"address": "Eden Grove, London, N7", "price": 1950, "area": "Holloway",
           "verified_features": [], "unverified_features": ["furnished"], "absent_features": []}
    ledger = validate_candidates([row], {"area": "Holloway", "areas": ["Holloway"],
                                         "max_budget": 2000, "property_features": ["furnished"]},
                                 [])
    assert ledger["statuses"][0]["status"] != "eligible"


# ── R2-17: 'pool' inside 'Liverpool' ─────────────────────────────────────────────────
def test_r2_17_amenity_keywords_match_whole_words_only():
    prefs = extract_preferences_from_message(
        "Compare the first two on commute to Liverpool Street and total monthly cost", {})
    assert not any("pool" in str(v).lower() for v in prefs.get("soft_preferences") or [])
    prefs = extract_preferences_from_message("I'd like a building with a pool", {})
    assert any("pool" in str(v).lower() for v in
               (prefs.get("soft_preferences") or []) + (prefs.get("required_amenities") or []))
