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


# V-04 ── a zh budget keeps its thousands separator
@pytest.mark.parametrize("message,amount", [
    ("我想在Hackney租一套两居室，预算每月£2,500以内。", 2500), ("帮我在Hackney找两居室，预算每月£1,900以内，不通勤。", 1900),
    ("预算£2,000以内", 2000), ("预算2,500以内", 2500), ("预算2500以下", 2500), ("￡2,500以内", 2500),
    ("每月不超过2,500", 2500), ("不高于2,500", 2500), ("最多2,500英镑", 2500), ("2,500镑左右", 2500),
    ("£2,500封顶", 2500), ("2,500之内", 2500), ("月租低于1,800", 1800), ("低于2,000的房子", 2000),
    ("预算2.5k", 2500), ("Find a 2-bed in Hackney under £2,500", 2500),
])
def test_v_04_a_zh_budget_keeps_its_thousands(message, amount):
    from core.tools.search_properties import _extract_budget
    assert _extract_budget(message) == (amount, "month")


@pytest.mark.parametrize("message", ["不超过500米", "离地铁站不超过1500米", "少于300平", "押金不超过3000", "不超过30分钟"])
def test_v_04_a_distance_a_floor_area_or_a_deposit_is_no_ceiling(message):
    from core.tools.search_properties import _extract_budget
    assert _extract_budget(message) == (None, None)


# V-05 ── a trailing question does not cancel an explicit switch
_BOW_STATE = {"area": "Camden", "areas": ["Camden"], "bedrooms": 1, "max_budget": 2000, "no_commute": True}


@pytest.mark.parametrize("message", [
    "Switch the area to Bow and tell me how safe it is.", "Switch the area to Bow and tell me the weather.",
    "Change the area to Bow and tell me the weather.", "Move the search to Bow and what is the weather there?",
    "Switch to Bow",
])
def test_v_05_a_trailing_question_keeps_the_switch(message):
    assert _state_node(_BOW_STATE, message)["area"] == "Bow"


@pytest.mark.parametrize("message", ["Is Bow safe?", "How far is Bow?", "What is the weather in Bow?"])
def test_v_05_a_question_about_a_place_moves_nothing(message):
    assert _state_node(_BOW_STATE, message)["area"] == "Camden"


# V-06 ── a correction list naming a known area switches it
_HACKNEY = {"area": "Hackney", "areas": ["Hackney"], "bedrooms": 1, "max_budget": 1800, "room_type": "whole"}


@pytest.mark.parametrize("message,beds", [
    ("actually make it 2 bedrooms and Clapham", 2), ("2 bedrooms and Clapham", 2),
    ("Make it 2 bedrooms and Clapham please", 2), ("£2,000 and Clapham", 1),
])
def test_v_06_a_correction_list_names_the_area(message, beds):
    out = _state_node(_HACKNEY, message)
    assert out["area"] == "Clapham" and out["bedrooms"] == beds


@pytest.mark.parametrize("message", [
    "2 bedrooms and a garden", "2 bedrooms and Bow Street", "I need 2 bedrooms and Clapham Junction is nearby",
])
def test_v_06_anything_else_after_and_keeps_the_area(message):
    assert _state_node(_HACKNEY, message)["area"] == "Hackney"


def test_v_06_a_followup_place_sent_as_location_is_searched():
    from core.agent_loop import build_fc_nodes
    from tests.test_agent_contract_guards import _state, _tc
    from tests.test_qa_null_injection import _ValidatingProvider, _strict_null_args
    from langchain_core.messages import AIMessage
    message = "actually make it 2 bedrooms and Kew"
    state = _state(message)
    state["accumulated_search_criteria"] = {"area": "Hackney", "areas": ["Hackney"], "room_type": "whole",
                                            "max_budget": 1800, "no_commute": True}
    args = _strict_null_args("search_properties", {"user_query": message, "location": "Kew, London",
                                                   "area": "Hackney"})
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_properties", args, "c0")])]
    provider = _ValidatingProvider(["search_properties"])
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    (_, params), = provider.calls
    assert params["area"] == "Kew" and "location" not in params


# V-07 ── a new workplace is stated in more ways than "I work at"
_LSE = "London School of Economics, Houghton Street, London WC2A 2AE"
_HOLBORN = {"area": "Holborn", "areas": ["Holborn"], "bedrooms": 1, "max_budget": 2600,
            "commute_destination": _LSE, "destination": _LSE}


@pytest.mark.parametrize("message", [
    "Never mind LSE, I will be working at Canary Wharf instead. Recompute for the same listing",
    "I'll be working at Canary Wharf", "I am going to work at Canary Wharf", "My new job is at Canary Wharf",
    "I'm working at Canary Wharf now", "I now work at Canary Wharf instead", "我改成在金丝雀码头上班了",
    "不去LSE了，改成在金丝雀码头上班", "我现在改成在Canary Wharf上班", "工作地点改成金丝雀码头",
])
def test_v_07_the_new_workplace_replaces_the_destination(message):
    out = _state_node(_HOLBORN, message)
    assert out["commute_destination"] == "Canary Wharf, London E14 5AB"
    assert out["destination"] == out["commute_destination"] and out["area"] == "Holborn"


