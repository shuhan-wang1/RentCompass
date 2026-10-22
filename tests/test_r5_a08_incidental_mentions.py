"""Stress A-08, criteria half: a background turn rewrote the live search criteria.

Stress run of 7483f77, inst-A alice turn 12 (rid 94982f08d4c84dfdb0e6d50cc0f4aeb8). After
"budget at most £1,900 … 1 bedroom whole flat in Hackney … I work at Canary Wharf", the user
wrote the message below. view_state then showed area=Peckham (a friend's home),
max_budget=2100 (a conditional stretch) and commute_destination="Malet Street, Bloomsbury,
London WC1E 7HX" (Birkbeck, where the user studies part-time, mentioned as background in a
"still nothing to search" turn; the workplace destination was never withdrawn). Reproduced
offline on b8d7632 with ``_apply_explicit_criteria_updates``.

The memory layer applies the same rule to memory rows (``rag.agent_memory.user_stated_changes``,
fix/r5-memory); this is the parser side.
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from core import preference_grounding
from core.agent_loop import build_fc_nodes
from core.constraint_updates import without_third_party_clauses
from core.langgraph_agent import _apply_explicit_criteria_updates
from core.tools.search_properties import _extract_area, _extract_budget
from tests.test_agent_contract_guards import _state, _tc
from tests.test_qa_null_injection import _ValidatingProvider

A08_MESSAGE = (
    "More background, still nothing to search: I prefer the Overground to buses, I need fast "
    "broadband for streaming, I'd like a dishwasher, wooden floors are better for my dust allergy, "
    "my friend lives in Peckham, I'm moving in March, my budget could stretch to £2,100 for the "
    "right place, and I study part-time at Birkbeck.")
_CW = "Canary Wharf, London E14 5AB"
PRIOR = {"area": "Hackney", "areas": ["Hackney"], "max_budget": 1900, "budget_period": "month",
         "bedrooms": 1, "room_type": "whole", "commute_destination": _CW, "destination": _CW,
         "property_features": [], "soft_preferences": []}


def test_a08_the_background_turn_keeps_area_ceiling_and_destination():
    after = _apply_explicit_criteria_updates(dict(PRIOR), A08_MESSAGE)
    assert after["area"] == "Hackney" and after["areas"] == ["Hackney"]
    assert after["max_budget"] == 1900
    assert after["commute_destination"] == _CW and after["destination"] == _CW
    # The stretch is kept as the user's own soft note; the stated move-in month still applies.
    assert after["soft_preferences"] == ["my budget could stretch to £2,100 for the right place"]
    assert str(after.get("move_in_date") or "").endswith("-03-01")


def test_a08_the_executor_does_not_reopen_it(monkeypatch):
    """The same message reaching a search (the model sent Peckham and £2,100) searches the
    user's own criteria."""
    monkeypatch.setenv("DEEPSEEK_STRICT", "1")
    monkeypatch.setattr(preference_grounding, "saved_fact_texts", lambda user_id: [])
    provider = _ValidatingProvider(["search_properties"])
    state = _state(A08_MESSAGE)
    state["accumulated_search_criteria"] = _apply_explicit_criteria_updates(dict(PRIOR), A08_MESSAGE)
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_properties", {
        "user_query": "flat in Peckham up to £2,100", "area": "Peckham", "max_budget": 2100,
        "commute_destination": "Malet Street, Bloomsbury, London WC1E 7HX",
        "current_message": A08_MESSAGE}, "c0")])]
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    assert provider.rejected == []
    (_name, params), = provider.calls
    assert params["area"] == "Hackney" and params["max_budget"] == 1900
    assert params["commute_destination"] == _CW


@pytest.mark.parametrize("message, budget", [
    ("My budget is £1,900, but I could stretch to £2,100.", 1900),
    ("my budget is £1,800 and could stretch to £2,000 for the right place", 1800),
    ("£2,100 at a push", None),
    ("Could you find flats under £2,000?", 2000),     # a request, not a stretch
    ("Raise the budget to £2,100.", 2100),
])
def test_a08_a_conditional_stretch_is_not_a_ceiling(message, budget):
    amount, _period = _extract_budget(message)
    assert amount == budget


@pytest.mark.parametrize("message, area", [
    ("My friend lives in Peckham.", None),
    ("My sister recommended Peckham.", None),
    ("我朋友住在Peckham", None),
    ("My partner and I want a 2-bed in Peckham.", "Peckham"),       # the user is in the subject
    ("My friend lives in Peckham, find me a flat there.", "Peckham"),  # the user adopts it
    ("Search Peckham, my friend lives there.", "Peckham"),
])
def test_a08_someone_elses_place_is_not_an_area_switch(message, area):
    after = _apply_explicit_criteria_updates(dict(PRIOR), message)
    assert after["area"] == (area or "Hackney")
    if area is None:
        assert _extract_area(message) is None


@pytest.mark.parametrize("message, destination", [
    ("My friend studies at UCL.", _CW),
    ("I study at UCL, find me a studio nearby.", "University College London, Gower Street, London WC1E 6BT"),
    ("Actually I study at UCL now.", "University College London, Gower Street, London WC1E 6BT"),
])
def test_a08_a_mentioned_place_moves_the_destination_only_when_presented_as_one(message, destination):
    after = _apply_explicit_criteria_updates(dict(PRIOR), message)
    assert after["commute_destination"] == destination


def test_a08_a_first_destination_is_still_taken_from_the_users_own_place():
    after = _apply_explicit_criteria_updates({}, "I study part-time at Birkbeck.")
    assert after["commute_destination"] == "Malet Street, Bloomsbury, London WC1E 7HX"
    assert without_third_party_clauses("My partner works at Canary Wharf.").strip(" .") == ""
