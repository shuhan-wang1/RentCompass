"""Regression tests (U-xx ids in each test name)."""
from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
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
           "room_type": "whole", "max_travel_time": 30}


class _CapturingGraph:
    def __init__(self, reply):
        self.reply, self.inputs = reply, []

    async def ainvoke(self, graph_input, config=None):
        self.inputs.append(graph_input)
        return {"final_response": self.reply, "response_type": "answer", "tool_data": {}}


def _chat_turn(monkeypatch, memory, message, reply, language="en"):
    am_mod = importlib.import_module("rag.agent_memory")
    appmod = importlib.import_module("app")
    monkeypatch.setattr(am_mod, "get_agent_memory", lambda *a, **k: memory)
    graph = _CapturingGraph(reply)
    setattr(graph, appmod._GRAPH_RUNNER_ATTR, appmod._graph_loop_runner)
    monkeypatch.setattr(appmod, "agent_graph", graph)
    tag = uuid.uuid4().hex[:10]
    return asyncio.run(appmod.handle_with_react_agent(
        message, {}, False, user_id=memory.test_user, conversation_id="c-" + tag,
        request_id="req-" + tag, ui_language=language)), graph


@pytest.fixture
def memory(tmp_path, monkeypatch):
    am_mod = importlib.import_module("rag.agent_memory")
    monkeypatch.setattr(am_mod, "call_ollama", lambda *a, **k: "5")
    store = am_mod.AgentMemory(db_path=str(tmp_path / "mem"))
    store.test_user = "u-r6-" + uuid.uuid4().hex[:10]
    return store


def _documents(store) -> list:
    return store.col.get(where={"user_id": store.test_user})["documents"]


# U-10b ── a figure touching CJK text is a value, and an ack never claims a deletion it did not do
@pytest.mark.parametrize("row", [
    "我的预算是1800镑", "我的预算是每月1800英镑", "预算1800", "我的预算1,800元", "我的预算2k",
    "我的预算是两千块", "我的预算是一千八百英镑", "My budget is 1800pcm", "My budget is two thousand pounds",
])
def test_u_10b_a_figure_touching_cjk_or_a_unit_is_erased(memory, row):
    memory.add(row, "semantic", user_id=memory.test_user, importance=5)
    report = memory.forget_fact(memory.test_user, ("budget",))
    assert report["deleted"] == 1 and report["complete"] and not _documents(memory)


def test_u_10b_the_other_facts_of_a_zh_note_survive(memory):
    memory.add("我的预算是每月1800英镑，我对猫过敏，我在国王十字车站附近上班", "semantic",
               user_id=memory.test_user, importance=5)
    report = memory.forget_fact(memory.test_user, ("budget",))
    assert report["rewritten"] == 1 and report["deleted"] == 0
    assert _documents(memory) == ["我对猫过敏，我在国王十字车站附近上班。"]


@pytest.mark.parametrize("sentence,kept", [
    ("My budget is £1,800, cats are fine.", "cats are fine."),
    ("我的预算是1800，我对猫过敏", "我对猫过敏。"),
])
def test_u_10b_a_clause_comma_after_a_figure_still_splits(sentence, kept):
    from rag.agent_memory import _without_field_clauses
    assert _without_field_clauses(sentence, "budget") == kept


@pytest.mark.parametrize("text", ["2025年", "300米", "2 bedrooms", "30分钟", "三百米", "千万别", "E14 5AB"])
def test_u_10b_a_year_a_distance_or_a_count_is_no_money_figure(text):
    from rag.agent_memory import _MONEY_RE
    assert _MONEY_RE.search(text) is None


def test_u_10b_a_zh_note_keeps_a_date_beside_the_budget():
    from rag.agent_memory import _without_field_clauses
    assert "2025年" in _without_field_clauses("我的预算是1800镑，我2025年9月搬家", "budget")


def test_u_10b_nothing_removed_is_said_plainly():
    from core import memory_gate
    report = {"fields": ("budget",), "complete": True, "deleted": 0, "rewritten": 0}
    note, ack = memory_gate.memory_erasure_texts(report, "zh")
    assert "删除" in ack and "已从我的记忆中删除" not in ack and "ALREADY" not in note
    reply = memory_gate.acknowledge_memory_erasure("好的。", report, "en")
    assert reply.startswith("I couldn't find a saved budget figure to delete")
    assert memory_gate.acknowledge_memory_erasure(reply, report, "en") == reply


def test_u_10b_a_turn_with_nothing_on_file_does_not_say_it_deleted(memory, monkeypatch):
    memory.add("我的预算很紧", "semantic", user_id=memory.test_user, importance=5)
    payload, graph = _chat_turn(monkeypatch, memory, "删除我的预算", "好的，已从记忆中删除你的预算。", "zh")
    message = payload["message"]
    assert "没有删除任何内容" in message and "已从记忆中删除" not in message
    seen = graph.inputs[0].get("memory_context") or graph.inputs[0].get("user_query") or ""
    assert "nothing was deleted" in seen


def test_u_10b_a_zh_budget_is_really_erased_in_a_turn(memory, monkeypatch):
    memory.add("我的预算是1800镑", "semantic", user_id=memory.test_user, importance=5)
    payload, _graph = _chat_turn(monkeypatch, memory, "删除我的预算", "好的。", "zh")
    assert payload["message"].startswith("已从我的记忆中删除你保存的预算")
    assert not any("1800" in doc for doc in _documents(memory))


@pytest.mark.parametrize("message", ["删除我的预算", "忘掉我的预算", "把预算删了", "forget my budget"])
def test_u_10b_the_ceiling_leaves_the_search_too(message):
    assert _state_node(_CAMDEN, message)["max_budget"] is None


