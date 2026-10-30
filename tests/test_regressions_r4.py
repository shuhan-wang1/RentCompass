"""Regression tests (R4-xx / S-xx ids in each test name)."""
from __future__ import annotations

import pytest


def _state_node(accumulated: dict, message: str, history=None) -> dict:
    from core.langgraph_agent import _make_extract_preferences_node
    state = {"user_preferences": {}, "accumulated_search_criteria": dict(accumulated),
             "extracted_context": {"current_message": message,
                                   "history": history or [{"user": "Find a flat", "assistant": "ok"}]}}
    update = _make_extract_preferences_node()(state)
    return update.get("accumulated_search_criteria", state["accumulated_search_criteria"])


_BOW_1BED = {"area": "Bow", "areas": ["Bow"], "bedrooms": 1, "max_budget": 1800, "room_type": "whole"}


# S-01 ── the figure before the field ───────────────────────────────────────────────
@pytest.mark.parametrize("message,budget", [
    ("改成1500预算", 1500),
    ("改为1500预算", 1500),
    ("我想改成1500预算，找Bow的一居室", 1500),
    ("1500的预算", 1500),
    ("换成1500的预算", 1500),
    ("换成1500镑的预算", 1500),
    ("预算改成1500", 1500),
    ("1500以内", 1500),
    ("make it a £1,500 budget", 1500),
    ("£1,500 budget instead", 1500),
    ("change to a 1,500 budget", 1500),
    ("1500 is my new budget", 1500),
    ("a budget of 1500 instead", 1500),
    ("1.5k budget", 1500),
    ("£1.5k budget instead", 1500),
    ("budget 2k", 2000),
    ("2,000 max now", 2000),
    ("1500 tops", 1500),
])
def test_s_01_a_figure_before_the_budget_noun_sets_the_budget(message, budget):
    acc = _state_node(_BOW_1BED, message)
    assert acc["max_budget"] == budget
    assert acc["area"] == "Bow" and acc["bedrooms"] == 1


@pytest.mark.parametrize("message", [
    "1500预算够吗？",
    "预算1500够吗？",
    "1500的预算够不够？",
    "Is a £1,500 budget enough?",
    "is £1,500 enough?",
    "show listing 1500",
    "within 1500 metres of the station",
    "1500 metres max from the station",
    "离车站1500米以内",
    "1500平方英尺以内",
    "I run 5k every morning",
    "我是2020年来的",
])
def test_s_01_questions_and_other_numbers_leave_the_budget_alone(message):
    assert _state_node(_BOW_1BED, message)["max_budget"] == 1800


@pytest.mark.parametrize("message,beds", [
    ("改成2个卧室", 2),
    ("两个卧室", 2),
    ("改成两室一厅", 2),
    ("两室", 2),
    ("三间卧室", 3),
    ("make it 2 bedrooms", 2),
    ("改成两居室", 2),
])
def test_s_01_a_count_before_the_bedroom_word_sets_bedrooms(message, beds):
    acc = _state_node(_BOW_1BED, message)
    assert acc["bedrooms"] == beds
    assert acc["max_budget"] == 1800


@pytest.mark.parametrize("message", ["302室的那套怎么样", "每人一个卧室", "我有两个室友"])
def test_s_01_room_numbers_and_per_person_rooms_are_not_bedroom_counts(message):
    assert _state_node(_BOW_1BED, message)["bedrooms"] == 1


def test_s_01_minutes_before_the_commute_word_still_set_the_limit():
    assert _state_node(_BOW_1BED, "改成30分钟通勤")["max_travel_time"] == 30
