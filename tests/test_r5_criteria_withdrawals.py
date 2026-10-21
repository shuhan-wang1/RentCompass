"""A topic the user withdraws is cleared in the structured criteria (round 5: R4-14).

r07 inst-A long1 (A-F5) turn 28: "Forget everything we have said in this conversation about
location." The reply said "Location: none set", but ``view_state.criteria`` kept
``area=Camden`` and ``destination=commute_destination='Canary Wharf, London E14 5AB'`` for the
rest of the conversation. r06 acceptance NEW-4 (bf614de0…): "Never mind about commute." set
``no_commute`` but left ``destination``/``commute_destination = King's Cross`` in view_state;
r07 long1 turn 7 "Actually forget the commute, I will work from home." did the same.

The criteria below are the recorded view_state criteria of those conversations.
"""
from __future__ import annotations

import asyncio
import copy

import pytest
from langchain_core.messages import AIMessage

from core.agent_loop import build_fc_nodes
from core.constraint_updates import withdrawn_topics
from core.criteria_contract import reconcile_search_criteria
from core.langgraph_agent import _apply_explicit_criteria_updates
from tests.test_agent_contract_guards import _state, _tc
from tests.test_overnight_null_injection import _ValidatingProvider

# r07 long1 turn 27 (rid f1632a5b…): view_state.criteria just before the withdrawal.
_CW = "Canary Wharf, London E14 5AB"
R07_CRITERIA = {
    "amenities_of_interest": [], "area": "Camden", "areas": ["Camden"], "bedrooms": 1,
    "bedrooms_minimum": False, "budget_period": "month", "commute_destination": _CW,
    "constraint_sources": {
        "area": {"source": "user_message", "text": "预算改成 £2,000，区域改成 Camden。", "value": "Camden"},
        "areas": {"source": "user_message", "text": "预算改成 £2,000，区域改成 Camden。", "value": ["Camden"]},
        "bedrooms": {"source": "user_message", "text": "Make it 1 bed rather than 2.", "value": 1},
        "commute_destination": {"source": "user_message",
                                "text": "Set my commute destination to Canary Wharf, max 40 minutes.",
                                "value": _CW},
        "destination": {"source": "user_message",
                        "text": "Set my commute destination to Canary Wharf, max 40 minutes.", "value": _CW},
        "max_budget": {"source": "user_message", "text": "Reduce budget by £300.", "value": 2200},
        "room_type": {"source": "user_message", "text": "Find a 2-bed flat in Hackney under £2,800/month.",
                      "value": "whole"},
    },
    "destination": _CW, "max_budget": 2200, "max_travel_time": None, "no_commute": True,
    "property_features": [], "room_type": "whole", "soft_preferences": ["garden"],
}
FORGET_LOCATION = "Forget everything we have said in this conversation about location."


def test_r4_14_forget_everything_about_location_clears_area_and_destination():
    after = _apply_explicit_criteria_updates(copy.deepcopy(R07_CRITERIA), FORGET_LOCATION)
    assert after["area"] is None and after["areas"] == []
    assert after["commute_destination"] is None and after["destination"] is None
    # Everything else the user did not withdraw stays.
    assert (after["max_budget"], after["bedrooms"], after["room_type"]) == (2200, 1, "whole")
    for field in ("area", "areas", "commute_destination", "destination"):
        assert after["constraint_sources"][field]["source"] == "user_message"
        assert after["constraint_sources"][field]["value"] in (None, [])
    # A later search echo cannot fill the withdrawn slots back in.
    echo = {"area": "Camden", "areas": ["Camden"], "commute_destination": _CW, "max_budget": 2200}
    reconciled = reconcile_search_criteria(after, echo)
    assert reconciled["area"] is None and reconciled["commute_destination"] is None
    assert reconciled["destination"] is None


def _search_params(acc, message, model_args):
    provider = _ValidatingProvider(["search_properties"])
    state = _state(message)
    state["accumulated_search_criteria"] = _apply_explicit_criteria_updates(acc, message)
    state["messages"] = [AIMessage(content="", tool_calls=[
        _tc("search_properties", dict(model_args, current_message=message), "c0")])]
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    assert provider.rejected == []
    (_name, params), = provider.calls
    return params


