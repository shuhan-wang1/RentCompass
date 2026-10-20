"""The no-results template (overnight round r01: R1-13).

* Any search_properties no_results in the turn replaced the whole answer with "I couldn't find
  listings … Please check the area spelling …" — even when the user had asked something else
  ("What exactly have you kept about me …").
* The text told the user to check the area spelling when the cause was a budget, a room type
  or an unresolved commute destination.
"""
from __future__ import annotations

from core import agent_loop
from core.agent_loop import build_fc_nodes
from core.tools.search_properties import _no_results_message
from tests.test_agent_contract_guards import Chat, Provider, _state

CRITERIA = {"max_budget": 2000, "bedrooms": 1, "room_type": "whole",
            "property_features": ["pets allowed"], "max_travel_time": 30,
            "commute_destination": "King's Cross", "no_commute": False}


def test_r1_13_message_names_the_applied_filters():
    text = _no_results_message("Islington", False, criteria=CRITERIA)
    for part in ("up to £2,000/month", "1 bedroom", "Whole property", "pets allowed",
                 "within 30 min of King's Cross"):
        assert part in text, part
    assert "spelling" not in text


def test_r1_13_spelling_hint_only_when_the_area_did_not_resolve():
    assert "spelling" in _no_results_message("Islingtn", False, criteria={}, area_unresolved=True)
    zh = _no_results_message("伊斯灵顿", True, criteria={"max_budget": 2000})
    assert "拼写" not in zh and "£2,000" in zh


def _rendered(message, final_response):
    nodes = build_fc_nodes(Provider([]), agent_llm=Chat())
    state = _state(message)
    state["final_response"] = final_response
    state["tool_artifacts"] = [agent_loop._artifact(1, "search_properties", {
        "status": "no_results", "recommendations": [],
        "message": _no_results_message("Hackney", False, criteria={"max_budget": 1500}),
        "search_criteria": {"area": "Hackney", "max_budget": 1500}}, success=True)]
    return nodes["format_output_fc"](state)["final_response"]


def test_r1_13_unrelated_question_keeps_its_answer():
    answer = ("I keep your search criteria for this conversation: a studio in Hackney up to "
              "£1,500. I don't store your phone number or health details.")
    out = _rendered("Do you store anything about me? What exactly have you kept about me so far?",
                    answer)
    assert out.startswith("I keep your search criteria")
    assert "couldn't find listings" in out


def test_r1_13_a_search_turn_still_reports_the_empty_search():
    out = _rendered("Lower the budget to £1,500.", "Here are some flats.")
    assert out.startswith("I couldn't find listings near Hackney")
    assert "Here are some flats" not in out
