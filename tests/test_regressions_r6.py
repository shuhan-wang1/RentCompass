"""Regression tests (U-xx ids in each test name)."""
from __future__ import annotations

import asyncio
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
    out = memory_gate.settle_memory_deletion_claims(reply + " Anything else?", "ok thanks", None, "en")
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
])
def test_u_10a_a_true_statement_stays(reply, message):
    from core import memory_gate
    assert memory_gate.settle_memory_deletion_claims(reply, message, None, "en") == reply


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
    "这套附近有超市吗",
])
def test_u_09_anything_else_keeps_the_area(message):
    acc = _state_node({"area": "Bristol", "areas": ["Bristol"], "bedrooms": 1, "max_budget": 1200}, message)
    assert acc["area"] == "Bristol" and acc["areas"] == ["Bristol"]


@pytest.mark.parametrize("message", ["温莎附近有房吗", "牛津街附近有房吗", "UCL附近有房吗"])
def test_u_09_a_place_nothing_here_resolves_does_not_reuse_the_old_area(message):
    acc = _state_node({"area": "Bristol", "areas": ["Bristol"], "bedrooms": 1, "max_budget": 1200}, message)
    assert acc["area"] is None and acc["areas"] == []


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
    (_, params), = provider.calls
    assert not params.get("confirmed") and not params.get("criteria_gate_shown")
