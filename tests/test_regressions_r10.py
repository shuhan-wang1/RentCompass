"""Regression tests (G-xx ids in each test name)."""
from __future__ import annotations

import json

import pytest


def _state_node(accumulated: dict, message: str, history=None) -> dict:
    from core.langgraph_agent import _make_extract_preferences_node
    state = {"user_preferences": {}, "accumulated_search_criteria": dict(accumulated),
             "extracted_context": {"current_message": message,
                                   "history": history or [{"user": "Find a flat", "assistant": "ok"}]}}
    update = _make_extract_preferences_node()(state)
    return update.get("accumulated_search_criteria", state["accumulated_search_criteria"])


# G-01 ── a weekly rent is weekly however it is written
@pytest.mark.parametrize("text,expected", [
    ("Find me a 1-bed in Hackney, £450pw", (450, "week")),
    ("£600pw max in Camden", (600, "week")),
    ("£450 pcw", (450, "week")),
    ("450 pcw", (450, "week")),
    ("max £300 p.w.", (300, "week")),
    ("max £300 p.w. Looking in Camden.", (300, "week")),
    ("£300 p.c.w. in Hackney", (300, "week")),
    ("£350pw all bills", (350, "week")),
    ("£120 per wk", (120, "week")),
    ("£120 a wk", (120, "week")),
    ("£120 weekly", (120, "week")),
    ("450 weekly", (450, "week")),
    ("my weekly budget is £300", (300, "week")),
    ("£450 p/w", (450, "week")),
    ("£400 per week", (400, "week")),
    ("每周£350", (350, "week")),
    ("350镑/周", (350, "week")),
])
def test_g_01_a_weekly_figure_reads_weekly(text, expected):
    from core.tools.search_properties import _extract_budget
    assert _extract_budget(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("£1800 p.c.m.", (1800, "month")),
    ("£1,800pcm", (1800, "month")),
    ("£1500/month", (1500, "month")),
    ("1500 pm", (1500, "month")),
    ("under 1800 pcm", (1800, "month")),
    ("budget £1,800 pcm, gym once a week", (1800, "month")),
    ("budget £1500 and I go to the gym weekly", (1500, "month")),
    ("budget £1500, gym twice a week", (1500, "month")),
    ("£2000 a month, three days a week in the office", (2000, "month")),
    ("预算每月1500，我每周去两次健身房", (1500, "month")),
])
def test_g_01_a_monthly_figure_stays_monthly(text, expected):
    from core.tools.search_properties import _extract_budget
    assert _extract_budget(text) == expected


def test_g_01_a_weekly_correction_moves_the_ceiling():
    after = _state_node({"area": "Hackney", "areas": ["Hackney"], "max_budget": 1500, "budget_period": "month"},
                        "Actually I can stretch to £480pw.")
    assert after["max_budget"] == 2080


@pytest.mark.parametrize("text,expected", [
    ("Actually I can stretch to £2,000.", (2000, "month")),
    ("I can stretch to £480pw", (480, "week")),
    # A stretch beside a ceiling, or with a condition, stays a note.
    ("My budget is £1,900, but I can stretch to £2,100.", (1900, "month")),
    ("I can stretch to £2,000 if needed", (None, None)),
    ("I could stretch to £2,000", (None, None)),
])
def test_g_01_a_plain_can_stretch_is_the_new_ceiling(text, expected):
    from core.tools.search_properties import _extract_budget
    assert _extract_budget(text) == expected


@pytest.mark.parametrize("text", ["raise it by £50pw", "raise it by £50 p.w.", "raise it by £50 per wk"])
def test_g_01_a_weekly_delta_is_converted(text):
    from core.search_readiness import detect_relative_budget_delta
    assert detect_relative_budget_delta(text) == pytest.approx(216.67, abs=0.01)


# G-02 ── a withdrawal with a replacement in one message
_STRATFORD = {"area": "Stratford", "areas": ["Stratford"], "bedrooms": 1, "max_budget": 2000,
              "no_commute": True, "room_type": "whole", "budget_period": "month"}


@pytest.mark.parametrize("text", [
    "Actually I changed my mind, forget Stratford, use Poplar",
    "Forget about Stratford and search Poplar",
    "Never mind Stratford, Poplar please",
    "Never mind Stratford, how about Poplar?",
    "Scrap Stratford, use Poplar instead",
    "Forget Stratford. Use Poplar.",
    "Use Poplar, forget Stratford",
    "Ignore Stratford, use Poplar",
    "Disregard Stratford, search Poplar.",
    "忘掉Stratford，用Poplar",
    "不要Stratford了，搜Poplar",
])
def test_g_02_the_replacement_takes_over(text):
    from core.langgraph_agent import _apply_explicit_criteria_updates as apply
    after = apply(dict(_STRATFORD), text)
    assert (after["area"], after["areas"]) == ("Poplar", ["Poplar"])
    assert after["max_budget"] == 2000


