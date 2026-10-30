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


# V-10 ── an explicit save is not refused because the model translated it
_ZH_SAVE = "请记住：我对猫过敏，我喜欢安静的Chelsea。我的预算是每月2600镑，一居室，不需要通勤，继续。"


def _remember_turn(message, proposal, *, search=True):
    from langchain_core.messages import AIMessage
    import dataclasses

    from core import memory_gate
    from core.agent_loop import build_fc_nodes
    from tests.test_agent_contract_guards import _state, _tc
    from tests.test_qa_null_injection import _ValidatingProvider, _strict_null_args
    state = _state(message)
    state["session_id"] = "s-" + uuid.uuid4().hex   # a denied write freezes under the session
    provider = _ValidatingProvider(["search_properties", "remember"])
    provider.specs = [dataclasses.replace(spec, side_effect="write" if spec.name == "remember" else "none")
                      for spec in provider.specs]
    calls = [_tc("remember", _strict_null_args("remember", {"content": proposal}), "c0")]
    if search:
        calls.append(_tc("search_properties", _strict_null_args("search_properties", {"user_query": message}), "c1"))
    state["messages"] = [AIMessage(content="", tool_calls=calls)]
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    return dict(provider.calls).get("remember"), memory_gate


@pytest.mark.parametrize("message,proposal", [
    (_ZH_SAVE, "User is allergic to cats and prefers a quiet Chelsea; budget £2600 per month, "
               "1-bedroom whole flat, no commute needed."),
    ("请记住：我对猫过敏，我喜欢安静的Knightsbridge。预算每月2600镑，一居室，不需要通勤，继续。",
     "User is allergic to cats and prefers quiet Knightsbridge."),
    ("请记住我在金丝雀码头上班，帮我找房", "User works at Canary Wharf."),
    ("Please remember that I have a dog, and find me a flat", "用户有一只狗。"),
])
def test_v_10_a_translated_proposal_saves_the_users_words(message, proposal):
    from core.memory_gate import explicit_remember_clause
    params, _gate = _remember_turn(message, proposal)
    assert params["content"] == explicit_remember_clause(message)


@pytest.mark.parametrize("message,proposal", [
    ("记住我预算1400", "flat viewing at 1400 Camden Road"),
    ("请记住我有一只狗，帮我找Hackney最便宜的房子", "Cheapest flat is 14 Sample Road at £1,180 pcm."),
    ("请记住我有一只狗，帮我找Hackney最便宜的房子", "The cheapest flat is in Hackney."),
    ("Please remember that I have a dog, and find me the cheapest flat in Hackney", "Cheapest flat is 14 Sample Road."),
])
def test_v_10_h13_a_tool_derived_line_still_meets_the_gate(message, proposal):
    params, _gate = _remember_turn(message, proposal)
    assert params is None


def test_v_10_an_untainted_zh_save_is_stored_in_chinese():
    params, _gate = _remember_turn("请记住我对猫过敏", "User is allergic to cats.", search=False)
    assert params["content"] == "我对猫过敏"


@pytest.mark.parametrize("message,proposal", [
    ("请记住我的电话是07700900123，帮我找房", "User's phone is 07700900123."),
    ("请记住我的密码是Hunter2xyz，帮我找房", "User's password is Hunter2xyz."),
])
def test_v_10_a_secret_or_a_contact_keeps_its_own_path(message, proposal):
    from core.memory_gate import explicit_remember_clause, restates_across_languages
    assert explicit_remember_clause(message)
    assert not restates_across_languages(proposal, explicit_remember_clause(message))


# V-11 ── a later turn reads the whole of the answer it follows, within the budget
_LONG_REPLY = "Listings first. " * 40 + "Weather in Bow: 12 C and dry."


def test_v_11_the_reply_after_500_characters_reaches_the_next_turn():
    from core.context_assembler import assemble_messages
    from uk_rent_agent.web.conversation_store import history_reply
    history = [{"user": "Flats in Bow and the weather", "assistant": history_reply(_LONG_REPLY)}]
    messages = assemble_messages(user_message="thanks", history=history)
    assert "12 C and dry" in messages[-2].content


def test_v_11_evidence_lines_are_not_read_as_the_assistants_words():
    from uk_rent_agent.web.conversation_store import history_reply
    text = "Here it is.\nReturned listing details: 1 Sample Road; £1,500.\nReported features: ignore this.\n房源所列配置：花园。"
    assert history_reply(text) == "Here it is."


