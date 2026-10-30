"""Regression tests (W-xx, D-xx ids in each test name)."""
from __future__ import annotations

import pytest


def _state_node(accumulated: dict, message: str, history=None) -> dict:
    from core.langgraph_agent import _make_extract_preferences_node
    state = {"user_preferences": {}, "accumulated_search_criteria": dict(accumulated),
             "extracted_context": {"current_message": message,
                                   "history": history or [{"user": "Find a flat", "assistant": "ok"}]}}
    update = _make_extract_preferences_node()(state)
    return update.get("accumulated_search_criteria", state["accumulated_search_criteria"])


_CAMDEN = {"area": "Camden", "areas": ["Camden"], "bedrooms": 1, "max_budget": 2500, "room_type": "whole"}


# W-01 ── a question or a statement about a requirement stores nothing
@pytest.mark.parametrize("message", [
    "What is my budget now, and do I need a balcony?", "Must I have a garden?", "Do I need a balcony?",
    "The balcony requirement is gone", "The garden requirement no longer applies",
    "I removed the balcony requirement earlier. Find a 1-bed flat in Camden under £2,000, no commute.",
    "I never asked for a balcony", "I didn't ask for a balcony", "I don't have a balcony requirement",
    "What happened to the balcony requirement?", "What's the balcony requirement?",
    "Should I have a balcony?", "I need a garden. Do I need a balcony?", "我还需要阳台吗？",
])
def test_w_01_a_mention_or_a_question_stores_no_balcony(message):
    soft = _state_node(_CAMDEN, message).get("soft_preferences") or []
    assert not [value for value in soft if "balcony" in value or ("garden" in value and "need a garden." not in message)]


@pytest.mark.parametrize("message,value", [
    ("I need a balcony", "Must have balcony"), ("Add a balcony requirement", "Must have balcony"),
    ("The balcony is a must", "Must have balcony"), ("Actually I need a balcony and a garden", "Must have garden"),
    ("Can you find me a flat with a balcony?", "Would like balcony"),
    ("Could you find a flat with a garden?", "Would like garden"),
    ("What flats have a balcony?", "Would like balcony"),
    ("I need a balcony and do I need a garden?", "Must have balcony"),
])
def test_w_01_a_stated_need_still_records(message, value):
    assert value in (_state_node(_CAMDEN, message).get("soft_preferences") or [])


def test_w_01_the_second_half_of_a_compound_stays_a_question():
    assert "Must have garden" not in _state_node(_CAMDEN, "I need a balcony and do I need a garden?")["soft_preferences"]


@pytest.mark.parametrize("message", [
    "The balcony requirement is gone", "I removed the balcony requirement earlier.", "I never asked for a balcony",
])
def test_w_01_a_statement_that_it_is_gone_takes_it_back(message):
    stored = dict(_CAMDEN, soft_preferences=["Must have balcony"])
    assert _state_node(stored, message)["soft_preferences"] == []


@pytest.mark.parametrize("message", [
    "Did I remove the balcony requirement?", "Have you removed the balcony requirement?", "Is the balcony requirement gone?",
])
def test_w_01_a_question_withdraws_nothing(message):
    stored = dict(_CAMDEN, soft_preferences=["Must have balcony"])
    assert _state_node(stored, message)["soft_preferences"] == ["Must have balcony"]


@pytest.mark.parametrize("message", ["Do I need a lift?", "What's my budget, and do I need a step-free flat?"])
def test_w_01_an_accessibility_question_adds_no_filter(message):
    assert not _state_node(_CAMDEN, message).get("property_features")


def test_w_01_a_lift_filter_survives_a_question_and_leaves_on_a_statement():
    stored = dict(_CAMDEN, property_features=["lift"])
    assert _state_node(stored, "Did I remove the lift requirement?")["property_features"] == ["lift"]
    assert _state_node(stored, "The lift requirement is gone")["property_features"] == []


def test_w_01_a_search_phrased_as_a_question_keeps_its_filter():
    assert "step-free" in _state_node(_CAMDEN, "Do you have any step-free flats in Camden?")["property_features"]