@pytest.mark.parametrize("text", [
    "Remove Stratford", "Never mind Stratford", "Scrap Stratford",
    "Remove Stratford, Canary Wharf is too far",
    "Remove Stratford and Hackney",
])
def test_g_02_a_removal_with_no_replacement_leaves_no_area(text):
    from core.langgraph_agent import _apply_explicit_criteria_updates as apply
    after = apply(dict(_STRATFORD), text)
    assert (after["area"], after["areas"]) == (None, [])


def test_g_02_one_of_two_areas_is_replaced():
    from core.tools.search_properties import area_list_edit
    assert area_list_edit("Forget Stratford, use Poplar", ["Stratford", "Hackney"]) == ["Hackney", "Poplar"]
    assert area_list_edit("Forget Stratford, use Poplar", ["Poplar"]) == ["Poplar"]     # idempotent


@pytest.mark.parametrize("text", ["Search Poplar", "Try Poplar", "Poplar please"])
def test_g_02_a_place_without_a_removal_is_no_list_edit(text):
    from core.tools.search_properties import area_list_edit
    assert area_list_edit(text, ["Stratford"]) is None


def test_g_02_a_budget_named_to_be_replaced_is_no_reference_to_it():
    from core.constraint_updates import budget_reference
    assert budget_reference("Forget my old budget, use £2,300 instead") is None
    assert budget_reference("Go back to my old budget") == "previous"
    assert budget_reference("Use the original budget") == "first"


def test_g_02_the_new_budget_holds_through_the_state_node():
    """The erasure pops the budget's history first: "previous" then had no target and undid the
    turn, so view_state held no budget while the search and the reply used £2,300."""
    before = {"area": "Bow", "areas": ["Bow"], "bedrooms": 1, "max_budget": 1700, "budget_period": "month",
              "constraint_history": {"area": ["Bow"], "bedrooms": [1]},
              "_last_change": {"before": {"area": None, "areas": None, "bedrooms": None, "max_budget": None}}}
    after = _state_node(before, "Forget my old budget, use £2,300 instead")
    assert after["max_budget"] == 2300
    assert after["constraint_sources"]["max_budget"]["source"] == "user_message"


def test_g_02_an_erased_budget_stays_out_of_the_records():
    before = {"area": "Bow", "areas": ["Bow"], "max_budget": 1700, "budget_period": "month",
              "_erased_fields": ["budget"], "constraint_history": {"area": ["Bow"]},
              "_last_change": {"before": {"area": None, "areas": None, "max_budget": None}}}
    after = _state_node(before, "Forget my old budget, use £2,300 instead")
    assert after["constraint_history"]["max_budget"] == [2300]
    assert after["_last_change"]["before"]["max_budget"] is None
    # A later change keeps its own history as usual.
    later = _state_node(after, "Actually make it £2,500")
    assert later["constraint_history"]["max_budget"] == [2300, 2500]
    assert later["_last_change"]["before"]["max_budget"] == 2300


def test_g_02_a_figure_beside_a_previous_budget_wins():
    from core.langgraph_agent import undo_last_change
    before = {"max_budget": 1700, "_last_change": {"before": {"max_budget": 1500}}}
    after = {"max_budget": 1900}
    assert undo_last_change(before, after, "Back to my previous budget, £1,900")["max_budget"] == 1900
    assert undo_last_change(before, dict(before), "Back to my previous budget")["max_budget"] == 1500


# G-03 ── a change verb moves the search, it does not filter the cards
_POPLAR_IN_BOW = [
    {"address": "Aegean Court, Poplar, E3", "price": "£1,800 pcm", "area": "bow", "bedrooms": 1, "url": "https://x/1"},
    {"address": "Bow Road, London E3", "price": "£1,700 pcm", "area": "bow", "bedrooms": 1, "url": "https://x/2"},
]


@pytest.mark.parametrize("text", [
    "Change the area to Poplar", "Switch to Poplar", "Move the search to Poplar", "Go back to Poplar",
    "Make it Poplar", "换到Poplar", "改成Poplar",
])
def test_g_03_a_change_of_area_is_no_subset(text):
    from core.refine_results import parse_refinement
    assert parse_refinement(text, _POPLAR_IN_BOW) is None


@pytest.mark.parametrize("text", ["Only the ones in Poplar", "Just the ones in Poplar"])
def test_g_03_a_real_subset_still_filters(text):
    from core.refine_results import parse_refinement
    spec = parse_refinement(text, _POPLAR_IN_BOW)
    assert spec and spec["filters"][0]["kind"] == "area"