@pytest.mark.parametrize("message", ["把预算改成2000，并删掉猫过敏这条", "别忘记我的预算", "删除猫过敏，保留预算"])
def test_u_10b_a_message_that_erases_no_budget_keeps_the_ceiling(message):
    assert _state_node(_CAMDEN, message)["max_budget"] is not None


# U-10a ── a zh cue governs the field in its own clause; a request we cannot do is said plainly
@pytest.mark.parametrize("message", [
    "删除我的预算", "忘掉预算", "忘掉我的预算", "把预算删了", "我的预算请删掉", "预算信息帮我删除一下",
    "忘记我保存的预算", "删除我之前说的预算1800镑", "请删除我的预算和租金", "我的预算不要记了",
    "Delete my预算", "delete the budget from your memory", "Forget my budget and delete my cat allergy",
])
def test_u_10a_a_budget_delete_is_still_recognised(message):
    from rag.agent_memory import erasure_request_fields
    assert erasure_request_fields(message) == ("budget",)


@pytest.mark.parametrize("message", [
    "好的，请把已保存的预算更新为2000英镑，并删掉我对猫过敏这条信息。",
    "把预算改成2000并删掉猫过敏这条",
    "删掉猫过敏这条，预算更新为2000",
    "删除猫过敏，保留预算",
    "帮我删掉租金太高的房源",
    "删掉那套房子的价格",
    "别忘记我的预算",
    "不要删除我的预算",
    "Don't forget my budget is £2,000",
    "Please don't delete my budget",
    "Update my saved budget to £2,000 and delete the cat allergy from your memory.",
    "Delete the cat and update my budget to 2000",
])
def test_u_10a_a_cue_that_does_not_govern_the_budget_erases_nothing(message):
    from rag.agent_memory import erasure_request_fields
    assert erasure_request_fields(message) == ()


def test_u_10a_an_update_with_a_delete_elsewhere_leaves_the_budget(memory, monkeypatch):
    memory.add("我的预算是1800镑", "semantic", user_id=memory.test_user, importance=5)
    message = "好的，请把已保存的预算更新为2000英镑，并删掉我对猫过敏这条信息。"
    payload, _graph = _chat_turn(monkeypatch, memory, message, "好的，已删除这条信息。", "zh")
    assert any("1800" in doc for doc in _documents(memory))
    assert "已删除这条" not in payload["message"] and "没有删除" in payload["message"]


@pytest.mark.parametrize("message", [
    "Update my saved budget to £2,000 and delete the cat allergy from your memory.",
    "delete the cat allergy from your memory", "Forget that I'm allergic to cats",
    "Please delete my workplace", "forget my address",
    "好的，请把已保存的预算更新为2000英镑，并删掉我对猫过敏这条信息。", "忘掉我对猫过敏", "删除我的工作地点",
    "Forget my budget and delete my cat allergy",
])
def test_u_10a_a_non_field_delete_is_recognised(message):
    from rag.agent_memory import erasure_request_names_other_memory
    assert erasure_request_names_other_memory(message)


@pytest.mark.parametrize("message", [
    "forget my budget", "删除我的预算", "forget the garden", "remove my area filter",
    "Forget that requirement.", "forget it", "Forget Camden", "remove the garden requirement",
    "delete that listing", "Don't forget that I'm allergic to cats", "别忘记我对猫过敏", "删掉这个条件",
    "Forget that I need a garden", "clear my filters", "forget that I said Camden",
])
def test_u_10a_a_search_retraction_is_no_memory_delete(message):
    from rag.agent_memory import erasure_request_names_other_memory
    assert not erasure_request_names_other_memory(message)


def test_u_10a_asking_not_to_keep_something_is_no_delete():
    from rag.agent_memory import erasure_request_names_other_memory
    assert not erasure_request_names_other_memory("不要记我的名字")


def test_u_10a_a_delete_we_cannot_do_gets_the_fixed_line():
    from core import memory_gate
    reply = memory_gate.settle_memory_deletion_claims(
        "I've saved that to memory.", "Update my saved budget to £2,000 and delete the cat allergy "
        "from your memory.", None, "en")
    assert reply.startswith("I've saved that to memory.") and memory_gate.MEMORY_SINGLE_ERASE_EN in reply
    assert memory_gate.settle_memory_deletion_claims(reply, "delete my workplace", None, "en") == reply


@pytest.mark.parametrize("reply", [
    "I've removed the cat allergy from memory.", "Your cat allergy has been deleted from my memory.",
    "Done, I have forgotten that.", "It's no longer stored.",
])
def test_u_10a_a_deletion_claim_without_an_erasure_does_not_stand(reply):
    from core import memory_gate
    out = memory_gate.settle_memory_deletion_claims(reply + " Anything else?", "forget it, thanks", None, "en")
    assert reply not in out and "Anything else?" in out and memory_gate.MEMORY_SINGLE_ERASE_EN in out


@pytest.mark.parametrize("reply", ["已从记忆中删除这条信息。", "我已经忘记了。", "已删除。"])
def test_u_10a_a_zh_deletion_claim_does_not_stand(reply):
    from core import memory_gate
    out = memory_gate.settle_memory_deletion_claims(reply, "删掉我对猫过敏这条信息", None, "zh")
    assert out == memory_gate.MEMORY_SINGLE_ERASE_ZH


@pytest.mark.parametrize("reply,message", [
    ("I've removed the garden requirement.", "forget the garden"),
    ("I've cleared your search criteria.", "Reset my criteria"),
    ("已移除花园要求。", "不要花园了"),
    ("Yes, your budget was deleted from memory earlier.", "Did you delete my budget?"),
    ("You had your budget deleted from memory, so I have none.", "What's my budget?"),
    ("I've deleted it from memory, as you asked.", "What do you remember about me?"),
])
def test_u_10a_a_true_statement_stays(reply, message):
    from core import memory_gate
    assert memory_gate.settle_memory_deletion_claims(reply, message, None, "en") == reply