def test_w_01_a_withdrawn_amenity_leaves_the_preference_labels():
    from core.langgraph_agent import extract_preferences_from_message
    prefs = {"hard_preferences": ["Must have balcony"], "soft_preferences": ["Would like gym"],
             "required_amenities": ["balcony"]}
    out = extract_preferences_from_message("Remove the balcony requirement", prefs)
    assert out["hard_preferences"] == [] and out["required_amenities"] == []
    assert out["soft_preferences"] == ["Would like gym"]


# W-02 ── a zh withdrawal reaches the requirement it names, in either word order
_ZH_WITHDRAWALS = ["算了，不要{}要求了", "不要{}要求了", "不要{}了", "{}要求不要了", "不需要{}了", "{}要求算了",
                   "去掉{}要求", "{}不用了", "取消{}要求", "{}算了", "不用{}了", "{}这个要求不要了"]


@pytest.mark.parametrize("form", _ZH_WITHDRAWALS)
def test_w_02_pets_are_withdrawn(form):
    stored = dict(_CAMDEN, property_features=["pets allowed"], soft_preferences=["pets allowed"])
    out = _state_node(stored, form.format("宠物"))
    assert not out.get("property_features") and not out.get("soft_preferences")


@pytest.mark.parametrize("form", _ZH_WITHDRAWALS)
@pytest.mark.parametrize("word,left", [("家具", ["lift"]), ("电梯", ["furnished"])])
def test_w_02_a_filter_feature_is_withdrawn(form, word, left):
    stored = dict(_CAMDEN, property_features=["furnished", "lift"])
    assert _state_node(stored, form.format(word))["property_features"] == left


@pytest.mark.parametrize("form", _ZH_WITHDRAWALS)
@pytest.mark.parametrize("word,left", [("阳台", ["Would like garden"]), ("花园", ["Must have balcony"])])
def test_w_02_an_amenity_is_withdrawn(form, word, left):
    stored = dict(_CAMDEN, soft_preferences=["Must have balcony", "Would like garden"])
    assert _state_node(stored, form.format(word))["soft_preferences"] == left


@pytest.mark.parametrize("message", ["算了，不用阳台了", "不要阳台了", "我有猫，不要狗", "不要宠物", "不要宠物的房子",
                                     "宠物要求取消了吗？", "不要宠物了吗"])
def test_w_02_another_feature_or_a_question_leaves_pets_alone(message):
    stored = dict(_CAMDEN, property_features=["pets allowed"], soft_preferences=["pets allowed"])
    out = _state_node(stored, message)
    assert out["property_features"] == ["pets allowed"] and out["soft_preferences"] == ["pets allowed"]


@pytest.mark.parametrize("message", ["阳台要求取消了吗？", "不要阳台了吗"])
def test_w_02_a_question_keeps_an_amenity(message):
    stored = dict(_CAMDEN, soft_preferences=["Must have balcony"])
    assert _state_node(stored, message)["soft_preferences"] == ["Must have balcony"]


def test_w_02_a_filter_is_not_withdrawn_by_a_question():
    stored = dict(_CAMDEN, property_features=["furnished", "lift"])
    assert _state_node(stored, "电梯要求取消了吗？")["property_features"] == ["furnished", "lift"]
    assert _state_node(stored, "家具要求取消了吗？")["property_features"] == ["furnished", "lift"]


# W-03 ── a workplace change moves the destination and leaves the area
def _search_params(message: str, accumulated: dict, **model) -> dict:
    """What the executor sends to the tool after this turn's state node."""
    import asyncio

    from langchain_core.messages import AIMessage

    from core.agent_loop import build_fc_nodes
    from tests.test_agent_contract_guards import _state, _tc
    from tests.test_qa_null_injection import _ValidatingProvider, _strict_null_args
    state = _state(message)
    state["accumulated_search_criteria"] = _state_node(accumulated, message)
    args = _strict_null_args("search_properties", {"user_query": message, **model})
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_properties", args, "c0")])]
    provider = _ValidatingProvider(["search_properties"])
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    (_, params), = provider.calls
    return params


_ISLINGTON = {"area": "Islington", "areas": ["Islington"], "bedrooms": 1, "max_budget": 2200, "room_type": "whole",
              "commute_destination": "Bank", "destination": "Bank", "max_travel_time": 40}


