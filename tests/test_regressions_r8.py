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