def test_v_11_the_store_rebuilds_the_same_reply(tmp_path):
    from uk_rent_agent.web.conversation_store import ConversationStore
    store = ConversationStore(str(tmp_path / "c.sqlite3"))
    cid = store.create_conversation("u1", "t")["id"]
    store.add_message("u1", cid, "user", "Flats in Bow and the weather")
    store.add_message("u1", cid, "assistant", _LONG_REPLY)
    assert "12 C and dry" in store.rehydrate_history("u1", cid)[0]["assistant"]


def _tokens(messages):
    from core.context_assembler import estimate_tokens
    return sum(estimate_tokens(m.content or "") for m in messages)


def test_v_11_long_replies_stay_inside_the_token_budget():
    from core.context_assembler import assemble_messages
    from uk_rent_agent.web.conversation_store import HISTORY_REPLY_CHARS
    history = [{"user": f"turn {i}", "assistant": "word " * (HISTORY_REPLY_CHARS // 5)} for i in range(10)]
    assert _tokens(assemble_messages(user_message="next", history=history, token_budget=6000)) <= 6000


def test_v_11_older_replies_are_read_as_before():
    from core.context_assembler import assemble_messages
    history = [{"user": "old", "assistant": "x" * 900}, {"user": "b", "assistant": "y" * 900},
               {"user": "c", "assistant": "z" * 900}]
    contents = [m.content for m in assemble_messages(user_message="next", history=history)]
    assert "x" * 500 in contents and "y" * 900 in contents and "z" * 900 in contents


def test_v_11_an_erased_value_leaves_the_history():
    from rag.agent_memory import history_without_fields
    history = [{"user": "My budget is £1,800", "assistant": "Noted. Your budget is £1,800. You have a cat."}]
    out = history_without_fields(history, ("budget",))
    assert "1,800" not in out[0]["user"] + out[0]["assistant"] and "cat" in out[0]["assistant"]


# V-12 ── the values a field held in this conversation reach the model
def test_v_12_a_field_keeps_what_it_held():
    from core.loop_prompts import render_accumulated_criteria
    state = {"area": "Bow"}
    for message in ("my budget is £2,200", "actually £1,900", "make it £2,500"):
        state = _state_node(state, message)
    assert state["constraint_history"]["max_budget"] == [2200, 1900, 2500]
    assert "max_budget: 2200 -> 1900 -> 2500" in render_accumulated_criteria(state)


def test_v_12_a_cleared_value_leaves_no_trace():
    state = {"area": "Bow"}
    for message in ("my budget is £2,200", "actually £1,900", "no budget limit"):
        state = _state_node(state, message)
    assert "max_budget" not in state.get("constraint_history", {})


def test_v_12_one_value_changes_nothing_and_a_reset_forgets():
    from core.loop_prompts import render_accumulated_criteria
    state = _state_node({"area": "Bow"}, "my budget is £2,200")
    assert "Changed in this conversation" not in render_accumulated_criteria(state)
    state = _state_node(_state_node(state, "actually £1,900"), "Start from scratch")
    assert not (state.get("constraint_history") or {}).get("max_budget")


# V-14 ── a write the model tried on a recall turn does not replace the recall
def test_v_14_a_denied_write_on_a_recall_turn_leaves_the_answer():
    from core.memory_contract import (RECALL_TURN_WRITE_ERROR, compose_memory_contract_response,
                                      memory_contract_from_artifacts)
    contract = memory_contract_from_artifacts([{"tool": "remember", "denied": True, "success": False,
                                                "error": RECALL_TURN_WRITE_ERROR}])
    assert contract == {}
    assert compose_memory_contract_response("I have nothing saved.", contract) == "I have nothing saved."


def test_v_14_any_other_denied_write_keeps_its_notice():
    from core.memory_contract import memory_contract_from_artifacts
    contract = memory_contract_from_artifacts([{"tool": "remember", "denied": True, "success": False,
                                                "error": "denied: tainted write requires confirmation"}])
    assert contract["requested"] and contract["attempted"] is False


# V-15 ── a zh withdrawal reaches the English-labelled wish
@pytest.mark.parametrize("message,wish", [
    ("忘掉我喜欢公园这件事。", "Would like park"), ("不要洗碗机了", "Must have dishwasher"),
    ("洗碗机算了", "Would like dishwasher"), ("不需要阳台", "Would like balcony"),
])
def test_v_15_a_zh_withdrawal_removes_the_wish(message, wish):
    state = {"area": "Bow", "soft_preferences": [wish, "Would like gym"]}
    assert _state_node(state, message)["soft_preferences"] == ["Would like gym"]


@pytest.mark.parametrize("message", ["我喜欢公园", "要有洗碗机", "这里的公园怎么样？"])
def test_v_15_a_mention_is_no_withdrawal(message):
    state = {"area": "Bow", "soft_preferences": ["Would like park", "Must have dishwasher"]}
    out = _state_node(state, message)["soft_preferences"]
    assert "Would like park" in out and "Must have dishwasher" in out


# V-16 ── the home before 附近 is the area; a fare's destination is not
@pytest.mark.parametrize("message,area", [
    ("找一个Angel附近的一居室预算2300以内，同时告诉我Angel的天气和治安，还有去国王十字的车费", "Angel"),
    ("找一个Hackney附近的两居室，还有到金丝雀码头的路线", "Hackney"),
    ("帮我找卡姆登附近的studio，去国王十字的票价是多少", "Camden"),
])
def test_v_16_the_home_is_what_comes_before_near(message, area):
    assert _state_node({}, message, [])["area"] == area


def test_v_16_a_fare_alone_moves_nothing():
    assert _state_node({"area": "Bow", "areas": ["Bow"]}, "去国王十字的车费多少钱")["area"] == "Bow"


# V-17 ── an unstated commute stays unstated
def test_v_17_a_search_does_not_make_no_commute_false():
    from core.langgraph_agent import update_search_criteria
    assert update_search_criteria({"area": "Bow"}, {"no_commute": False}).get("no_commute") is None
    assert update_search_criteria({"no_commute": True}, {"no_commute": False})["no_commute"] is True


@pytest.mark.parametrize("criteria,line", [
    ({"area": "Bow", "no_commute": False}, "commute: not specified"),
    ({"area": "Bow"}, "commute: not specified"),
    ({"area": "Bow", "no_commute": True}, "no_commute: True"),
    ({"area": "Bow", "no_commute": False, "commute_destination": "Bank"}, "commute_destination: Bank"),
])
def test_v_17_the_recap_reads_three_states(criteria, line):
    from core.loop_prompts import render_accumulated_criteria
    text = render_accumulated_criteria(criteria)
    assert line in text and "no_commute: False" not in text


# V-18 ── a commute from a listing starts at the listing's own coordinates
@pytest.mark.parametrize("tool", ["calculate_commute", "calculate_commute_cost"])
def test_v_18_the_listing_coordinates_reach_the_commute_tool(tool):
    from langchain_core.messages import AIMessage

    from core.agent_loop import build_fc_nodes
    from tests.test_agent_contract_guards import _state, _tc
    from tests.test_qa_null_injection import _ValidatingProvider, _strict_null_args
    message = "Whats the commute from the first one to Canary Wharf?"
    state = _state(message)
    state["extracted_context"]["last_results"] = [{"address": "King Square, London, EC1V",
                                                   "geo_location": "51.5262,-0.0989"}]
    args = _strict_null_args(tool, {"from_address": "King Square, London, EC1V",
                                    "to_address": "Canary Wharf, London"})
    state["messages"] = [AIMessage(content="", tool_calls=[_tc(tool, args, "c0")])]
    provider = _ValidatingProvider([tool])
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    (_, params), = provider.raw_calls
    assert params["_from_coordinates"] == {"lat": 51.5262, "lng": -0.0989}


def test_v_18_the_route_is_planned_from_those_coordinates(monkeypatch):
    from core import maps_service
    monkeypatch.setattr(maps_service, "_get_coordinates", lambda a: None if "King Square" in a else {"lat": 51.5, "lng": -0.02})
    seen = {}

    def journey(origin, destination, mode):
        seen["origin"] = origin
        return {"duration": 17, "legs": []}
    monkeypatch.setattr(maps_service, "_tfl_journey", journey)
    monkeypatch.setattr(maps_service, "_summarise_tfl_legs", lambda j: ([], "route"))
    monkeypatch.setattr(maps_service, "_tfl_changes_count", lambda j: 0)
    assert maps_service.calculate_travel_details("King Square, London, EC1V", "Bank", "transit") is None
    out = maps_service.calculate_travel_details("King Square, London, EC1V", "Bank", "transit",
                                                origin_coordinates={"lat": 51.52, "lng": -0.09})
    assert out["duration_minutes"] == 17 and seen["origin"]["lat"] == 51.52


@pytest.mark.parametrize("address,zone", [("King Square, London, EC1V", 1), ("St. Clements Street, Islington, London, N7", 2)])
def test_v_18_a_display_address_gives_its_zone(address, zone):
    from core.tools.calculate_commute_cost import _get_zone_from_address
    assert _get_zone_from_address(address) == zone


# V-19 ── a limit the user stated earlier is theirs, not an unverified claim
def test_v_19_an_earlier_limit_is_a_known_cap():
    from core.candidate_validation import _known_commute_caps
    state = {"extracted_context": {"current_message": "Summarise my final criteria.",
                                   "history": [{"user": "I commute to Holborn, max 30 minutes.", "assistant": "ok"}]}}
    assert 30.0 in _known_commute_caps(state)


# V-21 ── an unresolved place ends in a question, not a search elsewhere
def test_v_21_the_old_destination_does_not_choose_the_search():
    from langchain_core.messages import AIMessage

    from core.agent_loop import build_fc_nodes
    from tests.test_agent_contract_guards import _state, _tc
    from tests.test_qa_null_injection import _ValidatingProvider, _strict_null_args
    message = "温莎附近有房吗"
    state = _state(message)
    state["accumulated_search_criteria"] = {
        "area": None, "areas": [], "max_budget": 1800, "room_type": "whole", "bedrooms": 1,
        "commute_destination": "Imperial College London, South Kensington, London SW7 2AZ"}
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_properties", _strict_null_args(
        "search_properties", {"user_query": message, "confirmed": True}), "c0")])]
    provider = _ValidatingProvider(["search_properties"])
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    (_, params), = provider.calls
    assert not params.get("commute_destination") and not params.get("area") and not params.get("location")