@pytest.mark.parametrize("message", ["I will be working at a bank", "I am working in finance"])
def test_v_07_what_the_job_is_names_no_destination(message):
    assert _state_node(_HOLBORN, message)["commute_destination"] == _LSE


def test_v_07_a_forgotten_workplace_is_still_dropped():
    assert _state_node(_HOLBORN, "Forget that I work at LSE").get("commute_destination") is None


# V-08 ── a stated pet requirement is recorded, verified or listed as not checked
@pytest.mark.parametrize("message", [
    "Find a 2 bed in Hackney under 3000 with a garden and pets allowed",
    "Find a 2 bed in Hackney under 3000, pets allowed", "I need it to be pet friendly.",
    "Find a pet friendly 1 bed in Camden",
])
def test_v_08_a_stated_pet_requirement_is_a_feature(message):
    assert _state_node({"area": "Bow"}, message)["property_features"] == ["pets allowed"]


def test_v_08_the_garden_stays_beside_the_pets():
    out = _state_node({}, "Find a 2 bed in Hackney under 3000 with a garden and pets allowed")
    assert out["soft_preferences"] == ["Would like garden"]


def test_v_08_a_soft_wish_is_listed_as_not_checked():
    assert _state_node({"area": "Bow"}, "I'd like pets allowed if possible")["soft_preferences"] == [
        "Would like pets allowed"]


@pytest.mark.parametrize("message", [
    "Are pets allowed?", "Are pets allowed in the first one?", "No pets allowed is fine",
    "Find a flat with no pets allowed",
])
def test_v_08_a_question_or_a_refusal_adds_nothing(message):
    assert not _state_node({"area": "Bow"}, message).get("property_features")


@pytest.mark.parametrize("message", [
    "No wait, I'm not bringing a pet after all.", "I don't need pets allowed",
    "No need for it to be pet friendly",
])
def test_v_08_a_withdrawal_removes_it(message):
    state = {"area": "Bow", "property_features": ["pets allowed"]}
    assert not _state_node(state, message).get("property_features")


# V-09 ── 附近 is a preposition: amenities are looked up when the user asks about them
@pytest.mark.parametrize("message", [
    "我想在国王十字附近找一个一居室，预算每月2200镑以内", "伦敦玛丽女王大学附近有没有合租单间，预算800以内？",
    "请记住我在国王十字附近上班", "Angel周边的studio", "牛津大学附近有房吗",
    "找一个Angel附近的一居室预算2300以内，同时告诉我Angel的天气和治安",
])
def test_v_09_near_is_no_amenity_question(message):
    from core.dimensions import cued_dimensions
    from core.tool_policy import unasked_nearby_lookup
    assert "nearby" not in cued_dimensions(message)
    assert unasked_nearby_lookup({"address": "Angel"}, message)


@pytest.mark.parametrize("message", [
    "Angel附近有什么超市？", "这里附近有什么？", "周边环境怎么样", "附近配套如何", "附近要有超市", "what's nearby?",
    "Any supermarkets near the first flat?", "What's near the flat?", "How close is the nearest gym?",
    "Is there anything good around there?", "Are there shops close by?", "Find a flat near a Tesco",
])
def test_v_09_a_real_question_still_runs(message):
    from core.tool_policy import unasked_nearby_lookup
    assert not unasked_nearby_lookup({"address": "Angel"}, message)


@pytest.mark.parametrize("message", ["Find a flat near Angel", "Any flats near Finsbury Park?",
                                     "2-bed in Bethnal Green under £2,000"])
def test_v_09_an_english_home_search_needs_no_lookup_either(message):
    from core.tool_policy import unasked_nearby_lookup
    assert unasked_nearby_lookup({"address": "Angel", "poi_type": "all"}, message)


def test_v_09_a_type_the_model_named_runs():
    from core.tool_policy import unasked_nearby_lookup
    assert not unasked_nearby_lookup({"address": "Angel", "poi_type": "park"}, "Find a flat near Angel")


def test_v_09_a_dropped_call_still_answers_the_provider():
    from langchain_core.messages import AIMessage, ToolMessage

    from core.agent_loop import build_fc_nodes
    from tests.test_agent_contract_guards import _state, _tc
    from tests.test_qa_null_injection import _ValidatingProvider, _strict_null_args
    message = "我想在国王十字附近找一个一居室"
    state = _state(message)
    args = _strict_null_args("search_nearby_pois", {"address": "King's Cross"})
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_nearby_pois", args, "c0")])]
    provider = _ValidatingProvider(["search_nearby_pois"])
    command = asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    assert provider.calls == []
    replies = [m for m in command.update["messages"] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in replies] == ["c0"] and command.goto == "agent"
    assert all(a.get("denied") for a in command.update["tool_artifacts"])
