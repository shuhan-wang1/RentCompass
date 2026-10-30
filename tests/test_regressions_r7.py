"""Regression tests (V-xx ids in each test name)."""
from __future__ import annotations

import asyncio
import importlib
import uuid

import pytest


def _state_node(accumulated: dict, message: str, history=None) -> dict:
    from core.langgraph_agent import _make_extract_preferences_node
    state = {"user_preferences": {}, "accumulated_search_criteria": dict(accumulated),
             "extracted_context": {"current_message": message,
                                   "history": history or [{"user": "Find a flat", "assistant": "ok"}]}}
    update = _make_extract_preferences_node()(state)
    return update.get("accumulated_search_criteria", state["accumulated_search_criteria"])


_CAMDEN = {"area": "Camden", "areas": ["Camden"], "bedrooms": 1, "max_budget": 2200,
           "room_type": "whole", "max_travel_time": 30, "commute_destination": "King's Cross"}


# V-01 ── a place followed by "here" is no name
@pytest.mark.parametrize("message", [
    "Camden here", "Hi, Camden here", "Camden here, looking for a flat", "This is Hackney here",
    "This is Hackney", "King's Cross here", "Camden Town here", "Bow here", "Oxford here",
])
def test_v_01_a_curated_place_is_not_a_name(message):
    from rag.agent_memory import redact_sensitive
    assert redact_sensitive(message) == (message, [])


@pytest.mark.parametrize("message", [
    "Sarah here", "Hi, Sarah here, looking for a flat", "This is Sarah speaking",
    "I'm Chelsea, looking for a flat", "This is Chelsea speaking",
])
def test_v_01_a_person_stays_withheld(message):
    from rag.agent_memory import redact_sensitive
    text, found = redact_sensitive(message)
    assert "[personal detail not stored]" in text and found == ["identity"]


# V-02 ── a reset naming a place clears the rest and keeps the place
@pytest.mark.parametrize("message", [
    "Can we start from scratch with Hackney?", "Start from scratch with Hackney",
    "start over in Hackney", "start over from Hackney", "Start again with Hackney",
    "Could we restart with Hackney?", "restart the search in Hackney",
    "Let's start from scratch in Hackney",
])
def test_v_02_a_reset_keeps_the_place_it_names(message):
    out = _state_node(_CAMDEN, message)
    assert out["area"] == "Hackney" and out["areas"] == ["Hackney"]
    assert out["max_budget"] is None and out["bedrooms"] is None
    assert out["commute_destination"] is None and out["max_travel_time"] is None


@pytest.mark.parametrize("message", ["Start from scratch with a 2-bed", "Start over with a 2-bed"])
def test_v_02_what_follows_with_may_be_no_place(message):
    out = _state_node(_CAMDEN, message)
    assert out["area"] is None and out["bedrooms"] == 2


@pytest.mark.parametrize("message", ["I start again in May", "We start over in September"])
def test_v_02_a_date_after_start_again_is_no_reset(message):
    assert _state_node(_CAMDEN, message)["area"] == "Camden"


# V-03 ── the gate asks once, is not re-dispatched, and "any budget" is an answer
_GATE_Q = ("Before I search, could you confirm a couple of things: what room type you'd like "
           "(studio / en-suite / shared)? You can also just say \"continue\" to search anyway, or "
           "fill in the search-criteria panel on the right and press Search.")
_GATE_ARTIFACT = {"tool": "search_properties", "success": False, "raw_data": {
    "success": False, "status": "need_clarification", "clarification_kind": "soft_criteria",
    "question": _GATE_Q, "missing_fields": ["room_type"]}}


def _recall_state(message, **call):
    from langchain_core.messages import AIMessage

    from tests.test_agent_contract_guards import _state, _tc
    from tests.test_qa_null_injection import _strict_null_args
    state = _state(message)
    state["accumulated_search_criteria"] = {"area": "Edinburgh", "areas": ["Edinburgh"],
                                            "criteria_gate_shown": True}
    state["tool_artifacts"] = [dict(_GATE_ARTIFACT)]
    args = _strict_null_args("search_properties", {"user_query": message, **call})
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_properties", args, "c0")])]
    return state


@pytest.mark.parametrize("call", [{"confirmed": True}, {}])
def test_v_03_a_same_turn_recall_is_not_dispatched_and_ends_on_the_question(call):
    from core.agent_loop import build_fc_nodes
    from tests.test_qa_null_injection import _ValidatingProvider
    provider = _ValidatingProvider(["search_properties"])
    nodes = build_fc_nodes(provider)
    command = asyncio.run(nodes["execute_tools"](_recall_state("Find me a flat in Edinburgh", **call)))
    assert provider.calls == [] and command.goto == "format_output_fc"
    state = {**_recall_state("Find me a flat in Edinburgh"), **command.update}
    out = nodes["format_output_fc"](state)
    assert out["final_response"].count("Before I search") == 1
    assert out["response_type"] == "clarification" and out["tool_data"]["missing_fields"] == ["room_type"]


def test_v_03_a_recall_carrying_the_stated_fields_runs_unconfirmed():
    from core.agent_loop import build_fc_nodes
    from tests.test_qa_null_injection import _ValidatingProvider
    provider = _ValidatingProvider(["search_properties"])
    message = "A studio in Edinburgh for me, up to 900 a month, I do not commute"
    state = _recall_state(message, confirmed=True, room_type="studio", max_budget=900, no_commute=True)
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    (_, params), = provider.calls
    assert not params.get("confirmed") and not params.get("criteria_gate_shown")
    assert params["room_type"] == "studio"