def test_u_10a_a_real_erasure_is_never_called_undone():
    from core import memory_gate
    report = {"fields": ("budget",), "complete": True, "deleted": 1, "rewritten": 0}
    reply = "Done. It's no longer stored."
    assert memory_gate.settle_memory_deletion_claims(reply, "forget my budget", report, "en") == reply


def test_u_10a_an_erasure_that_found_nothing_keeps_its_own_acknowledgement():
    from core import memory_gate
    report = {"fields": ("budget",), "complete": True, "deleted": 0, "rewritten": 0}
    out = memory_gate.settle_memory_deletion_claims(
        "I couldn't find a saved budget figure to delete, so nothing was removed.\n\nI've deleted it from memory.",
        "forget my budget", report, "en")
    assert "I've deleted it" not in out and memory_gate.MEMORY_SINGLE_ERASE_EN not in out


def test_u_10a_a_licensed_claim_stands_beside_the_field_erasure():
    from core import memory_gate
    report = {"fields": ("budget",), "complete": True, "deleted": 1, "rewritten": 0}
    out = memory_gate.settle_memory_deletion_claims(
        "I've deleted your budget from memory.", "forget my budget", report, "en")
    assert out == "I've deleted your budget from memory."


# U-09 ── a zh home search near an institution moves the search; a distance question does not
@pytest.mark.parametrize("message,area", [
    ("牛津大学附近有房吗", "Oxford"), ("剑桥大学附近有房吗", "Cambridge"), ("曼彻斯特大学附近的房子", "Manchester"),
    ("爱丁堡大学附近有房吗", "Edinburgh"), ("利兹大学附近有房吗", "Leeds"), ("兰卡斯特大学附近有房吗", "Lancaster"),
    ("伦敦大学学院附近有房吗", "Bloomsbury"), ("伦敦大学学院附近", "Bloomsbury"),
    ("帝国理工附近有房吗", "South Kensington"), ("帮我在帝国理工学院附近找个一居室", "South Kensington"),
    ("国王学院附近租房", "Holborn"), ("伦敦国王学院附近的房子", "Holborn"), ("伦敦政经附近有房吗", "Holborn"),
    ("伦敦政治经济学院附近", "Holborn"), ("那牛津大学附近呢", "Oxford"), ("想住在剑桥大学附近", "Cambridge"),
])
def test_u_09_a_home_search_near_an_institution_moves_the_area(message, area):
    acc = _state_node({"area": "Bristol", "areas": ["Bristol"], "bedrooms": 1, "max_budget": 1200}, message)
    assert acc["area"] == area and acc["areas"] == [area]


@pytest.mark.parametrize("message", [
    "从第一个到伦敦大学学院要多久?", "去伦敦政治经济学院通勤多久", "到牛津大学多远", "我在牛津大学读书",
    "我在伦敦大学学院上学", "我在伦敦大学学院附近上班", "不要牛津大学附近的房子", "如果在牛津大学附近找房会更便宜吗",
    "帝国理工附近的房租贵吗", "帝国理工附近有地铁吗", "地铁站附近的房子", "公园附近有房吗", "我家附近有房吗",
    "这套附近有超市吗", "牛津街附近有房吗", "剑桥路附近的公寓",
])
def test_u_09_anything_else_keeps_the_area(message):
    acc = _state_node({"area": "Bristol", "areas": ["Bristol"], "bedrooms": 1, "max_budget": 1200}, message)
    assert acc["area"] == "Bristol" and acc["areas"] == ["Bristol"]


@pytest.mark.parametrize("message", ["温莎附近有房吗", "UCL附近有房吗", "Hogsmeade附近有房吗"])
def test_u_09_a_place_nothing_here_resolves_does_not_reuse_the_old_area(message):
    acc = _state_node({"area": "Bristol", "areas": ["Bristol"], "bedrooms": 1, "max_budget": 1200}, message)
    assert acc["area"] is None and acc["areas"] == []


@pytest.mark.parametrize("message", ["从这些房子到伦敦大学学院哪个最近？", "从这些房子到帝国理工哪个最近？"])
def test_u_09_comparing_listings_by_distance_sets_no_destination(message):
    assert not _state_node({}, message).get("commute_destination")


def test_u_09_the_institution_stays_the_destination_of_a_london_search():
    acc = _state_node({"area": "Bristol", "areas": ["Bristol"], "commute_destination": "University of Bristol"},
                      "伦敦大学学院附近有房吗")
    assert acc["area"] == "Bloomsbury" and "University College London" in acc["commute_destination"]


def test_u_09_the_model_cannot_confirm_the_gate_in_the_same_turn():
    from langchain_core.messages import AIMessage

    from core.agent_loop import build_fc_nodes
    from tests.test_agent_contract_guards import _state, _tc
    from tests.test_qa_null_injection import _ValidatingProvider, _strict_null_args
    message = "Find me a flat in Edinburgh"
    state = _state(message)
    state["accumulated_search_criteria"] = {"area": "Edinburgh", "areas": ["Edinburgh"],
                                            "criteria_gate_shown": True}
    state["tool_artifacts"] = [{"tool": "search_properties", "success": True, "raw_data": {
        "status": "need_clarification", "clarification_kind": "soft_criteria"}}]
    provider = _ValidatingProvider(["search_properties"])
    args = _strict_null_args("search_properties", {"user_query": message, "confirmed": True})
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_properties", args, "c0")])]
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    assert provider.calls == []     # V-03: the open gate is not dispatched again