@pytest.mark.parametrize("message,destination", [
    ("Actually make it Canary Wharf as the workplace instead", "Canary Wharf, London E14 5AB"),
    ("Actually make it King's Cross as the workplace instead", "King's Cross"),
    ("Make Stratford my workplace", "Stratford"), ("Stratford is now my office", "Stratford"),
    ("Use Stratford as my office", "Stratford"), ("Change the commute destination to King's Cross", "King's Cross"),
    ("Change my commute destination to Stratford", "Stratford"), ("Set the destination to Stratford", "Stratford"),
    ("Commute is now to Stratford", "Stratford"), ("My commute is now to Canary Wharf", "Canary Wharf, London E14 5AB"),
    ("Commute destination is now Stratford", "Stratford"), ("Commute destination: Stratford", "Stratford"),
    ("Change my commute from Bank to Stratford", "Stratford"), ("my workplace is now Stratford", "Stratford"),
    ("Switch my commute to King's Cross instead of Bank", "King's Cross"), ("Change my workplace to Stratford", "Stratford"),
    ("通勤目的地改成Stratford", "Stratford"), ("把通勤地点换成金丝雀码头", "Canary Wharf, London E14 5AB"),
    ("把上班地点改成Stratford", "Stratford"),
])
def test_w_03_the_workplace_moves_and_the_area_stays(message, destination):
    out = _state_node(_ISLINGTON, message)
    assert out["area"] == "Islington" and out["areas"] == ["Islington"]
    assert out["commute_destination"] == destination and out["destination"] == destination


@pytest.mark.parametrize("message", ["What is my commute destination?", "Where is my workplace?", "通勤改成30分钟"])
def test_w_03_a_question_or_a_limit_moves_nothing(message):
    assert _state_node(_ISLINGTON, message)["commute_destination"] == "Bank"


@pytest.mark.parametrize("message", ["Make it Stratford", "Make it Stratford instead", "I want a flat near Stratford"])
def test_w_03_an_area_switch_stays_one(message):
    out = _state_node(_ISLINGTON, message)
    assert out["area"] == "Stratford" and out["commute_destination"] == "Bank"


@pytest.mark.parametrize("message,destination", [
    ("Actually make it Canary Wharf as the workplace instead", "Canary Wharf, London E14 5AB"),
    ("Commute to Stratford instead", "Stratford"), ("把上班地点改成Stratford", "Stratford"),
    ("把通勤地点换成金丝雀码头", "Canary Wharf, London E14 5AB"),
])
def test_w_03_the_search_runs_at_the_old_area_with_the_new_destination(message, destination):
    params = _search_params(message, _ISLINGTON, commute_destination="Bank")
    assert params["area"] == "Islington" and params["commute_destination"] == destination


# W-04 ── a place the user names beats the stale area
@pytest.mark.parametrize("message,area", [
    ("Find a 2 bed in Inverness", "Inverness"), ("Show me flats in Reading under £1,500", "Reading"),
    ("Find a flat in Bath", "Bath"), ("find me a place in inverness", "Inverness"),
    ("Find a 3-bed house in Inverness under £1,200 near a school, and tell me safety", "Inverness"),
    ("Find somewhere in Knightsbridge", "Knightsbridge"), ("Find a flat in Leeds", "Leeds"),
])
def test_w_04_a_named_place_wins(message, area):
    out = _state_node(_CAMDEN, message)
    assert out["area"] == area and out["areas"] == [area]


@pytest.mark.parametrize("message", [
    "flats in reading", "Is there a bath in the flat?", "Find a flat with a bath", "Find a flat in Atlantis",
    "I am reading about flats", "What if I found a flat in Reading?",
])
def test_w_04_a_word_or_an_unknown_place_changes_nothing(message):
    assert _state_node(_CAMDEN, message)["area"] == "Camden"


def test_w_04_the_tool_and_the_state_read_the_same_place():
    from core.tools.search_properties import _extract_area, _extract_areas
    assert _extract_area("Find a 2 bed in Inverness") == "Inverness"
    assert _extract_areas("Show me flats in Reading under £1,500") == ["Reading"]


_MANCHESTER = {"area": "Manchester", "areas": ["Manchester"], "bedrooms": 2, "max_budget": 1400,
               "no_commute": True, "room_type": "whole"}


def test_w_04_near_ucl_with_the_commute_disowned():
    out = _state_node(_MANCHESTER, "Find a flat near UCL for a student, 1 bed, under 1800, commute to UCL not needed")
    assert out["area"] == "Bloomsbury" and out["areas"] == ["Bloomsbury"]
    assert out["no_commute"] is True and not out.get("commute_destination") and not out.get("destination")
    assert out["bedrooms"] == 1 and out["max_budget"] == 1800