# G-04 ── a rent bound is the budget however it is worded
@pytest.mark.parametrize("row,rest", [
    ("User asked: Find me a 1-bed in Hackney under £1,850, no commute",
     "User asked: Find me a 1-bed in Hackney, no commute"),
    ("User asked: Find a flat, max £1,850 a month", "User asked: Find a flat"),
    ("User asked: cheap studio up to £1.5k", "User asked: cheap studio"),
    ("User asked: 1-bed, 1500 max", "User asked: 1-bed"),
    ("User asked: 在Hackney找一居室，1850镑以内", "User asked: 在Hackney找一居室"),
    ("User asked: 帮我找房，不超过两千镑", "User asked: 帮我找房"),
    ("The user seems focused on 1-beds under £1,850 in Hackney.", "The user seems focused on 1-beds in Hackney."),
])
def test_g_04_a_rent_bound_is_found_and_only_it_goes(row, rest):
    from rag.agent_memory import _states_field_value, _without_field_clauses
    assert _states_field_value(row, "budget", "episodic")
    assert _states_field_value(row, "budget", "reflection")
    assert _without_field_clauses(row, "budget") == rest


def test_g_04_a_bare_bound_answers_its_question():
    from rag.agent_memory import _states_field_value, _without_field_clauses
    assert _states_field_value("User asked: under £1,850 please", "budget", "episodic")
    assert _without_field_clauses("User asked: under £1,850 please", "budget") == ""


@pytest.mark.parametrize("row", [
    "User asked: Is there a travelcard under £150?",
    "User asked: What is the fare, under £200?",
    "User asked: flat with a deposit under £2,000",
    "User asked: The flat is listed at £1,850",
    "User asked: my friend wants a flat under £2,000",
    "User asked: find me a 1-bed within 800 metres of the station",
    "User asked: find a 2-bed up to 30 minutes from Bank",
    "User asked: houses under 500m from the park",
    "User asked: what do Camden rents average? £1800",
])
def test_g_04_other_figures_are_no_budget(row):
    from rag.agent_memory import _states_field_value
    assert not _states_field_value(row, "budget", "episodic")


def test_g_04_forget_scrubs_the_search_log(tmp_path, monkeypatch):
    from rag.agent_memory import AgentMemory
    monkeypatch.setenv("AGENT_MEMORY_DB_PATH", str(tmp_path / "memory"))
    memory = AgentMemory()
    uid = "g04-user"
    memory.add("User asked: Find me a 1-bed in Hackney under £1,850, no commute", "episodic", uid, uid, role="user")
    memory.add("User asked: Is there a travelcard under £150?", "episodic", uid, uid, role="user")
    memory.add("The user keeps looking at 1-beds under £1,850.", "reflection", uid, uid)
    report = memory.forget_fact(uid, ("budget",))
    assert report["complete"]
    documents = memory.col.get(where={"user_id": uid}).get("documents")
    assert sorted(documents) == ["The user keeps looking at 1-beds.",
                                 "User asked: Find me a 1-bed in Hackney, no commute",
                                 "User asked: Is there a travelcard under £150?"]


_ERASED = {"fields": ("budget",), "complete": True, "deleted": 1, "rewritten": 0}


@pytest.mark.parametrize("body,kept", [
    ("Done. I've deleted your saved budget from long-term memory, so it won't be recalled in any "
     "future conversation.\n\nI still have two other things on file.", "I still have two other things on file."),
    ("Done. Your saved budget figures have been deleted from long-term memory, in every place they "
     "were stored. They will not be recalled in any future conversation.\n\nAnything else?", "Anything else?"),
    ("I've deleted your old budget and set £2,600 as the new one.", "I've deleted your old budget and set £2,600 as the new one."),
])
def test_g_04_the_reply_says_the_deletion_once(body, kept):
    from core.memory_gate import MEMORY_ERASED_ACK_EN, acknowledge_memory_erasure
    text = acknowledge_memory_erasure(body, _ERASED, "en")
    assert text.startswith(MEMORY_ERASED_ACK_EN.format(fields="budget"))
    assert text.endswith(kept)
    assert "long-term memory" not in text and "future conversation" not in text


def test_g_04_the_zh_reply_says_the_deletion_once():
    from core.memory_gate import acknowledge_memory_erasure
    body = ("已经处理好了：你保存过的所有预算数据，我已经从长期记忆中全部删除，不会再被调取，之后的对话里也不会再用到。\n\n"
            "目前仍保留的内容有两条。")
    text = acknowledge_memory_erasure(body, _ERASED, "zh")
    assert text.count("删除") == 1 and text.endswith("目前仍保留的内容有两条。")