def test_v_21_the_tool_then_asks_where():
    from core.tools.search_properties import search_properties_impl
    out = asyncio.run(search_properties_impl(user_query="温莎附近有房吗", current_message="温莎附近有房吗",
                                             max_budget=1800, room_type="whole", confirmed=True))
    assert out["status"] == "need_clarification" and out["clarification_kind"] == "missing_area"


def test_v_21_a_destination_this_message_names_still_stands():
    from core.tools.search_properties import unresolved_zh_search_place
    assert unresolved_zh_search_place("帮我在Hackney找房，通勤到帝国理工") is None


# V-22 ── a saved value the search did not carry is not listed as used
_USED_REPLY = ("我这次用到的偏好（都来自你之前告诉我的内容）：\n- 区域：Hackney\n- 预算：每月 £2,500 以内\n- 户型：两居室公寓\n"
               "- 软性偏好：不喜欢吵闹的地方\n\n符合的只有两套。")


def _search_with(**criteria):
    return [{"tool": "search_properties", "success": True,
             "raw_data": {"success": True, "status": "found", "search_criteria": criteria}}]


def test_v_22_the_used_list_drops_what_the_search_lacked():
    from core.agent_loop import without_unapplied_used_claims
    out = without_unapplied_used_claims(_USED_REPLY, _search_with(area="Hackney"))
    assert "£2,500" not in out and "两居室" not in out and "- 区域：Hackney" in out and "符合的只有两套" in out