# U-05 ── the notice appears once; "Please remember that." saves the user's own statements
_DENIED = {"requested": True, "attempted": False, "success": False,
           "error": "denied: tainted write requires confirmation"}


@pytest.mark.parametrize("contract", [
    _DENIED,
    {"requested": True, "attempted": False, "success": False},
    {"requested": True, "attempted": True, "success": False},
    {"requested": True, "attempted": True, "success": False, "policy_refused": True,
     "policy_decision": "rejected_secret"},
    {"requested": True, "attempted": True, "success": False, "policy_refused": True},
    {"requested": True, "attempted": True, "success": True, "stored_content": "I have a dog"},
])
@pytest.mark.parametrize("language", ["en", "zh"])
def test_u_05_composing_again_changes_nothing(contract, language):
    from core.memory_contract import compose_memory_contract_response as compose
    once = compose("I found 2 flats", contract, language=language, preserve_content=True)
    assert compose(once, contract, language=language, preserve_content=True) == once
    assert "I found 2 flats" in once


def test_u_05_a_notice_of_the_other_language_does_not_stay():
    from core.memory_contract import compose_memory_contract_response as compose
    english = compose("I found 2 flats", _DENIED, language="en", preserve_content=True)
    assert compose(english, _DENIED, language="zh", preserve_content=True).count("This was not") == 0


def test_u_05_the_withheld_note_appears_once_and_survives():
    from core.memory_contract import compose_memory_contract_response as compose
    from core.memory_contract import with_withheld_details_note as note
    contract = {"requested": True, "attempted": True, "success": True,
                "stored_content": "I prefer quiet streets"}
    message = "Please remember that I prefer quiet streets and that my phone number is 07700 900123."
    first = note(compose("OK", contract, language="en", preserve_content=True), contract, message, "en")
    assert first.count("I did not save the phone number or email") == 1
    assert compose(first, contract, language="en", preserve_content=True).count("I did not save") == 1
    assert compose(first, contract, language="en", preserve_content=False).count("I did not save") == 1


MEM1 = "Hi, I have a dog and I work at Canary Wharf. My budget is £2,200 a month for a 1-bed. Please remember that."


@pytest.mark.parametrize("message,clause", [
    (MEM1, "I have a dog and I work at Canary Wharf. My budget is £2,200 a month for a 1-bed"),
    ("My budget is £2,000. Remember that. Find me a flat in Hackney", "My budget is £2,000"),
    ("I have a dog and I work at Canary Wharf, please remember that", "I have a dog and I work at Canary Wharf"),
    ("我养了一只狗。我在金丝雀码头上班。请记住。", "我养了一只狗。我在金丝雀码头上班"),
    ("我的预算是2000镑，请记住这些。", "我的预算是2000镑"),
    ("Please remember that I have a cat.", "I have a cat"),
])
def test_u_05_the_clause_is_what_the_user_stated(message, clause):
    from core import memory_gate
    assert memory_gate.explicit_remember_clause(message) == clause
    assert memory_gate.write_authorization(message, clause)


@pytest.mark.parametrize("message", [
    "Find me a 1-bed in Hackney under £2,500. Remember that.",   # a request, not a fact
    "remember that", "Do you remember that?", "My password is S3cret!pass. I have a dog. Remember that.",
])
def test_u_05_nothing_stated_or_a_secret_gives_no_clause(message):
    from core import memory_gate
    assert memory_gate.explicit_remember_clause(message) == ""


def test_u_05_a_personal_detail_stays_out_and_the_note_says_so():
    from core import memory_gate
    from core.memory_contract import with_withheld_details_note
    message = "My name is Sam. I have a dog. Please remember that."
    assert memory_gate.explicit_remember_clause(message) == "I have a dog"
    contract = {"requested": True, "attempted": True, "success": True, "stored_content": "I have a dog"}
    assert "I did not save the name" in with_withheld_details_note("Saved.", contract, message, "en")


def _remember_turn(message: str, proposal: str):
    from langchain_core.messages import AIMessage

    from core import memory_gate
    from core.agent_loop import build_fc_nodes
    from tests.test_agent_contract_guards import _state, _tc
    from tests.test_qa_null_injection import _ValidatingProvider, _strict_null_args
    state = _state(message)
    state["session_id"] = "s-" + uuid.uuid4().hex   # a denied write freezes under the session
    provider = _ValidatingProvider(["search_properties", "remember"])
    provider.specs = [dataclasses.replace(spec, side_effect="write" if spec.name == "remember" else "none")
                      for spec in provider.specs]
    calls = [_tc("remember", _strict_null_args("remember", {"content": proposal}), "c0"),
             _tc("search_properties", _strict_null_args("search_properties", {"user_query": message}), "c1")]
    state["messages"] = [AIMessage(content="", tool_calls=calls)]
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    return dict(provider.calls).get("remember"), memory_gate


def test_u_05_the_geocoded_paraphrase_is_replaced_by_the_users_words():
    params, _gate = _remember_turn(MEM1, "User has a dog, works at Canary Wharf, London E14 5AB, and has a "
                                         "budget of £2,200 per month for a 1-bedroom whole flat.")
    assert params["content"] == "I have a dog and I work at Canary Wharf. My budget is £2,200 a month for a 1-bed"


def test_u_05_h13_tool_derived_content_still_meets_the_gate():
    # A fact of its own is named, so an unrelated proposal is not replaced by it.
    message = "Please remember that I have a dog, and find me the cheapest flat in Hackney"
    params, _gate = _remember_turn(message, "Cheapest flat is 14 Sample Road at £1,180 pcm.")
    assert params is None


