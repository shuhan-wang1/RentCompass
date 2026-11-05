"""Browser test of 2026-11-05, two more findings from the same user.

A. Persona B searched with a £2,000 budget; the same user then started a NEW conversation for
   persona C, a Greenwich family at £2,800. After a refresh the reply to C used £2,800 but
   volunteered an "old record" of £2,000/month, and nothing said where it came from. Every
   remembered row now says whether this conversation saved it, another one did, or its origin is
   unknown; a line under the header says a row from elsewhere is never compared with this
   conversation's criteria; and while this conversation holds a budget, another conversation's
   budget is cut from the block. A "what do you remember about me?" still lists it, labelled.
B. "Before you search, first summarise all my requirements (cat allowed, Canary Wharf, move-in date,
   40-minute commute, room type, budget)" got a candidate list and no recap. A recap the user's
   own message asks for is rendered from the accumulated criteria and leads the reply; and a recap
   request that also changes a slot ("recap my criteria, and make it a 1-bed") still changes it.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import uuid

import pytest

from core.agent_loop import acknowledge_remembered_budget
from core.constraint_updates import is_criteria_consultation
from core.criteria_recap import (HEADING_EN, HEADING_ZH, asks_for_criteria_recap,
                                 render_criteria_recap, with_criteria_recap)
from rag.agent_memory import (MEMORY_SOURCE_OTHER, MEMORY_SOURCE_RULE, MEMORY_SOURCE_THIS,
                              MEMORY_SOURCE_UNKNOWN, conversation_holds_budget, stated_budget_note,
                              without_other_conversation_budgets)

CONV_B, CONV_C = "conv-persona-b", "conv-persona-c"
B_BUDGET = "The user's budget is £2,000 a month and they have a cat."
C_FAMILY = "The user wants a 3-bed house in Greenwich with a garden, budget £2,800 a month."
SAVED_DUST = "The user is allergic to dust."


def _rows():
    return [
        {"id": "c1", "mtype": "semantic", "text": C_FAMILY, "from_conversation": CONV_C},
        {"id": "b1", "mtype": "semantic", "text": B_BUDGET, "from_conversation": CONV_B},
        {"id": "b2", "mtype": "episodic", "text": "User asked: Find a 2-bed in Clapham under £2,000",
         "from_conversation": CONV_B},
        {"id": "x1", "mtype": "semantic", "text": SAVED_DUST},
    ]


@pytest.fixture
def memory(tmp_path, monkeypatch):
    am_mod = importlib.import_module("rag.agent_memory")
    monkeypatch.setattr(am_mod, "call_ollama", lambda *a, **k: "5")
    store = am_mod.AgentMemory(db_path=str(tmp_path / "mem"))
    store.test_user = "u-mem-scope-" + uuid.uuid4().hex[:10]
    return store


def _save_auto(store, text, conversation_id):
    am_mod = importlib.import_module("rag.agent_memory")
    store.add(text, "semantic", user_id=store.test_user, importance=7,
              extra_meta={"source": am_mod.SOURCE_AUTO, "origin_conversation_id": conversation_id,
                          "origin_turn_id": "t-" + uuid.uuid4().hex[:6]})


# ── A1. each remembered row says where it came from ─────────────────────────────────────────

def test_each_fact_is_labelled_with_its_source_and_the_rule_sits_under_the_header(memory):
    block = memory.format_for_prompt(_rows(), conversation_id=CONV_C)
    lines = block.split("\n")
    assert lines[0] == "What I remember about this user:"
    assert lines[1] == MEMORY_SOURCE_RULE
    assert f"[FACT] {MEMORY_SOURCE_THIS} {C_FAMILY}" in lines
    assert f"[FACT] {MEMORY_SOURCE_OTHER} {B_BUDGET}" in lines
    assert f"[FACT] {MEMORY_SOURCE_UNKNOWN} {SAVED_DUST}" in lines
    assert any(line.startswith(f"[PAST] {MEMORY_SOURCE_OTHER} User asked:") for line in lines)
    # The block's consumers cut it at its first blank line.
    assert "\n\n" not in block


def test_the_label_says_another_conversations_row_is_background_not_this_searchs_criteria():
    assert "another conversation" in MEMORY_SOURCE_OTHER
    assert "earlier search" in MEMORY_SOURCE_OTHER and "not this search's criteria" in MEMORY_SOURCE_OTHER
    for words in ("never conflicts with, overrides or gets compared against",
                  "asks what you remember", "has said nothing on that point"):
        assert words in MEMORY_SOURCE_RULE


def test_without_a_conversation_id_the_block_is_what_it_was(memory):
    block = memory.format_for_prompt(_rows())
    assert block.split("\n")[1] == f"[FACT] {C_FAMILY}"
    assert MEMORY_SOURCE_RULE not in block and "(saved in" not in block and "(origin" not in block


def test_the_rule_is_left_out_when_every_fact_is_from_this_conversation(memory):
    block = memory.format_for_prompt([_rows()[0]], conversation_id=CONV_C)
    assert block == f"What I remember about this user:\n[FACT] {MEMORY_SOURCE_THIS} {C_FAMILY}"


def test_a_recall_block_keeps_its_count_header_and_labels_every_fact(memory):
    block = memory.format_for_prompt(_rows(), saved_total=3, conversation_id=CONV_C)
    lines = block.split("\n")
    assert lines[0] == "What I remember about this user (all 3 saved facts are listed):"
    assert lines[1] == MEMORY_SOURCE_RULE
    assert f"[FACT] {MEMORY_SOURCE_OTHER} {B_BUDGET}" in lines


def test_stored_rows_carry_the_conversation_they_came_from(memory):
    _save_auto(memory, B_BUDGET, CONV_B)
    memory.add(SAVED_DUST, "semantic", user_id=memory.test_user, importance=7,
               extra_meta={"source": "explicit_remember"})
    by_text = {r["text"]: r for r in memory.semantic_records(user_id=memory.test_user)}
    assert by_text[B_BUDGET]["from_conversation"] == CONV_B
    assert "from_conversation" not in by_text[SAVED_DUST]
    retrieved = {r["text"]: r for r in memory.retrieve("budget cat", session_id=memory.test_user,
                                                       user_id=memory.test_user)}
    assert retrieved[B_BUDGET]["from_conversation"] == CONV_B


# ── A2. another conversation's budget is cut while this one holds a budget ───────────────────

def test_another_conversations_budget_is_cut_and_the_rest_of_its_row_kept():
    kept = {row["id"]: row["text"] for row in without_other_conversation_budgets(_rows(), CONV_C)}
    assert kept["c1"] == C_FAMILY                       # this conversation's own budget stays
    assert "2,000" not in kept["b1"] and "cat" in kept["b1"]
    assert "2,000" not in kept.get("b2", "")
    assert kept["x1"] == SAVED_DUST                     # origin unknown: left as it is


def test_a_row_that_is_only_another_conversations_budget_goes():
    rows = [{"id": "b", "mtype": "semantic", "text": "Budget £2,000 per month.", "from_conversation": CONV_B}]
    assert without_other_conversation_budgets(rows, CONV_C) == []
    assert without_other_conversation_budgets(rows, CONV_B) == rows


@pytest.mark.parametrize("criteria,message,holds", [
    ({"max_budget": 2800}, "Any with a garden?", True),
    ({}, "We're a family looking for a 3-bed house in Greenwich. Budget £2,800 a month.", True),
    ({}, "我们想在Greenwich找三居室，预算每月2800英镑", True),
    ({"max_budget": None}, "Any with a garden?", False),
    ({}, "Hello", False),
])
def test_the_conversation_holds_a_budget_from_its_criteria_or_this_message(criteria, message, holds):
    assert conversation_holds_budget(criteria, message) is holds


class _CapturingGraph:
    def __init__(self, reply, final=None):
        self.reply, self.final, self.inputs = reply, dict(final or {}), []

    async def ainvoke(self, graph_input, config=None):
        self.inputs.append(graph_input)
        return {"final_response": self.reply, "response_type": "answer", "tool_data": {}, **self.final}


def _turn(monkeypatch, memory, conversation_id, message, reply="OK.", *, criteria=None,
          final=None, language="en"):
    am_mod = importlib.import_module("rag.agent_memory")
    appmod = importlib.import_module("app")
    from uk_rent_agent.web.session_store import SessionStore
    monkeypatch.setattr(am_mod, "get_agent_memory", lambda *a, **k: memory)
    monkeypatch.setattr(appmod, "_session_store", SessionStore())
    if criteria is not None:
        appmod._write_back_turn(memory.test_user, conversation_id, "earlier turn", "Noted.", [],
                                accumulated_search_criteria=criteria)
    graph = _CapturingGraph(reply, final)
    setattr(graph, appmod._GRAPH_RUNNER_ATTR, appmod._graph_loop_runner)
    monkeypatch.setattr(appmod, "agent_graph", graph)
    tag = uuid.uuid4().hex[:10]
    payload = asyncio.run(appmod.handle_with_react_agent(
        message, {}, False, user_id=memory.test_user, conversation_id=conversation_id,
        request_id="req-" + tag, ui_language=language))
    seen = graph.inputs[0].get("memory_context") or graph.inputs[0].get("user_query") or ""
    return payload, seen


GREENWICH = {"area": "Greenwich", "areas": ["Greenwich"], "max_budget": 2800, "budget_period": "month",
             "room_type": "whole", "bedrooms": 3, "no_commute": True}


def test_persona_c_never_reads_persona_bs_budget(memory, monkeypatch):
    cid = CONV_C + "-" + uuid.uuid4().hex[:6]
    _save_auto(memory, B_BUDGET, CONV_B)
    _save_auto(memory, C_FAMILY, cid)
    _payload, seen = _turn(monkeypatch, memory, cid, "Which of these have a garden and fit my budget?",
                           criteria=GREENWICH)
    assert "2,000" not in seen
    assert f"{MEMORY_SOURCE_THIS} {C_FAMILY}" in seen     # this conversation's own budget stays
    assert f"{MEMORY_SOURCE_OTHER} they have a cat" in seen   # the rest of persona B's row stays
    assert MEMORY_SOURCE_RULE in seen


def test_the_first_turn_of_a_new_search_states_its_budget_and_still_never_reads_the_old_one(
        memory, monkeypatch):
    _save_auto(memory, B_BUDGET, CONV_B)
    _payload, seen = _turn(monkeypatch, memory, CONV_C + "-" + uuid.uuid4().hex[:6],
                           "We're a family looking for a 3-bed whole house in Greenwich with a "
                           "garden. Budget £2,800 a month.")
    assert "2,000" not in seen


def test_a_new_search_without_a_budget_sees_the_old_one_labelled_as_another_search(memory, monkeypatch):
    _save_auto(memory, B_BUDGET, CONV_B)
    _payload, seen = _turn(monkeypatch, memory, CONV_C + "-" + uuid.uuid4().hex[:6],
                           "Find me a 3-bed house in Greenwich, what budget did I use before?")
    assert f"{MEMORY_SOURCE_OTHER} {B_BUDGET}" in seen


def test_what_do_you_remember_still_lists_the_other_budget_with_its_source(memory, monkeypatch):
    _save_auto(memory, B_BUDGET, CONV_B)
    cid = CONV_C + "-" + uuid.uuid4().hex[:6]
    _save_auto(memory, C_FAMILY, cid)
    _payload, seen = _turn(monkeypatch, memory, cid, "What do you remember about me?",
                           criteria=GREENWICH)
    assert f"{MEMORY_SOURCE_OTHER} {B_BUDGET}" in seen
    assert f"{MEMORY_SOURCE_THIS} {C_FAMILY}" in seen
    assert "all 2 saved facts are listed" in seen
    # ... and it is not called an older figure of this search.
    assert "say the saved figure differs" not in seen


# ── A3. the two deterministic budget notes never name another conversation's budget ──────────

def test_the_recall_budget_note_ignores_another_conversations_figure():
    rows = [r for r in _rows() if r["mtype"] == "semantic"]
    assert stated_budget_note(rows, {"max_budget": 2800}, CONV_C) == ""
    # Without a conversation the note works as before; of unknown origin it still fires.
    assert "differs" in stated_budget_note(rows, {"max_budget": 2800})
    unknown = [{"mtype": "semantic", "text": "My budget is £1,500 a month."}]
    assert "differs" in stated_budget_note(unknown, {"max_budget": 2800}, CONV_C)


def test_acknowledge_remembered_budget_is_silent_while_this_conversation_has_a_budget(memory):
    block = memory.format_for_prompt(_rows(), conversation_id=CONV_C)
    answer = "Here are three 3-bed houses in Greenwich."
    for searched_without in (True, False):
        assert acknowledge_remembered_budget(answer, block, 2800, "en",
                                             searched_without_budget=searched_without) == answer
        assert acknowledge_remembered_budget("I don't have a budget from you yet.", block, 2800, "en",
                                             searched_without_budget=searched_without) \
            == "I don't have a budget from you yet."


def test_with_no_budget_here_the_offer_says_the_figure_is_from_an_earlier_conversation(memory):
    rows = [r for r in _rows() if r["id"] != "c1"]
    block = memory.format_for_prompt(rows, conversation_id=CONV_C)
    out = acknowledge_remembered_budget("Here are three houses.", block, None, "en",
                                        searched_without_budget=True)
    assert "£2,000 a month in an earlier conversation" in out and "apply it" in out
    zh = acknowledge_remembered_budget("这是房源。", block, None, "zh", searched_without_budget=True)
    assert "在之前的对话中保存过预算" in zh
    plain = acknowledge_remembered_budget("Here are three houses.",
                                          memory.format_for_prompt(rows), None, "en",
                                          searched_without_budget=True)
    assert "£2,000 a month earlier." in plain


def test_a_source_label_is_not_evidence_of_what_the_user_said(memory):
    from core.preference_grounding import conversation_evidence
    state = {"memory_context": memory.format_for_prompt(_rows(), conversation_id=CONV_C),
             "extracted_context": {"current_message": "hi"}}
    evidence = conversation_evidence(state)
    assert B_BUDGET in evidence and C_FAMILY in evidence and SAVED_DUST in evidence
    assert not any("conversation" in item or "origin" in item for item in evidence)


# ── B1. a recap request, in English and Chinese ──────────────────────────────────────────────

COUPLE_TURN2 = ("Before you search, please first summarise all my requirements: cat must be allowed, "
                "Canary Wharf, move-in date, 40-minute commute, room type, budget.")


@pytest.mark.parametrize("message", [
    COUPLE_TURN2,
    "Please first summarise every requirement (cat must be allowed, Canary Wharf, move-in date, "
    "40-minute commute, room type, budget)",
    "summarize my criteria", "Can you recap my requirements?", "Give me a summary of my requirements",
    "list back what I asked for", "repeat back my criteria please", "Confirm all my requirements first",
    "recap the criteria so far",
    "先总结一下我的条件", "复述一下", "确认一下我所有的要求", "把我的条件列一下", "我的条件总结一下",
    "给我一个需求总结", "先汇总一下我们的找房条件再搜索",
])
def test_a_recap_request_is_recognised(message):
    assert asks_for_criteria_recap(message)


@pytest.mark.parametrize("message", [
    "What do you remember about me?", "What do you remember about my requirements?",
    "你还记得我的条件吗", "summarise this listing", "give me a summary of the area",
    "Summarise all the requirements for renting in the UK", "confirm this flat meets all my requirements",
    "Does this one match my requirements?", "总结一下这个房源", "复述一下这个房源的介绍",
    "请确认我的条件是否符合这套房源", "don't repeat my requirements, just search",
    "list my saved preferences", "Find me a 2-bed in Camden", "",
])
def test_other_messages_are_not_a_recap_request(message):
    assert not asks_for_criteria_recap(message)


# ── B2. the recap, from the accumulated criteria ────────────────────────────────────────────

COUPLE = {"area": "Canary Wharf", "areas": ["Canary Wharf"], "commute_destination": "Canary Wharf, London E14 5AB",
          "destination": "Canary Wharf, London E14 5AB", "max_travel_time": 40, "no_commute": False,
          "max_budget": 2000, "budget_period": "month", "room_type": "whole", "bedrooms": 1,
          "bedrooms_minimum": False, "move_in_date": "2027-01-15", "property_features": ["cats allowed"],
          "soft_preferences": ["quiet street"], "tenant_status": "other", "constraint_sources": {}}


def test_the_recap_covers_every_slot_the_couple_named():
    assert render_criteria_recap(COUPLE, "en").split("\n") == [
        HEADING_EN,
        "- Areas: Canary Wharf",
        "- Commute: to Canary Wharf, London E14 5AB, at most 40 minutes",
        "- Budget: up to £2,000 a month",
        "- Home: whole property, 1 bedroom",
        "- Move-in date: 15 January 2027",
        "- Must have: cats allowed",
        "- Preferences (nice to have, not filters): quiet street",
        "- About you: not a student",
    ]


def test_the_chinese_recap():
    assert render_criteria_recap(COUPLE, "zh").split("\n") == [
        HEADING_ZH,
        "- 区域：Canary Wharf",
        "- 通勤：到 Canary Wharf, London E14 5AB，不超过 40 分钟",
        "- 预算：每月不超过 £2,000",
        "- 房型：整套，1 间卧室",
        "- 入住日期：2027年1月15日",
        "- 必须满足：允许养猫",
        "- 偏好（加分项，不作为筛选条件）：quiet street",
        "- 身份：非学生",
    ]


def test_a_weekly_budget_is_shown_as_the_user_gave_it():
    student = {"max_budget": 1300.0, "budget_period": "month", "room_type": "studio+ensuite",
               "property_features": ["bills included"], "tenant_status": "student",
               "constraint_sources": {"max_budget": {"source": "user_message", "value": 1300.0,
                                                     "text": "预算每周£300，包bills"}}}
    en = render_criteria_recap(student, "en")
    assert "- Budget: up to £1,300 a month (you gave it as £300 a week)" in en
    assert "- Home: studio or room with en-suite bathroom" in en
    assert "- About you: a student" in en
    assert "（你给出的是每周 £300）" in render_criteria_recap(student, "zh")
    weekly = render_criteria_recap({"max_budget": 250, "budget_period": "week"}, "en")
    assert "- Budget: up to £250 a week (about £1,083 a month)" in weekly


def test_unset_slots_are_said_and_no_commute_is_a_slot():
    lines = render_criteria_recap({"no_commute": True, "bedrooms": 2, "bedrooms_minimum": True}, "en").split("\n")
    assert "- Areas: not given yet" in lines and "- Budget: not given yet" in lines
    assert "- Commute: none (no commute needed)" in lines
    assert "- Home: at least 2 bedrooms" in lines
    assert "- 通勤：不需要通勤" in render_criteria_recap({"no_commute": True}, "zh").split("\n")


LEDGER = ("Matches all stated conditions (verified):\n- Westferry Road, E14 (£1,950/month)\n"
          "Not verified:\n- Water Lane, E15 (£900/month): cannot verify the commute condition")


def test_the_recap_leads_the_reply_and_is_added_once():
    out = with_criteria_recap(LEDGER, COUPLE, message=COUPLE_TURN2, language="en")
    assert out.startswith(HEADING_EN + "\n- Areas: Canary Wharf")
    assert out.endswith(LEDGER)
    assert with_criteria_recap(out, COUPLE, message=COUPLE_TURN2, language="en") == out
    assert with_criteria_recap(LEDGER, COUPLE, message="Show me more", language="en") == LEDGER
    assert with_criteria_recap("", COUPLE, message="复述一下", language="zh").startswith(HEADING_ZH)


def test_the_turn_reply_opens_with_the_recap(memory, monkeypatch):
    payload, _seen = _turn(monkeypatch, memory, "c-" + uuid.uuid4().hex[:10], COUPLE_TURN2,
                           reply="Here are two flats near Canary Wharf: Westferry Road, E14 (£1,950/month).",
                           final={"accumulated_search_criteria": COUPLE})
    message = payload["message"]
    assert message.startswith(HEADING_EN)
    for line in ("- Budget: up to £2,000 a month", "- Move-in date: 15 January 2027",
                 "- Must have: cats allowed", "- Commute: to Canary Wharf, London E14 5AB, at most 40 minutes"):
        assert line in message
    assert message.index("- Must have: cats allowed") < message.index("Westferry Road")


def test_a_chinese_turn_gets_the_chinese_recap(memory, monkeypatch):
    payload, _seen = _turn(monkeypatch, memory, "c-" + uuid.uuid4().hex[:10], "先总结一下我的条件",
                           reply="好的。", final={"accumulated_search_criteria": COUPLE}, language="zh")
    assert payload["message"].startswith(HEADING_ZH + "\n- 区域：Canary Wharf")


# ── B3. a recap request that also changes a slot still changes it ────────────────────────────

def _criteria_after(*messages):
    from core.langgraph_agent import _apply_explicit_criteria_updates
    criteria = {}
    for message in messages:
        criteria = _apply_explicit_criteria_updates(json.loads(json.dumps(criteria)), message)
    return criteria


COUPLE_TURN1 = ("My partner and I want a whole 2-bed flat near Canary Wharf, our cat must be allowed, "
                "move in 2027-01-15, max 40 minutes commute, budget £2,000 a month")


@pytest.mark.parametrize("message,field,value", [
    ("Recap my criteria, and make it a 1-bed", "bedrooms", 1),
    ("summarise, and make it a 1-bed", "bedrooms", 1),
    ("Recap my requirements, budget is now £1,900", "max_budget", 1900),
    ("Give me a summary of my requirements and change the budget to £1,800", "max_budget", 1800),
    ("给我一个需求总结，预算改成1800", "max_budget", 1800),
    ("先总结一下我的条件，然后改成一居室", "bedrooms", 1),
])
def test_a_recap_request_that_changes_a_slot_changes_it(message, field, value):
    assert not is_criteria_consultation(message)
    assert _criteria_after(COUPLE_TURN1, message)[field] == value
    assert asks_for_criteria_recap(message)


@pytest.mark.parametrize("message", ["Recap my criteria", "Give me a summary of my requirements",
                                     "Please don't search; give me a summary",
                                     "Give me a summary of the 2-bed flats in Camden"])
def test_a_recap_request_alone_changes_nothing(message):
    before = _criteria_after(COUPLE_TURN1)
    after = _criteria_after(COUPLE_TURN1, message)
    assert is_criteria_consultation(message)
    for field in ("bedrooms", "max_budget", "area", "room_type", "move_in_date", "max_travel_time"):
        assert after.get(field) == before.get(field)
