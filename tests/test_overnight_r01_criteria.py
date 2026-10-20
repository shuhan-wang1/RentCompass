"""Criteria parsing found in overnight round r01 (R1-03, R1-04, R1-05, R1-06).

R1-03: "Search web for average rent in Hackney 2026 and cite sources" stored
       max_budget=2026 and the reply offered listings "within your £2,026 budget".
R1-04: "I commute to King's Cross, max 30 minutes." stored the destination
       "King's Cross, max 30 minutes"; geocoding failed and four turns returned nothing.
R1-05: "Actually forget the pets thing, I no longer have a cat." left the soft
       preference 'pet friendly' in place and in the next search's arguments.
R1-06: "It must allow pets." was only a soft preference, and "Find a 1-bed in Hackney"
       (no word "flat") left room_type unknown, so a "1 bed Flat share" room and listings
       with no pet evidence were shown under the verified header.
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from core.agent_loop import build_fc_nodes
from core.constraint_updates import pet_requirement_update
from core.langgraph_agent import _apply_explicit_criteria_updates
from core.tools.search_properties import _extract_budget, _extract_room_type, _matches_room_type
from tests.test_agent_contract_guards import _state, _tc
from tests.test_overnight_null_injection import _ValidatingProvider, _strict_null_args


@pytest.fixture(autouse=True)
def _strict(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_STRICT", "1")


def _dispatch(message, acc, model_args=None):
    state = _state(message)
    state["accumulated_search_criteria"] = _apply_explicit_criteria_updates(acc, message)
    provider = _ValidatingProvider(["search_properties"])
    nodes = build_fc_nodes(provider)
    args = _strict_null_args("search_properties", {"user_query": message, **(model_args or {})})
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_properties", args, "c0")])]
    asyncio.run(nodes["execute_tools"](state))
    assert provider.rejected == []
    (_, params), = provider.calls
    return state["accumulated_search_criteria"], params


# ── R1-03 ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "Search web for average rent in Hackney 2026 and cite sources",
    "rent in 2025 vs 2026",
    "2026年哈克尼平均租金",
    "What were average rents in Camden in 2024?",
])
def test_r1_03_a_year_is_never_a_budget(message):
    assert _extract_budget(message) == (None, None)
    acc = _apply_explicit_criteria_updates({"area": "Hackney", "max_budget": 1800}, message)
    assert acc.get("max_budget") == 1800


@pytest.mark.parametrize("message, amount", [
    ("my budget is 2000", 2000),
    ("under 2000", 2000),
    ("2000 pcm", 2000),
    ("I can pay 2000 a month", 2000),
    ("£2026 max", 2026),
    ("Budget 2026", 2026),
    ("预算2026", 2026),
    ("Moving in 2027, budget is 2000", 2000),
    ("In 2026 I want a flat under £1,950", 1950),
    ("2026年1月入住，预算1800", 1800),
])
def test_r1_03_money_next_to_a_year_shaped_number_still_counts(message, amount):
    assert _extract_budget(message)[0] == amount


# ── R1-04 ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message, destination, minutes", [
    ("I commute to King's Cross, max 30 minutes.", "King's Cross", 30),
    ("I commute to King's Cross, 30 minutes max.", "King's Cross", 30),
    ("I commute to King's Cross (max 30 minutes)", "King's Cross", 30),
    ("My office is in Soho, no more than 35 minutes", "Soho", 35),
    ("I work in Shoreditch - 30 min max", "Shoreditch", 30),
    ("I commute to Liverpool Street, up to 45 minutes door to door", "Liverpool Street", 45),
])
def test_r1_04_time_limit_is_not_part_of_the_destination(message, destination, minutes):
    acc = _apply_explicit_criteria_updates(
        {"area": "Islington", "areas": ["Islington"], "no_commute": True}, message)
    assert acc["commute_destination"] == destination
    assert acc["destination"] == destination
    assert acc["max_travel_time"] == minutes
    assert acc["no_commute"] is False


def test_r1_04_control_known_destination_unchanged():
    acc = _apply_explicit_criteria_updates({}, "I work at Canary Wharf, max 40 minutes")
    assert acc["commute_destination"].startswith("Canary Wharf")
    assert acc["max_travel_time"] == 40


# ── R1-05 ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "Actually forget the pets thing, I no longer have a cat.",
    "Never mind the pets requirement",
    "I don't have a cat any more.",
    "我不再养猫了",
])
def test_r1_05_pet_withdrawal_removes_hard_and_soft(message):
    acc = {"area": "Islington", "areas": ["Islington"],
           "property_features": ["pets allowed", "cats allowed", "lift"],
           "soft_preferences": ["pet friendly", "pet-friendly (cat)", "quiet street"]}
    assert pet_requirement_update(message) is False
    result = _apply_explicit_criteria_updates(acc, message)
    assert result["property_features"] == ["lift"]
    assert result["soft_preferences"] == ["quiet street"]


def test_r1_05_next_search_does_not_carry_the_withdrawn_preference():
    acc = {"area": "Islington", "areas": ["Islington"], "max_budget": 2000,
           "soft_preferences": ["pet friendly"], "property_features": []}
    acc = _apply_explicit_criteria_updates(
        acc, "Actually forget the pets thing, I no longer have a cat.")
    assert acc["soft_preferences"] == []
    _, params = _dispatch("Please search now.", acc,
                          {"area": "Islington", "accumulated_preferences": ["pet friendly"]})
    assert "pet friendly" not in (params.get("accumulated_preferences") or [])
    assert "pets allowed" not in (params.get("property_features") or [])


# ── R1-06 ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "It must allow pets.",
    "Find a 1-bed in Hackney that allows pets",
    "I'm a cat owner on £1,800/month working at Canary Wharf. Find a 1-bed in Hackney that allows pets.",
    "I need a pet-friendly flat",
    "必须允许养宠物",
])
def test_r1_06a_stated_pet_requirement_is_a_required_feature(message):
    assert pet_requirement_update(message) is True
    acc = _apply_explicit_criteria_updates({"soft_preferences": ["pet-friendly (cat)"]}, message)
    assert "pets allowed" in acc["property_features"] or "cats allowed" in acc["property_features"]
    assert acc.get("soft_preferences") == []        # the hard copy replaces the soft one


@pytest.mark.parametrize("message", [
    "Does it allow pets?",
    "No pets in the building please",
    "Pet-friendly would be nice",
    "I have a cat",
    "What if it doesn't allow pets?",
])
def test_r1_06a_questions_and_wishes_are_not_requirements(message):
    assert pet_requirement_update(message) is None


def test_r1_06a_pet_requirement_reaches_the_tool_as_a_feature():
    acc, params = _dispatch("Find a 1-bed in Hackney that allows pets", {},
                            {"area": "Hackney", "bedrooms": 1})
    assert "pets allowed" in params["property_features"]
    assert params["room_type"] == "whole"
    assert acc["room_type"] == "whole"


@pytest.mark.parametrize("message, expected", [
    ("Find a 1-bed in Hackney that allows pets", "whole"),
    ("Find me a 1-bed that fits what you know about me, in Hackney", "whole"),
    ("Looking for a 2-bed in Hackney", "whole"),
    ("2br in Camden", "whole"),
    ("伦敦肖迪奇附近的一居室", "whole"),
    ("a room in a 3-bed flat", None),
    ("rooms in a 4 bed house", None),
    ("1 bed flat share", "shared"),
    ("1 bed in a shared house", "shared"),
    ("合租一居室", "shared"),
    ("a double room in Hackney", None),
])
def test_r1_06b_bare_n_bed_is_a_whole_home(message, expected):
    assert _extract_room_type(message) == expected


def test_r1_06b_flat_share_does_not_match_whole():
    flat_share = {"Room_Type_Category": "1 bed Flat share", "Type": "1 bed Flat share",
                  "Address": "Flat Share - ALL Bills & Wifi Included - Bethnal Green"}
    assert not _matches_room_type(flat_share, "whole")
