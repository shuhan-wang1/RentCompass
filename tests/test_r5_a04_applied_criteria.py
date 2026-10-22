"""Stress A-04 re-verified on b8d7632: what the recap claims vs what the criteria hold.

Stress run of 7483f77, inst-A long1 (conversation b7b196fd…). The recaps said "Areas: Camden
and Angel", "Lift: no longer required" and "balcony is a nice-to-have" while
``view_state.criteria`` held ``areas=[Camden]``, ``property_features=[lift]`` and no balcony.
On b8d7632 the lift retraction and the added area already apply (asserted below as guards).
What still reproduced:

* the nice-to-have balcony reached only ``user_preferences``, never the structured criteria
  or the search;
* the model's ``accumulated_preferences`` restated hard slots ("1 bedroom", "whole flat",
  "Islington", "commute to King's Cross under 30 minutes"), the echo folded them into the
  criteria, and view_state still listed "Islington" after the switch to Camden.

Messages and arguments are the recorded turns (rids 158874d2…, 2b04906d…, 0095725f…, 9eddd79e…).
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from core import preference_grounding
from core.agent_loop import build_fc_nodes
from core.langgraph_agent import _apply_explicit_criteria_updates
from tests.test_agent_contract_guards import _state, _tc
from tests.test_qa_null_injection import _ValidatingProvider

TURNS = [
    "Hi! I want to rent in Islington. Budget is £1,800 a month, 1 bedroom, whole flat please.",
    "Actually, bump the budget to £2,100 a month.",
    "I work at King's Cross, so I'd like a commute of under 30 minutes.",
    "A lift in the building is a must for me.",
    "Nice-to-have: a balcony, but it's not essential.",
]
# The model's search arguments on turn 2 (rid 158874d2…), minus the harness-injected keys.
TURN2_MODEL_ARGS = {
    "user_query": "1 bedroom whole flat in Islington, budget £2100 a month, commute to King's Cross "
                  "under 30 minutes",
    "area": "Islington", "areas": ["Islington"], "commute_destination": "King's Cross",
    "no_commute": False, "bedrooms": 1, "bedrooms_minimum": False, "max_budget": 2100,
    "max_commute_time": 30, "limit": 10, "property_features": [],
    "accumulated_preferences": ["1 bedroom", "whole flat", "Islington",
                                "commute to King's Cross under 30 minutes"],
    "budget_period": "month", "room_type": "whole", "confirmed": True,
}


def _criteria_after(turns, start=None):
    acc = dict(start or {"destination": None, "max_budget": None, "max_travel_time": None,
                         "property_features": [], "soft_preferences": [], "amenities_of_interest": []})
    for message in turns:
        acc = _apply_explicit_criteria_updates(acc, message)
    return acc


def _search_params(acc, message, args):
    provider = _ValidatingProvider(["search_properties"])
    state = _state(message)
    state["extracted_context"]["history"] = [{"user": m, "assistant": "..."} for m in TURNS]
    state["accumulated_search_criteria"] = acc
    state["messages"] = [AIMessage(content="", tool_calls=[
        _tc("search_properties", dict(args, current_message=message), "c0")])]
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    assert provider.rejected == []
    (_name, params), = provider.calls
    return params


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_STRICT", "1")
    monkeypatch.setattr(preference_grounding, "saved_fact_texts", lambda user_id: [])


def test_a04_hard_slot_restatements_are_not_soft_preferences():
    acc = _criteria_after(TURNS[:3])
    params = _search_params(acc, TURNS[2], TURN2_MODEL_ARGS)
    assert "accumulated_preferences" not in params
    assert params["area"] == "Islington" and params["max_commute_time"] == 30


def test_a04_the_stated_nice_to_have_is_in_the_criteria_and_the_search():
    acc = _criteria_after(TURNS)
    assert acc["soft_preferences"] == ["Would like balcony"]
    assert acc["property_features"] == ["lift"]          # the hard need is unchanged
    params = _search_params(acc, TURNS[4], dict(TURN2_MODEL_ARGS, property_features=["lift"]))
    assert params["accumulated_preferences"] == ["Would like balcony"]


@pytest.mark.parametrize("message", [
    "Actually I don't need a balcony after all.",
    "I changed my mind about the balcony, not needed anymore.",
])
def test_a04_a_withdrawn_nice_to_have_leaves_the_criteria(message):
    acc = _criteria_after(TURNS + [message])
    assert "Would like balcony" not in acc["soft_preferences"]


def test_a04_the_recorded_retractions_apply_on_b8d7632():
    # Guards for the parts that no longer reproduce: the lift retraction and the added area.
    acc = _criteria_after(["Switch to Camden.", "I changed my mind about the lift, not needed anymore.",
                           "Add Angel as a second area to search."], start=_criteria_after(TURNS))
    assert acc["property_features"] == []
    assert acc["areas"] == ["Camden", "Angel"]
    assert acc["soft_preferences"] == ["Would like balcony"]


def test_a04_dont_need_is_not_a_requirement():
    from core.langgraph_agent import extract_preferences_from_message
    prefs = extract_preferences_from_message(
        "Actually I don't need a balcony after all.",
        {"hard_preferences": [], "soft_preferences": [], "required_amenities": []})
    assert prefs == {"hard_preferences": [], "soft_preferences": [], "required_amenities": []}