def test_v_03_a_recall_beside_another_result_goes_back_to_the_model():
    from core.agent_loop import build_fc_nodes
    from tests.test_qa_null_injection import _ValidatingProvider
    state = _recall_state("Find me a flat in Edinburgh and what's the weather?")
    state["tool_artifacts"].append({"tool": "get_weather", "success": True, "raw_data": {"temp": 9}})
    command = asyncio.run(build_fc_nodes(_ValidatingProvider(["search_properties"]))["execute_tools"](state))
    assert command.goto == "agent"


@pytest.mark.parametrize("reply", [
    "I couldn't run the Manchester search. The search system still needs a few details before it "
    "will return listings, even though you said any budget. Could you tell me:\n\n"
    "1. Room type: studio, en-suite, shared, or a whole self-contained flat?\n"
    "2. Monthly budget: \"any budget\" is fine, but a rough ceiling helps narrow the results.\n"
    "3. Commute: do you travel to a university or workplace in Manchester? If so, where. If not, "
    "just say \"no commute\".\n4. Move-in date (optional).\n\n"
    "If you reply with even one or two of these, for example \"whole flat, no commute\", I'll "
    "search straight away.",
    "I couldn't pull up any Inverness listings yet. The search needs a bit more detail before it "
    "will return results, and my repeated attempts with \"any budget\" didn't get through.\n\n"
    "Could you tell me any of the following? Even one or two will help:\n"
    "- Room type: studio, en-suite, shared, or a whole self-contained flat\n"
    "- Monthly budget, or confirm you really have no limit\n"
    "- Whether you need to commute anywhere (for example a university or workplace), or say "
    "\"no commute\"\n- Your preferred move-in date (optional)\n\n"
    "Once you reply, I'll run the search for Inverness with those criteria.",
])
def test_v_03_the_models_wording_of_the_gate_gives_way_to_the_question(reply):
    from core.agent_loop import answer_with_clarification
    assert answer_with_clarification(reply, _GATE_Q, alone=True) == _GATE_Q


def test_v_03_a_compound_answer_keeps_its_facts_and_asks_once():
    from core.agent_loop import answer_with_clarification
    text = ("In Inverness it is 9.5 C with light rain. The fare is £2.80, so factor it into your "
            "budget. I couldn't run the search. What room type would you like?")
    out = answer_with_clarification(text, _GATE_Q)
    assert "9.5 C with light rain" in out and "into your budget" in out
    assert "couldn't" not in out and "What room type" not in out and out.count(_GATE_Q) == 1


def test_v_03_a_question_the_user_asked_is_still_answered():
    from core.agent_loop import answer_with_clarification
    text = "A break clause lets either side end the tenancy early. Which room type would you like?"
    out = answer_with_clarification(text, _GATE_Q, alone=True)
    assert out == f"A break clause lets either side end the tenancy early.\n\n{_GATE_Q}"


@pytest.mark.parametrize("message", ["Flats in Manchester, any budget", "no budget limit",
                                     "预算不限", "budget doesn't matter"])
def test_v_03_an_open_budget_answers_the_budget_question(message):
    from core.search_readiness import assess_search_readiness
    readiness = assess_search_readiness(
        resolved_area="Manchester", max_budget=None, room_type=None, no_commute=False,
        commute_destination=None, criteria_gate_shown=False, confirmed=False, user_message=message)
    assert readiness.missing_soft == ("room_type", "commute")


def test_v_03_a_gate_is_a_final_result_and_is_not_retried():
    import ast
    import pathlib
    source = pathlib.Path(importlib.import_module("core.tools.search_properties").__file__).read_text(
        encoding="utf-8")
    gates = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Dict) and any(
        isinstance(k, ast.Constant) and k.value == "status" and isinstance(v, ast.Constant)
        and v.value == "need_clarification" for k, v in zip(node.keys, node.values))]
    assert gates and all(any(isinstance(k, ast.Constant) and k.value == "retryable"
                             for k in node.keys) for node in gates)


# V-13 ── a budget the user stated earlier is no external figure on a gate turn
def test_v_13_a_stated_budget_keeps_the_reply():
    from uk_rent_agent.agent.critic import enforce_no_evidence_numeric_contract
    reply = "Sure, a 1-bed in Bow under £2,000 a month."
    assert enforce_no_evidence_numeric_contract(
        reply, retrieved_evidence=[], user_message="帮我在Bow找一套一居室。",
        reply_language="zh", stated_budgets=[2000, None]) == reply


def test_v_13_a_figure_nobody_stated_is_still_withdrawn():
    from uk_rent_agent.agent.critic import enforce_no_evidence_numeric_contract
    out = enforce_no_evidence_numeric_contract(
        "Typical rent in Bow is £1,750 a month.", retrieved_evidence=[], user_message="Rent in Bow?",
        stated_budgets=[2000])
    assert "1,750" not in out


def test_v_13_the_critics_stock_line_never_opens_a_clarification():
    from core.agent_loop import answer_with_clarification
    from uk_rent_agent.agent.critic import no_reliable_data_message
    for language in ("zh", "en"):
        assert answer_with_clarification(no_reliable_data_message(language), _GATE_Q) == _GATE_Q