def test_u_05_tool_derived_content_never_replaces_a_statement_free_message():
    message = "Find me the cheapest 1-bed in Hackney. Remember that."
    params, _gate = _remember_turn(message, "Cheapest flat is 14 Sample Road at £1,180 pcm.")
    assert params is None


# U-06 ── a reset clears the whole search; one field or a date is not a reset
_FULL = {"area": "Camden", "areas": ["Camden"], "bedrooms": 2, "max_budget": 2500, "room_type": "whole",
         "commute_destination": "King's Cross", "destination": "King's Cross", "max_travel_time": 30,
         "move_in_date": "2026-11-01", "property_features": ["balcony"], "soft_preferences": ["Would like garden"],
         "criteria_gate_shown": True,
         "constraint_sources": {"area": {"source": "user_message", "value": "Camden", "text": "1-bed in Camden"}}}


@pytest.mark.parametrize("message", [
    "Reset all my criteria and start from scratch.", "Clear my filters", "start again from scratch",
    "Reset my preferences", "重新开始", "清空条件", "Clear all my search criteria", "Start over", "Let's start again.",
    "Reset everything", "Can you clear my filters?", "Remove all my filters", "重置搜索条件", "清空所有条件", "从头开始吧",
    "Please reset the search", "清除筛选",
])
def test_u_06_a_reset_clears_every_slot(message):
    acc = _state_node(_FULL, message)
    assert acc["area"] is None and acc["areas"] == [] and acc["max_budget"] is None
    assert acc["bedrooms"] is None and acc["room_type"] is None and acc["move_in_date"] is None
    assert acc["commute_destination"] is None and acc["destination"] is None
    assert acc["max_travel_time"] is None and not acc["no_commute"]
    assert acc["property_features"] == [] and acc["soft_preferences"] == []
    assert acc["constraint_sources"] == {} and "criteria_gate_shown" not in acc


@pytest.mark.parametrize("message", [
    "clear my budget", "start the move in May", "reset the budget", "clear the commute filter", "reset the search area",
    "How do I reset my filters?", "Don't reset my filters", "不要重新开始", "我下个月重新开始上班", "Start again in May",
    "删除这个条件", "Can I start the tenancy over the summer?", "Forget everything you know about me",
])
def test_u_06_anything_else_is_not_a_reset(message):
    from core.constraint_updates import resets_all_criteria
    assert not resets_all_criteria(message)


def test_u_06_what_follows_the_reset_applies():
    acc = _state_node(_FULL, "Start again: 2-bed in Hackney under £1,800.")
    assert acc["area"] == "Hackney" and acc["bedrooms"] == 2 and acc["max_budget"] == 1800
    assert acc["commute_destination"] is None and acc["property_features"] == []


def test_u_06_the_preferences_panel_is_cleared_and_memory_is_not_read():
    from core.langgraph_agent import _make_extract_preferences_node
    state = {"user_preferences": {"hard_preferences": ["Must have lift"], "soft_preferences": []},
             "accumulated_search_criteria": dict(_FULL),
             "extracted_context": {"current_message": "Clear my filters", "history": []}}
    update = _make_extract_preferences_node()(state)
    assert update["user_preferences"]["hard_preferences"] == []


def test_u_06_the_model_cannot_put_the_old_area_back():
    from langchain_core.messages import AIMessage

    from core.agent_loop import build_fc_nodes
    from tests.test_agent_contract_guards import _state, _tc
    from tests.test_qa_null_injection import _ValidatingProvider, _strict_null_args
    message = "Reset all my criteria and start from scratch."
    state = _state(message)
    state["accumulated_search_criteria"] = _state_node(_FULL, message)
    provider = _ValidatingProvider(["search_properties"])
    args = _strict_null_args("search_properties", {"user_query": message, "area": "Camden", "max_budget": 2500})
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_properties", args, "c0")])]
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    (_, params), = provider.calls
    assert not params.get("area") and not params.get("max_budget")


def test_u_06_a_reset_shares_no_list_between_conversations():
    first = _state_node(_FULL, "Clear my filters")
    first["property_features"].append("lift")
    first["soft_preferences"].append("Would like garden")
    second = _state_node(_FULL, "Clear my filters")
    assert second["property_features"] == [] and second["soft_preferences"] == []


def test_u_06_undo_brings_the_search_back():
    reset = _state_node(_FULL, "Clear my filters")
    assert _state_node(reset, "Undo that last change")["area"] == "Camden"


# U-07 ── a body or field of the wrong type is a 400, never a 500
@pytest.fixture
def client(tmp_path, monkeypatch):
    appmod = importlib.import_module("app")
    from dataclasses import replace

    from uk_rent_agent.web.auth_store import AuthStore
    monkeypatch.setattr(appmod, "auth_store", AuthStore(str(tmp_path / "auth.sqlite3")))
    monkeypatch.setattr(appmod, "_runtime_config",
                        replace(appmod._runtime_config, allow_legacy_client_user_id=True))
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


@pytest.mark.parametrize("body", ["[1]", '"s"', "5", "true", "null"])
def test_u_07_generate_map_rejects_a_body_that_is_no_object(client, body):
    response = client.post("/api/generate_map", data=body, content_type="application/json")
    assert response.status_code == 400 and "error" in response.get_json()


def test_u_07_generate_map_still_needs_an_address(client):
    assert client.post("/api/generate_map", json={"price": "1"}).status_code == 400


@pytest.mark.parametrize("value", [5, 1.5, True, [1], {"a": 1}, [], {}, 0, False])
def test_u_07_register_rejects_a_display_name_that_is_no_string(client, value):
    response = client.post("/api/auth/register", json={
        "username": "name" + uuid.uuid4().hex[:8], "password": "a-long-enough-passphrase",
        "display_name": value})
    assert response.status_code == 400 and "display_name" in response.get_json()["error"]