@pytest.fixture(autouse=True)
def _strict(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_STRICT", "1")


def test_r4_14_the_next_search_does_not_bring_the_withdrawn_destination_back():
    acc = copy.deepcopy(R07_CRITERIA)
    acc["no_commute"] = False      # the location withdrawal alone, with a commute still on
    acc["max_travel_time"] = 40
    acc = _apply_explicit_criteria_updates(acc, FORGET_LOCATION)
    # The model still remembers Canary Wharf (and Camden) from the conversation.
    params = _search_params(acc, "Search for Stratford with the same budget.", {
        "user_query": "flats in Stratford", "area": "Stratford", "commute_destination": _CW,
        "max_budget": 2200})
    assert params["area"] == "Stratford" and params["areas"] == ["Stratford"]
    assert "commute_destination" not in params
    assert params["max_budget"] == 2200


def test_r4_14_the_withdrawn_area_does_not_return_as_a_legacy_location():
    acc = _apply_explicit_criteria_updates(copy.deepcopy(R07_CRITERIA), FORGET_LOCATION)
    params = _search_params(acc, "Search again with the same budget.", {
        "user_query": "flats", "location": "Camden", "area": "Camden", "max_budget": 2200})
    assert "area" not in params and "location" not in params and not params.get("areas")


def test_r4_14_a_destination_the_message_names_again_is_kept():
    acc = _apply_explicit_criteria_updates(copy.deepcopy(R07_CRITERIA), FORGET_LOCATION)
    params = _search_params(acc, "Search Stratford, I still commute to Canary Wharf.", {
        "user_query": "flats in Stratford", "area": "Stratford", "commute_destination": "Canary Wharf"})
    assert str(params.get("commute_destination")).startswith("Canary Wharf")


@pytest.mark.parametrize("message", [
    "Never mind about commute.",                              # r06 NEW-4 (bf614de0…)
    "Actually forget the commute, I will work from home.",    # r07 long1 turn 7
    "Forget everything about the commute.",
    "不考虑通勤了",
])
def test_r4_14_withdrawing_the_commute_clears_the_destination(message):
    acc = {"area": "Islington", "areas": ["Islington"], "commute_destination": "King's Cross",
           "destination": "King's Cross", "max_travel_time": 30, "no_commute": False,
           "max_budget": 3200}
    after = _apply_explicit_criteria_updates(acc, message)
    assert after["no_commute"] is True and after["max_travel_time"] is None
    assert after["commute_destination"] is None and after["destination"] is None
    assert after["area"] == "Islington"


@pytest.mark.parametrize("message", ["Remove the commute limit.", "取消通勤时间",
                                     "Forget about the commute time."])
def test_r4_14_lifting_only_the_limit_keeps_the_destination(message):
    acc = {"area": "Islington", "commute_destination": "King's Cross", "destination": "King's Cross",
           "max_travel_time": 30, "no_commute": False}
    after = _apply_explicit_criteria_updates(acc, message)
    assert after["max_travel_time"] is None
    assert after["commute_destination"] == "King's Cross"


@pytest.mark.parametrize("message, area", [
    ("Disregard the area, search Stratford.", "Stratford"),
    ("Forget the location, just search anywhere in London.", "London"),
    ("Never mind the area.", None),
])
def test_r4_14_a_place_named_after_the_withdrawal_applies(message, area):
    acc = {"area": "Camden", "areas": ["Camden"], "commute_destination": _CW, "destination": _CW}
    after = _apply_explicit_criteria_updates(acc, message)
    assert after["area"] == area


@pytest.mark.parametrize("message", [
    "Don't forget the location.",
    "Should I forget about the location?",
    "What if we ignore the area?",
    "Ignore my friend's area suggestion.",
    "Keep the location as it is.",
])
def test_r4_14_no_withdrawal_no_change(message):
    acc = {"area": "Camden", "areas": ["Camden"], "commute_destination": _CW, "destination": _CW}
    assert withdrawn_topics(message) == set()
    after = _apply_explicit_criteria_updates(acc, message)
    assert (after["area"], after["commute_destination"]) == ("Camden", _CW)


def test_r4_14_the_topic_word_never_becomes_an_area():
    # R2-14's guard: a bare word is not a place.
    for message in (FORGET_LOCATION, "forget everything about location", "Never mind the area."):
        after = _apply_explicit_criteria_updates({}, message)
        assert not after.get("area") and not after.get("areas")
