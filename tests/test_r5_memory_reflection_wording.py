"""R4-12 (memory half): reflections never escalate a preference or present a guess as the user's.

Recorded rows (acceptance r06 instance D, user 2f5816d7…, the R2-06 chain; r08 user d3a70656…):
the user wrote "Find a 1-bed in Hackney under £2,500", "Actually make it £2,200", "Let's say
£1,800", "Does the first one allow pets?", "Is the area safe?", "Remember I have a cat and work
at Canary Wharf", "What do you remember about me?" — and the reflection job stored "pet-
friendliness (cat) and area safety are deal-breakers, not nice-to-haves", "budget is negotiable"
and "a firm budget ceiling around £2,200"; the next search then carried "pet friendly (cat)" and
"safe area" as preferences. r08 stored "User has a firm, non-negotiable set of core criteria …" for
"Find a step-free 1-bed in Camden under £1,800."
"""
from __future__ import annotations

import importlib
import json
import os
import sys

import pytest


def _pin_app():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(repo, "app")
    if local in sys.path:
        sys.path.remove(local)
    sys.path.insert(0, local)


_pin_app()
am_mod = importlib.import_module("rag.agent_memory")
USER = "user-r5-reflection-wording"

R2_06_USER_TURNS = (
    "Find a 1-bed in Hackney under £2,500", "Actually make it £2,200", "Let's say £1,800",
    "Does the first one allow pets?", "Is the area safe?",
    "Remember I have a cat and work at Canary Wharf", "What do you remember about me?",
)
# Verbatim reflection rows the r06 / r08 memory jobs stored.
RECORDED_ESCALATIONS = (
    "Beyond price and location, practical livability factors dominate: pet-friendliness (cat) "
    "and area safety are deal-breakers, not nice-to-haves.",
    "Location (Hackney) and property type (1-bed) are firm priorities, while budget is negotiable.",
    "User's budget is flexible but anchored low: they repeatedly revise between £1,800 and £2,500, "
    "suggesting £1,800–£2,200 is the real comfort zone with some stretch for the right place.",
    "User wants a 1-bedroom flat in Hackney with a firm budget ceiling around £2,200, having "
    "tightened the initial £2,500 limit.",
    "User has a firm, non-negotiable set of core criteria: step-free access, a 1-bedroom home, "
    "located in Camden, and priced under £1,800.",
    # r07 inst-A vol1: an inference worded as what the user said.
    "There is a conflict in the records about Canary Wharf and Manchester. The memories say the "
    "guarantor is in Canary Wharf, but the user's own request said Manchester.",
    "该用户的预算是硬性上限，必须严格控制在£1,800以内。",
)
# Kept by the same job, and still kept: worded as a guess, no escalation.
RECORDED_TENTATIVE = ("Hackney is likely chosen for commute practicality to Canary Wharf combined "
                      "with budget fit, so they need a balance of affordability, safety, and "
                      "pet-friendly housing over pure location prestige.")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    prompts: list[str] = []
    script = {"insights": []}

    def fake_llm(prompt, *a, **k):
        prompts.append(prompt)
        if '"insights"' in prompt:
            return json.dumps({"insights": list(script["insights"])})
        if '"facts"' in prompt:
            return json.dumps({"facts": []})
        if '"ops"' in prompt:
            return json.dumps({"ops": []})
        return "5"

    monkeypatch.setattr(am_mod, "call_ollama", fake_llm)
    return am_mod.AgentMemory(db_path=str(tmp_path / "mem")), prompts, script


def _reflections(memory, user=USER):
    got = memory.col.get(where={"$and": [{"user_id": user}, {"mtype": "reflection"}]})
    return list(got.get("documents") or [])


def _reflect_over(memory, script, user_turns, insights, user=USER):
    for text in user_turns:
        memory.add(f"User asked: {text}", "episodic", "s", user, role="user", importance=5)
    script["insights"] = insights
    memory._accum[(user, "s")] = am_mod.REFLECT_IMPORTANCE_THRESHOLD
    memory.maybe_reflect("s", user)
    return _reflections(memory, user)


def test_recorded_escalating_reflections_are_not_stored(harness):
    memory, _prompts, script = harness
    stored = []
    for insight in RECORDED_ESCALATIONS:
        stored += _reflect_over(memory, script, R2_06_USER_TURNS, [insight, RECORDED_TENTATIVE])
    assert set(stored) == {RECORDED_TENTATIVE}, stored