@pytest.mark.parametrize("value", [None, "", "Sam"])
def test_u_07_register_still_takes_a_missing_or_string_display_name(client, value):
    response = client.post("/api/auth/register", json={
        "username": "name" + uuid.uuid4().hex[:8], "password": "a-long-enough-passphrase",
        "display_name": value})
    assert response.status_code == 200


@pytest.mark.parametrize("name,valid", [
    ("abc", True), ("a" * 32, True), ("abc\n", False), ("a" * 32 + "\n", False), ("ab", False),
    ("a" * 33, False), ("a b c", False), (5, False),
])
def test_u_07_a_username_is_matched_whole(name, valid):
    from uk_rent_agent.web.auth_store import valid_username
    assert valid_username(name) is valid


def test_u_07_register_rejects_a_trailing_newline(client):
    response = client.post("/api/auth/register", json={
        "username": "name\n", "password": "a-long-enough-passphrase"})
    assert response.status_code == 400


# U-08 ── a self-introduction is a name; everything else after "I'm" is not
@pytest.mark.parametrize("message,name", [
    ("Hi, I'm Oluwaseun Adebayo, looking for a flat", "Adebayo"), ("this is Mei Lin Zhao speaking", "Zhao"),
    ("I'm Sarah Jones", "Jones"), ("I am Tom Baker and I need a 1-bed", "Baker"),
    ("Sarah here, looking for flats", "Sarah"), ("我是王小明，想找房", "王小明"), ("我是欧阳娜娜，想租房", "娜娜"),
    ("Hi I'm Sarah looking for a 1-bed in Camden", "Sarah"), ("I'm José García.", "García"),
    ("I'm Anne-Marie O'Neill, from Leeds", "Neill"), ("I'm Sean McDonald", "McDonald"),
])
def test_u_08_a_self_introduction_is_not_stored(message, name):
    from rag.agent_memory import redact_sensitive
    text, found = redact_sensitive(message)
    assert name not in text and "identity" in found


def test_u_08_the_rest_of_the_clause_survives():
    from rag.agent_memory import redact_sensitive
    assert "1-bed in Camden" in redact_sensitive("Hi I'm Sarah looking for a 1-bed in Camden")[0]
    assert "想找房" in redact_sensitive("我是王小明，想找房")[0]


@pytest.mark.parametrize("message", [
    "I'm looking for a flat", "I'm a student at UCL", "I'm moving to Camden", "I'm in Camden", "I'm Polish",
    "I'm British", "I'm Chinese", "我是学生", "我是留学生", "我是中国人", "I'm Polish and need a flat", "I'm Muslim",
    "I'm Looking For A Flat", "I'm In Camden", "I am A Student", "I'm Single", "I'm Camden based", "I'm from Leeds",
    "I'm Erasmus student", "This is Camden", "This is what I want", "Anything here, please", "Nothing here",
    "我是白领", "我是上班族", "我是程序员", "我是一名学生", "我是第一次租房", "我是高中生", "我是江苏人", "我是想找房",
    "我是女生", "我是租客",
])
def test_u_08_anything_else_after_i_am_stays(message):
    from rag.agent_memory import redact_sensitive
    assert redact_sensitive(message) == (message, [])


@pytest.mark.parametrize("message", [
    "Remember I'm a student", "Please remember I'm vegan", "Remember that I'm Polish", "记住我是学生",
])
def test_u_08_a_save_request_about_oneself_keeps_its_behaviour(message):
    from core.memory_contract import with_withheld_details_note
    from rag.agent_memory import redact_sensitive
    assert redact_sensitive(message)[0] == message
    contract = {"requested": True, "attempted": True, "success": True, "stored_content": message}
    assert with_withheld_details_note("Saved.", contract, message, "en") == "Saved."


def test_u_08_a_name_in_a_save_request_waits_for_a_confirmation():
    from core.memory_contract import with_withheld_details_note
    from rag.agent_memory import redact_sensitive
    message = "Please remember I'm Sarah Jones, and that I prefer quiet streets."
    assert "Sarah" not in redact_sensitive(message)[0]
    contract = {"requested": True, "attempted": True, "success": True,
                "stored_content": "I prefer quiet streets"}
    assert "I did not save the name" in with_withheld_details_note("Saved.", contract, message, "en")


# U-13 ── every result of a pair evidences a claim about it; the fallback quotes the right one
def _journey(minutes, tool="get_transport_info", basis="now", summary="Tube Northern to Waterloo (12 min)"):
    legs = [{"mode": "tube", "duration_minutes": minutes, "line": "Northern", "to": "Waterloo"}]
    return {"tool": tool, "success": True, "args": {}, "raw_data": {
        "success": True, "query_type": "journey", "duration_minutes": minutes, "route_summary": summary,
        "route_legs": legs, "journey_time": {"time_basis": basis},
        "from": {"input": "Camden Town Underground Station",
                 "resolved_station": "Camden Town Underground Station", "naptan": "940GZZLUCTN"},
        "to": {"input": "Waterloo Station", "resolved_station": "Waterloo", "naptan": "HUBWAT"}}}


def _turn(*artifacts):
    message = "and from Camden Town to Waterloo?"
    return {"tool_artifacts": list(artifacts), "user_query": message,
            "extracted_context": {"current_message": message, "reply_language": "en"}}