def test_v_22_what_the_search_used_stays():
    from core.agent_loop import without_unapplied_used_claims
    assert without_unapplied_used_claims(
        _USED_REPLY, _search_with(area="Hackney", max_budget=2500, bedrooms=2)) == _USED_REPLY


def test_v_22_a_list_of_listings_is_not_a_used_list():
    from core.agent_loop import without_unapplied_used_claims
    text = "Two homes fit:\n- Kenworthy Road: £2500, 2 bed\n- Kingsland Road: £2200, 2 bed"
    assert without_unapplied_used_claims(text, _search_with(area="Hackney")) == text


# V-23, V-24 ── prompt rules
def test_v_23_the_remember_tool_says_no_gender_from_a_name():
    from core import loop_prompts
    from core.tools.memory_tools import remember_tool
    assert "never infer a gender" in remember_tool.description
    assert "a first name says nothing about gender" in loop_prompts.behaviour_rules()


def test_v_24_listings_keep_the_card_order():
    from core import loop_prompts
    assert "never re-sort them in the text" in loop_prompts.behaviour_rules()


# P3 ── B-F13 an erasure that states the new value asks for no new value
def test_b_f13_the_ack_does_not_ask_for_what_the_message_gave():
    from core.memory_gate import acknowledge_memory_erasure
    report = {"fields": ("budget",), "complete": True, "deleted": 1, "rewritten": 0}
    asks = acknowledge_memory_erasure("", report, "en")
    gives = acknowledge_memory_erasure("", report, "en", states_new_value=True)
    assert "Tell me a new budget" in asks and "I've deleted the budget" in gives
    assert "Tell me a new budget" not in gives
    assert "告诉我新的" not in acknowledge_memory_erasure("", report, "zh", states_new_value=True)


