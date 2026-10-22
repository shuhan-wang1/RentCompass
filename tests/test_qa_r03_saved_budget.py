"""R3-03: a search that ran without a budget names the saved budget it did not use.

r04 rid df9fecea (conv 0428398f): memory held "[FACT] User's maximum rent budget is £1,800
per month"; "Find me a 1-bed in Camden, I don't want to repeat myself" ran search_properties
with no max_budget and listed £2,000-£3,900 homes without a word about the saved budget. The
saved budget is mentioned and offered, NOT applied automatically (an owner decision).
"""
from __future__ import annotations

from core import agent_loop
from core.agent_loop import (acknowledge_remembered_budget, build_fc_nodes,
                             searched_without_budget)

MEMORY = ("What I remember about this user:\n"
          "[FACT] User's maximum rent budget is £1,800 per month, and they want to live in a quiet area.")
MESSAGE = "Find me a 1-bed in Camden, I don't want to repeat myself"
# the tool's own echo from the r04 turn (search_criteria.max_budget null)
CRITERIA = {"area": "Camden", "areas": ["Camden"], "commute_destination": None, "no_commute": False,
            "bedrooms": 1, "bedrooms_minimum": False, "budget_period": "month", "room_type": "whole",
            "destination": "Camden", "max_budget": None, "max_travel_time": None,
            "soft_preferences": ["quiet area"]}
ROWS = [{"address": "Bayham Street, London, NW1", "price": 2700, "area": "Camden"},
        {"address": "Neal Street, London, WC2H", "price": 3900, "area": "Camden"}]
ANSWER = ("Here are the 1-bed whole-property listings currently available in Camden: Bayham "
          "Street, NW1 at £2700/month and Neal Street, WC2H at £3900/month.")


def _artifacts(max_budget=None):
    criteria = dict(CRITERIA, max_budget=max_budget)
    return [agent_loop._artifact(1, "search_properties", {
        "status": "found", "recommendations": ROWS, "search_criteria": criteria}, success=True)]


def test_budgetless_search_is_detected():
    assert searched_without_budget(_artifacts()) is True
    assert searched_without_budget(_artifacts(1800)) is False
    assert searched_without_budget([]) is False


def test_answer_names_the_saved_budget_says_it_was_not_applied_and_offers_it():
    out = acknowledge_remembered_budget(ANSWER, MEMORY, None, "en",
                                        searched_without_budget=True)
    assert out.startswith(ANSWER)
    assert "£1,800 per month" in out
    assert "not applied" in out and "apply it" in out
    # idempotent
    assert acknowledge_remembered_budget(out, MEMORY, None, "en", searched_without_budget=True) == out


def test_chinese_note():
    out = acknowledge_remembered_budget("这是 Camden 的房源。", MEMORY, None, "zh",
                                        searched_without_budget=True)
    assert "£1,800" in out and "没有使用" in out


def test_no_note_without_a_saved_budget_or_when_a_budget_was_used():
    assert acknowledge_remembered_budget(ANSWER, "[FACT] I have a cat", None, "en",
                                         searched_without_budget=True) == ANSWER
    assert acknowledge_remembered_budget(ANSWER, MEMORY, 1800, "en",
                                         searched_without_budget=True) == ANSWER
    assert acknowledge_remembered_budget(ANSWER, MEMORY, None, "en") == ANSWER


def test_format_node_adds_the_note_after_the_critic():
    from tests.test_agent_contract_guards import Chat, Provider, _state
    nodes = build_fc_nodes(Provider([]), agent_llm=Chat())
    state = _state(MESSAGE)
    state["final_response"] = ANSWER
    state["memory_context"] = MEMORY
    state["tool_artifacts"] = _artifacts()
    out = nodes["format_output_fc"](state)["final_response"]
    assert "Bayham Street" in out
    assert "You saved a budget".lower() in out.lower() and "£1,800 per month" in out
    assert out.count("£1,800 per month earlier") == 1


# ── R4-11: the no-budget claim does not stand beside the saved-budget note ─────────────
def test_r4_11_no_budget_claim_is_withdrawn_when_the_note_fires():
    answer = ("Here are 1-beds in Camden. Because you didn't give a budget, bedroom count or "
              "workplace, the search used Moorgate as the commute destination. Bayham Street, NW1 "
              "is £2700/month.")
    out = acknowledge_remembered_budget(answer, MEMORY, None, "en", searched_without_budget=True)
    assert "didn't give a budget" not in out
    assert "Bayham Street" in out and "Here are 1-beds in Camden." in out
    assert "£1,800 per month" in out and "not applied" in out