def test_the_users_own_escalation_word_may_stay(harness):
    memory, _prompts, script = harness
    stored = _reflect_over(
        memory, script,
        ["Please remember this for all my future searches: my budget is at most £1,900 a month, "
         "I need a 1 bedroom whole flat in Hackney, and I have a cat called Quillbert so pets "
         "must be allowed.", "Use my saved defaults and search again.",
         "Do I have a cat?", "What do you remember about my pet?"],
        ["A cat-friendly landlord seems to be a must for this user.",
         "Pet-friendliness looks like a deal-breaker for this user."])
    assert stored == ["A cat-friendly landlord seems to be a must for this user."]


def test_the_reflection_prompt_asks_for_tentative_unescalated_wording(harness):
    memory, prompts, script = harness
    _reflect_over(memory, script, R2_06_USER_TURNS, [])
    prompt = [p for p in prompts if '"insights"' in p][-1]
    assert "TENTATIVE" in prompt and "non-negotiable, deal-breaker" in prompt
    assert "unless the user used that word" in prompt
    assert "Never write that the user said" in prompt


@pytest.mark.parametrize("text,reason", [
    ("Pet-friendliness is a key requirement, likely a dealbreaker", "escalation:deal-breaker"),
    ("User's non-negotiables are a whole 1-bedroom flat in Islington", "escalation:non-negotiable"),
    ("A studio in Bow with a hard budget cap of £1700 pcm", "escalation:hard"),
    ("Budget is the dominant constraint", "escalation:core"),
    ("They would relax the price limit to stay in Hackney", "escalation:relax"),
    ("The user stated that a balcony is essential", "escalation:essential"),
    ("The user explicitly asked for a quiet street", "attribution"),
    ("用户明确表示预算不能超过£1,800", "attribution"),
])
def test_wording_rejection_reasons(text, reason):
    assert am_mod.reflection_rejection(text, "Find a 1-bed in Hackney under £2,500") == reason


@pytest.mark.parametrize("text", [
    RECORDED_TENTATIVE,
    "The user seems to want a quiet street near a park.",
    "Commute time to King's Cross probably matters when comparing areas.",
    "该用户可能更看重通勤时间。",
])
def test_tentative_wording_passes(text):
    assert am_mod.reflection_rejection(text, "") is None


def test_reflections_are_labelled_tentative_and_legacy_escalations_are_not_shown(harness):
    memory, _prompts, _script = harness
    block = memory.format_for_prompt([
        {"text": "I have a cat and work at Canary Wharf", "mtype": "semantic"},
        {"text": RECORDED_ESCALATIONS[0], "mtype": "reflection"},     # stored before R4-12
        {"text": RECORDED_TENTATIVE, "mtype": "reflection"},
    ])
    stated, inferred = block.split("The assistant's own inferences", 1)
    assert "[FACT] I have a cat" in stated
    assert "TENTATIVE guesses that may be wrong" in inferred
    assert "NOT said or saved by the user" in inferred
    assert f"[INFERENCE] {RECORDED_TENTATIVE}" in inferred
    assert "deal-breakers" not in block


def test_recall_tool_labels_reflections_and_drops_legacy_escalations(tmp_path, monkeypatch):
    import core.tools.memory_tools as mt
    memory = am_mod.AgentMemory(db_path=str(tmp_path / "mem2"))
    monkeypatch.setattr(mt, "_mem", lambda: memory)
    memory.add("User has a cat and works at Canary Wharf", "semantic", "s", USER)
    memory.add(RECORDED_ESCALATIONS[0], "reflection", "s", USER, importance=8)
    memory.add(RECORDED_TENTATIVE, "reflection", "s", USER, importance=8)
    result = mt.recall_memory_impl("pets cat area safety Hackney commute", user_id=USER, n=6)
    texts = [m["text"] for m in result["memories"]]
    assert RECORDED_ESCALATIONS[0] not in texts and "deal-breakers" not in result["formatted"]
    labelled = [m for m in result["memories"] if m["mtype"] == "reflection"]
    assert labelled and all(m["note"] == am_mod.INFERENCE_NOTE for m in labelled)