@pytest.mark.parametrize("message,area", [
    ("I want to live near Imperial College", "South Kensington"),
    ("Find me a studio near King's College London", "Holborn"),
    ("Find a flat near the University of Manchester", "Manchester"),
])
def test_w_04_a_home_near_a_university_moves_the_search(message, area):
    assert _state_node(dict(_CAMDEN, area="Leeds", areas=["Leeds"]), message)["area"] == area


@pytest.mark.parametrize("message", ["I study at UCL", "Is the flat near UCL?"])
def test_w_04_a_university_named_otherwise_moves_nothing(message):
    assert _state_node(_CAMDEN, message)["area"] == "Camden"


def test_w_04_an_area_named_with_the_university_wins():
    assert _state_node(_CAMDEN, "Find a flat near UCL in Hackney")["area"] == "Hackney"


@pytest.mark.parametrize("message", [
    "commute to King's Cross not needed", "commute to King's Cross is not needed",
    "I don't need to commute to King's Cross", "no commute to King's Cross please",
])
def test_w_04_a_disowned_commute_is_no_destination(message):
    stored = dict(_CAMDEN, commute_destination="King's Cross", destination="King's Cross", max_travel_time=30)
    out = _state_node(stored, message)
    assert out["no_commute"] is True and not out.get("commute_destination") and out.get("max_travel_time") is None


def test_w_04_the_model_cannot_bring_a_disowned_destination_back():
    message = "Find a flat near UCL, 1 bed, under 1800, commute to UCL not needed"
    params = _search_params(message, _MANCHESTER, commute_destination="UCL")
    assert params["area"] == "Bloomsbury" and params.get("no_commute") is True
    assert "commute_destination" not in params


@pytest.mark.parametrize("message", ["My commute to Bank is 30 minutes", "commute to Bank needed"])
def test_w_04_the_destination_ends_at_the_verb(message):
    assert _state_node(_CAMDEN, message)["commute_destination"] == "Bank"


# W-05 ── a reference to an earlier budget resolves from this conversation, or says it cannot
_HISTORY = {"max_budget": [2000, 2500]}
_MOVED = dict(_CAMDEN, constraint_history=_HISTORY)


@pytest.mark.parametrize("message", [
    "Budget back to what it was when I started", "把预算恢复成最开始的数字", "Go back to my original budget",
    "Back to the budget I started with", "Use the original budget", "Restore my initial budget please",
    "预算改回最开始的", "预算回到一开始的", "用最初的预算", "Set the budget to what it was at the start",
])
def test_w_05_the_first_value_comes_back(message):
    assert _state_node(_MOVED, message)["max_budget"] == 2000


@pytest.mark.parametrize("message", [
    "Go back to the previous budget", "back to my old budget", "Change the budget back to what it was before",
    "预算恢复到之前的", "把预算改回原来的", "用之前的预算",
])
def test_w_05_the_previous_value_comes_back(message):
    stored = dict(_CAMDEN, max_budget=2200, constraint_history={"max_budget": [2000, 2500, 2200]})
    assert _state_node(stored, message)["max_budget"] == 2500


@pytest.mark.parametrize("message", [
    "What was my original budget?", "What did I say my budget was originally?", "My original budget was too low",
    "最开始的预算是多少？", "Did I go back to the original budget?",
])
def test_w_05_a_question_changes_nothing(message):
    assert _state_node(_MOVED, message)["max_budget"] == 2500


def test_w_05_a_figure_in_the_same_message_wins():
    assert _state_node(_MOVED, "Go back to my original budget, make it £2,300")["max_budget"] == 2300


def test_w_05_later_relative_edits_build_on_the_restored_value():
    out = _state_node(_state_node(_MOVED, "Budget back to what it was when I started"), "Raise the budget by £100")
    assert out["max_budget"] == 2100


def test_w_05_it_can_be_undone_and_is_recorded():
    out = _state_node(_MOVED, "Budget back to what it was when I started")
    assert out["constraint_history"]["max_budget"] == [2000, 2500, 2000]
    assert _state_node(out, "Undo that last change")["max_budget"] == 2500