@pytest.mark.parametrize("claim", [
    "Camden Town to Waterloo takes about 27 minutes on the Northern line, with one change.",
    "It's 27 minutes by Tube from Camden Town to Waterloo.",
    "Camden Town to Waterloo takes 32 minutes at night.",
    "Camden Town to Waterloo is under half an hour on a weekday morning.",
])
def test_u_13_any_result_of_the_pair_evidences_the_claim(claim):
    from core.candidate_validation import validate_commute_response
    state = _turn(_journey(32, summary="Bus N29 (15 min) -> Bus N3 (10 min)"), _journey(31), _journey(27, basis="user_requested"))
    assert validate_commute_response(claim, state) == claim


def test_u_13_a_figure_of_no_result_still_goes():
    from core.candidate_validation import validate_commute_response
    state = _turn(_journey(32), _journey(27, basis="user_requested"))
    claim = "Camden Town to Waterloo takes 19 minutes on the Northern line."
    assert "19 minutes" not in validate_commute_response(claim, state)


def test_u_13_an_unnamed_bound_still_has_to_hold_for_every_measurement():
    from core.candidate_validation import validate_commute_response
    state = _turn(_journey(32), _journey(27, basis="user_requested"))
    claim = "Either way, this is a short commute, comfortably under 30 minutes."
    assert "under 30 minutes" not in validate_commute_response(claim, state)


def test_u_13_the_fallback_prefers_the_time_the_user_asked_for():
    from core.candidate_validation import render_structured_evidence_fallback
    out = render_structured_evidence_fallback(
        _turn(_journey(32), _journey(31), _journey(27, basis="user_requested")), language="en")
    assert "27 minutes" in out and "32 minutes" not in out and "31 minutes" not in out


def test_u_13_the_fallback_otherwise_takes_the_latest_call():
    from core.candidate_validation import render_structured_evidence_fallback
    out = render_structured_evidence_fallback(_turn(_journey(32), _journey(31)), language="en")
    assert "31 minutes" in out and "32 minutes" not in out


def test_u_13_two_requested_times_both_stay():
    from core.candidate_validation import render_structured_evidence_fallback
    out = render_structured_evidence_fallback(
        _turn(_journey(32, basis="user_requested"), _journey(27, basis="user_requested")), language="en")
    assert "27 minutes" in out and "32 minutes" in out


# U-11 ── an appliance or a park is recorded, so the status lists it as not checked
@pytest.mark.parametrize("message,value", [
    ("Find a 1-bed in Camden, I need a washing machine", "Must have washing machine"),
    ("I need a dishwasher", "Must have dishwasher"), ("I'd like a dishwasher", "Would like dishwasher"),
    ("Find a 1-bed in Camden with a dishwasher, near a park", "Would like dishwasher"),
    ("Find a 1-bed in Camden with a dishwasher, near a park", "Would like park"),
    ("near a park please", "Would like park"), ("I need to be near a park", "Must have park"),
    ("with a washing machine", "Would like washing machine"), ("需要洗碗机", "Must have dishwasher"),
    ("想要洗碗机", "Would like dishwasher"), ("靠近公园", "Would like park"), ("I need a balcony", "Must have balcony"),
    ("帮我找有洗碗机的房子吗", "Would like dishwasher"),
])
def test_u_11_a_stated_need_or_wish_is_recorded(message, value):
    assert value in _state_node(_CAMDEN, message)["soft_preferences"]


@pytest.mark.parametrize("message", [
    "Find a 1-bed near Green Park", "a flat near Hyde Park", "Park Lane flat", "Park Royal", "near Finsbury Park please",
    "Is there a car park?", "no need for a dishwasher", "I don't need a washing machine",
    "does it have a dishwasher?", "Does the flat have a dishwasher?", "is there a washing machine?",
    "这套有洗碗机吗", "海德公园附近有房吗",
])
def test_u_11_a_place_a_question_or_a_refusal_records_nothing(message):
    soft = _state_node(_CAMDEN, message).get("soft_preferences") or []
    assert not [value for value in soft if "park" in value or "dishwasher" in value or "washing" in value]


def test_u_11_the_status_lists_them_as_not_checked():
    from core.candidate_validation import render_candidate_status, validate_candidates
    acc = _state_node({}, "Find a 1-bed in Camden under £2500, I need a dishwasher, near a park")
    rows = [{"address": "A St, Camden, London NW1", "area": "Camden", "price": "£2400/month",
             "bedrooms": 1, "room_type": "whole", "url": "https://example.test/a"}]
    text = render_candidate_status(validate_candidates(rows, acc))
    assert "Not checked for any listing (confirm with the agent):" in text
    assert "dishwasher" in text and "park" in text and "Must have" not in text


def test_u_11_a_need_is_no_search_filter():
    assert not _state_node(_CAMDEN, "I need a dishwasher").get("property_features")


def test_u_11_a_need_replaces_the_wish_and_can_be_withdrawn():
    acc = _state_node(_CAMDEN, "I'd like a dishwasher")
    acc = _state_node(acc, "Actually I need a dishwasher")
    assert acc["soft_preferences"] == ["Must have dishwasher"]
    assert _state_node(acc, "I don't need a dishwasher after all")["soft_preferences"] == []


def test_u_11_forget_that_requirement_takes_a_need_back():
    acc = _state_node(_CAMDEN, "I need a dishwasher")
    out = _state_node(acc, "Forget that requirement.", history=[{"user": "I need a dishwasher", "assistant": "ok"}])
    assert out["soft_preferences"] == []


# U-12 ── a curated London area is looked for in London first; any other name as before
class _FakeNominatim:
    """Answers like Nominatim: a bounded search finds the London place, an open one the first GB hit."""

    def __init__(self, london=True):
        self.calls, self.london = [], london

    def __call__(self, url, params=None, **kwargs):
        from types import SimpleNamespace
        self.calls.append(dict(params or {}))
        if (params or {}).get("bounded"):
            rows = [{"lat": "51.527", "lon": "-0.024", "display_name": "Bow, Tower Hamlets, London"}] if self.london else []
        else:
            rows = [{"lat": "50.80", "lon": "-3.82", "display_name": "Bow, Devon"}]
        return SimpleNamespace(status_code=200, json=lambda: rows)