# Small ── the incomplete-erasure text names the real menu entry
def test_small_the_incomplete_erasure_names_the_page_label():
    from core.memory_gate import MEMORY_ERASURE_INCOMPLETE_EN, MEMORY_ERASURE_INCOMPLETE_ZH
    assert "Delete my data" in MEMORY_ERASURE_INCOMPLETE_EN and "forget me" not in MEMORY_ERASURE_INCOMPLETE_EN
    assert "删除我的数据" in MEMORY_ERASURE_INCOMPLETE_ZH


# P3 ── A-F8 a failed save does not talk about listings
@pytest.mark.parametrize("message,listings", [
    ("Please remember that I am vegan and my budget is £1,400.", False),
    ("Please remember that I am vegan, and find me a flat", True),
    ("Find me a flat under £1,400", True),
])
def test_a_f8_a_failed_save_says_so(message, listings):
    from core.agent_loop import _artifact_grounded_fallback_answer
    from tests.test_agent_contract_guards import _state
    out = _artifact_grounded_fallback_answer(_state(message), reason="provider_error")
    assert ("listing results" in out) is listings
    assert ("not saved to memory" in out) is (not listings)


# P3 ── B-F6 a pair beyond London gets no London-calibrated figure
def test_b_f6_outside_coverage_gives_no_figure(monkeypatch):
    from core import maps_service
    points = {"Newcastle": {"lat": 54.97, "lng": -1.61}, "Leeds": {"lat": 53.80, "lng": -1.55}}
    monkeypatch.setattr(maps_service, "_get_coordinates", lambda a: points[a])
    monkeypatch.setattr(maps_service, "_tfl_journey", lambda *a: None)
    monkeypatch.setattr(maps_service, "straight_line_travel_estimate",
                        lambda *a, **k: {"minutes": 500, "distance_km": 130.0})
    out = maps_service.calculate_travel_details("Newcastle", "Leeds", "transit")
    assert out["estimated_duration_minutes"] is None and out["estimate_high_minutes"] is None
    assert "outside" in out["basis_note"].lower() and out["duration_minutes"] is None


def test_b_f6_a_short_pair_keeps_its_estimate(monkeypatch):
    from core import maps_service
    points = {"Watford": {"lat": 51.75, "lng": -0.39}, "Bank": {"lat": 51.51, "lng": -0.09}}
    monkeypatch.setattr(maps_service, "_get_coordinates", lambda a: points[a])
    monkeypatch.setattr(maps_service, "_tfl_journey", lambda *a: None)
    monkeypatch.setattr(maps_service, "straight_line_travel_estimate",
                        lambda *a, **k: {"minutes": 60, "distance_km": 34.0})
    assert maps_service.calculate_travel_details("Watford", "Bank", "transit")["estimated_duration_minutes"]


# P3 ── C-F03, C-F04, C-07 through the API
@pytest.fixture
def client(tmp_path, monkeypatch):
    from dataclasses import replace

    from uk_rent_agent.web.conversation_store import ConversationStore
    appmod = importlib.import_module("app")
    monkeypatch.setattr(appmod, "conversation_store", ConversationStore(str(tmp_path / "c.sqlite3")))
    monkeypatch.setattr(appmod, "_runtime_config",
                        replace(appmod._runtime_config, allow_legacy_client_user_id=True))
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


@pytest.mark.parametrize("title", ["", "   ", "a" * 1001])
def test_c_f03_a_blank_or_huge_title_is_refused_on_create(client, title):
    response = client.post("/api/conversations", json={"user_id": "guest-" + uuid.uuid4().hex, "title": title})
    assert response.status_code == 400


def test_c_f03_no_title_and_a_normal_one_still_work(client):
    user = "guest-" + uuid.uuid4().hex
    assert client.post("/api/conversations", json={"user_id": user}).status_code == 201
    assert client.post("/api/conversations", json={"user_id": user, "title": "New chat"}).status_code == 201


def test_c_f04_security_headers(client):
    headers = client.get("/api/conversations").headers
    assert headers["X-Content-Type-Options"] == "nosniff" and headers["X-Frame-Options"] == "DENY"


def test_c_07_a_busy_erasure_has_its_own_page_text_and_retry_after():
    import pathlib
    page = (pathlib.Path(__file__).parents[1] / "app" / "unified-ui.html").read_text(encoding="utf-8")
    assert page.count("error_forget_busy:") == 2 and "t('error_forget_busy')" in page
    appmod = importlib.import_module("app")

    class Busy:
        turn_id, retry_after = "background-job:1", 299
    with appmod.app.test_request_context():
        response = appmod._conversation_busy_response(Busy(), forgotten=False, status="busy")
    assert response.status_code == 409 and response.headers["Retry-After"] == "3"