def test_w_05_the_previous_budget_after_another_change_is_read_from_the_history():
    stored = dict(_CAMDEN, constraint_history=_HISTORY, _last_change={"before": {"area": "Hackney", "max_budget": 2500}})
    assert _state_node(stored, "Go back to the previous budget")["max_budget"] == 2000


def test_w_05_the_last_change_still_answers_without_a_history():
    stored = dict(_CAMDEN, _last_change={"before": {"max_budget": 1800}})
    assert _state_node(stored, "Go back to the previous budget")["max_budget"] == 1800


@pytest.mark.parametrize("message,language,expected", [
    ("Budget back to what it was when I started", "en", "I don't have an earlier budget"),
    ("把预算恢复成最开始的数字", "zh", "没有更早的预算记录"),
])
def test_w_05_an_unresolved_reference_says_so(message, language, expected):
    from core.constraint_updates import budget_reference_note
    out = _state_node(_CAMDEN, message)
    assert out["max_budget"] == 2500
    note = budget_reference_note(message, out, language)
    assert expected in note and "2,500" in note


def test_w_05_a_resolved_reference_needs_no_note():
    from core.constraint_updates import budget_reference_note
    message = "Budget back to what it was when I started"
    assert budget_reference_note(message, _state_node(_MOVED, message)) == ""
    assert budget_reference_note("Find a flat in Camden", _CAMDEN) == ""


def test_w_05_the_reply_carries_the_note():
    from core.agent_loop import build_fc_nodes
    from tests.test_agent_contract_guards import _state
    from tests.test_qa_null_injection import _ValidatingProvider
    message = "Budget back to what it was when I started"
    state = _state(message)
    state["accumulated_search_criteria"] = _state_node(_CAMDEN, message)
    state["final_response"] = "Your criteria are unchanged."
    out = build_fc_nodes(_ValidatingProvider([]))["format_output_fc"](state)
    assert out["final_response"].endswith("so it stays at £2,500.")


def test_w_05_a_restored_budget_reaches_the_search_over_the_models_stale_value():
    params = _search_params("Budget back to what it was when I started, find a flat in Camden",
                            _MOVED, max_budget=2500)
    assert params["max_budget"] == 2000


# D-01, D-02 ── a recall question gets every saved fact, and a refused question is answered
@pytest.mark.parametrize("message", [
    "What did I tell you about my pets?", "Remember what I told you about my pets?",
    "Remember my budget and what pets I have?", "Remember my budget?", "我之前说过什么？",
])
def test_d_01_a_recall_question_reads_every_saved_fact(message):
    import importlib
    appmod = importlib.import_module("app")
    assert appmod._asks_what_is_remembered(message)


@pytest.mark.parametrize("message", [
    "Remember my budget is £2,000", "Remember my budget is £2,000?", "Remember my cat is called Bob?",
    "Please remember I have a cat", "Remember my budget and my cat", "Find a flat in Camden",
])
def test_d_02_a_save_stays_a_save(message):
    from core.memory_gate import is_pure_recall_question
    assert not is_pure_recall_question(message)


@pytest.mark.parametrize("message", ["Remember my budget and what pets I have?", "Remember my budget?"])
def test_d_02_the_write_is_not_authorised(message):
    from core.memory_gate import is_pure_recall_question, user_authorizes_memory
    assert is_pure_recall_question(message) and not user_authorizes_memory(message)


def test_d_02_a_refused_question_adds_no_notice():
    from core.memory_contract import compose_memory_contract_response, memory_contract_from_artifacts
    artifact = {"tool": "remember", "success": True, "raw_data": {
        "success": False, "security_decision": "rejected_question_not_fact", "error": "That was a question."}}
    contract = memory_contract_from_artifacts([artifact])
    assert contract == {}
    assert compose_memory_contract_response("Your budget is £2,000 and you have a cat.", contract) == (
        "Your budget is £2,000 and you have a cat.")


def test_d_02_a_refused_instruction_keeps_its_notice():
    from core.memory_contract import memory_contract_from_artifacts, memory_notice
    artifact = {"tool": "remember", "success": True, "raw_data": {
        "success": False, "security_decision": "rejected_instruction_shaped", "error": "no"}}
    assert "not instructions" in memory_notice(memory_contract_from_artifacts([artifact]), "en")