@pytest.fixture
def geocoder(monkeypatch):
    from core import maps_service
    monkeypatch.setattr(maps_service, "_read_versioned_cache", lambda key, **kw: ("miss", None))
    monkeypatch.setattr(maps_service, "_write_versioned_cache", lambda *a, **kw: None)
    return maps_service


@pytest.mark.parametrize("address", ["Bow", "Bow, London", "Richmond, London", "Richmond", "Bow, Greater London, UK"])
def test_u_12_a_curated_london_area_is_looked_for_in_london(geocoder, monkeypatch, address):
    fake = _FakeNominatim()
    monkeypatch.setattr(geocoder.requests, "get", fake)
    result = geocoder._free_geocode(address)
    assert fake.calls[0]["bounded"] == 1 and fake.calls[0]["viewbox"] and len(fake.calls) == 1
    assert 51 < result["lat"] < 52


@pytest.mark.parametrize("address", ["Bow, Devon", "Richmond, North Yorkshire", "Exeter", "12 Sample Road, Bow"])
def test_u_12_any_other_name_behaves_as_before(geocoder, monkeypatch, address):
    fake = _FakeNominatim()
    monkeypatch.setattr(geocoder.requests, "get", fake)
    geocoder._free_geocode(address)
    assert len(fake.calls) == 1 and "bounded" not in fake.calls[0] and "viewbox" not in fake.calls[0]


def test_u_12_nothing_in_london_falls_back_to_the_open_search(geocoder, monkeypatch):
    fake = _FakeNominatim(london=False)
    monkeypatch.setattr(geocoder.requests, "get", fake)
    assert geocoder._free_geocode("Bow")["resolved_name"] == "Bow, Devon" and len(fake.calls) == 2


def test_u_12_a_cached_result_outside_london_is_not_reused(geocoder, monkeypatch):
    monkeypatch.setattr(geocoder, "_read_versioned_cache",
                        lambda key, **kw: ("fresh", {"lat": 50.80, "lng": -3.82, "resolved_name": "Bow, Devon"}))
    monkeypatch.setattr(geocoder.requests, "get", _FakeNominatim())
    assert "London" in geocoder._free_geocode("Bow")["resolved_name"]


def _open_meteo(monkeypatch, places):
    from types import SimpleNamespace

    from core.tools import get_weather as weather
    seen = []

    def fake(url, params=None, **kwargs):
        seen.append((url, dict(params or {})))
        if "geocoding" in url:
            return SimpleNamespace(status_code=200, json=lambda: {"results": places})
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {
            "current": {"temperature_2m": 11, "relative_humidity_2m": 70, "apparent_temperature": 10,
                        "precipitation": 0, "weather_code": 1, "wind_speed_10m": 9},
            "hourly": {"time": [], "uv_index": []}})
    monkeypatch.setattr(weather.requests, "get", fake)
    return weather, seen


_BOW_DEVON = {"name": "Bow", "latitude": 50.80, "longitude": -3.82, "country_code": "GB", "admin1": "England", "admin2": "Devon"}
_BOW_LONDON = {"name": "Bow", "latitude": 51.527, "longitude": -0.024, "country_code": "GB", "admin1": "England",
               "admin2": "Greater London"}


def test_u_12_get_weather_bow_is_the_london_one(monkeypatch):
    weather, seen = _open_meteo(monkeypatch, [_BOW_DEVON, _BOW_LONDON])
    weather.get_weather_impl("Bow")
    assert [params["latitude"] for url, params in seen if "forecast" in url] == [51.527]


def test_u_12_get_weather_of_another_town_takes_the_first_uk_place(monkeypatch):
    place = dict(_BOW_DEVON, name="Exeter", latitude=50.72, admin2="Devon")
    weather, seen = _open_meteo(monkeypatch, [place])
    weather.get_weather_impl("Exeter")
    assert [params["latitude"] for url, params in seen if "forecast" in url] == [50.72]


# T-04 ── a claim that nothing is listed, from a turn that searched nothing, says so
@pytest.mark.parametrize("reply", [
    "That's a sample of two, and I have no Clapham listings in the cache to set against them.",
    "I don't have rent figures for Hackney.", "There are no listings for Bow.", "Nothing is listed in Angel.",
])
def test_t_04_an_unsearched_absence_gets_the_note(reply):
    from core.agent_loop import UNSEARCHED_ABSENCE_NOTE_EN, disclose_unsearched_absence
    out = disclose_unsearched_absence(reply, [{"tool": "check_safety"}], "en")
    assert out.startswith(reply) and UNSEARCHED_ABSENCE_NOTE_EN in out
    assert disclose_unsearched_absence(out, [], "en") == out


def test_t_04_zh():
    from core.agent_loop import UNSEARCHED_ABSENCE_NOTE_ZH, disclose_unsearched_absence
    assert UNSEARCHED_ABSENCE_NOTE_ZH in disclose_unsearched_absence("缓存里没有Clapham的房源。", [], "zh")


@pytest.mark.parametrize("reply,artifacts", [
    ("I have no Clapham listings in the cache.", [{"tool": "search_properties"}]),
    ("Hackney is safer than Clapham.", []), ("Here are three listings in Camden.", []),
])
def test_t_04_a_searched_or_unrelated_reply_stays(reply, artifacts):
    from core.agent_loop import disclose_unsearched_absence
    assert disclose_unsearched_absence(reply, artifacts, "en") == reply
