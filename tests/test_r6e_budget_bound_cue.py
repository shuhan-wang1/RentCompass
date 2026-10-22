"""The budget comes from the user's own bound, not from a price they quote (r6e).

r21 acceptance, rid 63fb6f8a0c614f16b6ce2592e2d41ec2 (same on main): "What would a 6-week
deposit and the first month's rent come to for a £1,950/month flat in Hackney? Find me one
under £2,000 too." The parser stored max_budget 1950 (the example flat's rent: the word
"rent" gave £1,950 the rent role, which outranks "under £2,000"), so the model's
search_properties call at 1950 passed the hard-constraint grounding. A place's quoted price
("a £1,950/month flat") is no ceiling when the message also states one.
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from core.agent_loop import build_fc_nodes
from core.langgraph_agent import _apply_explicit_criteria_updates
from core.tools.search_properties import _extract_budget
from tests.test_agent_contract_guards import _state, _tc
from tests.test_qa_null_injection import _ValidatingProvider

R21 = ("What would a 6-week deposit and the first month's rent come to for a £1,950/month flat "
       "in Hackney? Find me one under £2,000 too.")
# The recorded search_properties arguments of rid 63fb6f8a (tool-dispatches.jsonl).
R21_ARGS = {"user_query": "Hackney flat under £2,000 per month", "area": "Hackney",
            "areas": ["Hackney"], "max_budget": 1950, "limit": 10, "current_message": R21,
            "budget_period": "month", "confirmed": True, "criteria_gate_shown": False,
            "reply_language": "en", "bedrooms_minimum": False, "property_features": []}


@pytest.mark.parametrize("message, budget", [
    (R21, 2000),
    ("I like a £1,800 flat in Camden, but my budget is £2,200.", 2200),
    ("Is £2,100 a month reasonable? I can pay up to £2,300.", 2300),
    ("I can pay up to £2,300", 2300),
    # Unchanged: a price point alone is the search's price; a stretch is no ceiling.
    ("Find me a £1,800 flat in Camden", 1800),
    ("Find me a 1-bed in Hackney under £2,000.", 2000),
    ("My budget is £1,900, but I could stretch to £2,100.", 1900),
    ("Is £2,100 a month reasonable?", None),
])
def test_the_budget_comes_from_the_bound_cue(message, budget):
    assert _extract_budget(message)[0] == budget
    assert _apply_explicit_criteria_updates({}, message).get("max_budget") == budget


def test_fc_search_args_carry_the_users_ceiling():
    state = _state(R21)
    state["accumulated_search_criteria"] = _apply_explicit_criteria_updates({}, R21)
    assert state["accumulated_search_criteria"]["max_budget"] == 2000
    provider = _ValidatingProvider(["search_properties"])
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_properties", dict(R21_ARGS), "c0")])]
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    (_name, params), = provider.calls
    assert params["max_budget"] == 2000 and params["area"] == "Hackney"


PRIOR = "Find me a 1-bed in Hackney under £2,000."


@pytest.mark.parametrize("message", [
    "What's the rent on a £1,950 flat with a 6-week deposit?",
    "How much is the deposit on a £2,000 flat?",
    "What would the first month's rent and deposit be on a £1,800/month flat?",
])
def test_a_price_quoted_for_arithmetic_neither_sets_nor_changes_the_budget(message):
    assert _extract_budget(message) == (None, None)
    assert "max_budget" not in _apply_explicit_criteria_updates({}, message)
    before = _apply_explicit_criteria_updates({}, PRIOR)
    assert _apply_explicit_criteria_updates(dict(before), message)["max_budget"] == 2000


@pytest.mark.parametrize("message, budget", [
    ("Find me a £1,800 flat in Camden", 1800),                          # a search's price point
    ("What's the rent on a £1,950 flat? My budget is £2,100.", 2100),  # an explicit budget cue
])
def test_a_quoted_price_with_a_search_or_budget_cue_still_counts(message, budget):
    assert _apply_explicit_criteria_updates({}, message)["max_budget"] == budget
