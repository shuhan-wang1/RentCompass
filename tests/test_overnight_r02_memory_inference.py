"""Long-term memory must not amplify its own inferences into the user's constraints (R2-06,
R2-20).

Live: reflections escalated "Pet-friendliness is a key requirement, likely a dealbreaker" to
"a non-negotiable dealbreaker"; the recall answer ("What do you remember about me?") was fed to
the fact extractor and came back as NEW semantic rows ("Pet-friendliness is a non-negotiable
dealbreaker for the user"); the next search carried accumulated_preferences ['pet-friendly',
'value-conscious', 'total cost of living']. "no commute" was stored as "Wants no commute (to live
very close to their destination)" and repeated to the user as their preference.
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


@pytest.fixture
def harness(tmp_path, monkeypatch):
    prompts: list[str] = []

    def fake_llm(prompt, *a, **k):
        prompts.append(prompt)
        if '"facts"' in prompt:
            # A model that would happily restate the assistant's text as facts.
            facts = ["Wants no commute (to live very close to their destination)"]
            if "on file" in prompt:      # the recall answer reached the extractor
                facts.append("Pet-friendliness is a non-negotiable dealbreaker for the user")
            return json.dumps({"facts": facts})
        if '"ops"' in prompt:
            facts = json.loads(prompt.split("NEW FACTS:\n", 1)[1].split("\n\nJSON:", 1)[0])
            return json.dumps({"ops": [{"event": "ADD", "id": None, "text": f} for f in facts]})
        if '"insights"' in prompt:
            escalated = "dealbreaker" in prompt.split("JSON:")[0]
            return json.dumps({"insights": [
                "Pet-friendliness is a non-negotiable dealbreaker" if escalated
                else "Pet-friendliness is a key requirement, likely a dealbreaker"]})
        return "5"

    monkeypatch.setattr(am_mod, "call_ollama", fake_llm)
    return am_mod.AgentMemory(db_path=str(tmp_path / "mem")), prompts


def _docs(memory, user, mtype=None):
    got = memory.col.get(where={"user_id": user})
    return [d for d, m in zip(got.get("documents") or [], got.get("metadatas") or [])
            if mtype is None or (m or {}).get("mtype") == mtype]


def test_r2_06_the_assistant_text_never_reaches_the_fact_extractor(harness):
    memory, prompts = harness
    recall_answer = ("Here's what I have on file: pet-friendliness is a non-negotiable "
                     "dealbreaker for you, and you want the cheapest viable option.")
    memory.remember_turn("What do you remember about me?", recall_answer,
                         session_id="s", user_id="u_r206")
    extractor = [p for p in prompts if '"facts"' in p]
    assert extractor and all("on file" not in p and "cheapest viable" not in p
                             for p in extractor)
    assert not any("dealbreaker" in d for d in _docs(memory, "u_r206", "semantic"))


def test_r2_20_an_unstated_rationale_is_not_stored(harness):
    memory, _prompts = harness
    memory.remember_turn("1-bed in Camden, no commute", "Noted.", session_id="s",
                         user_id="u_r220")
    semantic = _docs(memory, "u_r220", "semantic")
    assert semantic == ["Wants no commute"]


def test_r2_20_rationale_helper_keeps_what_the_user_said():
    strip = am_mod.strip_unstated_rationale
    assert strip("Wants no commute (to live very close to their destination)", "no commute") == \
        "Wants no commute"
    assert strip("Needs a ground-floor flat because of their bike", "ground floor flat for my bike") \
        == "Needs a ground-floor flat"
    assert strip("Needs a quiet flat (works night shifts)", "quiet flat, I work night shifts") == \
        "Needs a quiet flat (works night shifts)"


def test_r2_06_reflections_are_not_built_from_reflections(harness):
    memory, prompts = harness
    for index in range(4):
        memory.add(f"User asked: does it allow pets? ({index})", "episodic", "s", "u_loop",
                   importance=5)
    memory._accum[("u_loop", "s")] = am_mod.REFLECT_IMPORTANCE_THRESHOLD
    memory.maybe_reflect("s", "u_loop")
    memory._accum[("u_loop", "s")] = am_mod.REFLECT_IMPORTANCE_THRESHOLD
    memory.maybe_reflect("s", "u_loop")
    reflections = _docs(memory, "u_loop", "reflection")
    assert reflections and not any("non-negotiable" in r for r in reflections)
    corpus_prompts = [p for p in prompts if '"insights"' in p]
    assert all("likely a dealbreaker" not in p for p in corpus_prompts)


def test_r2_06_inferences_are_labelled_apart_in_the_prompt(harness):
    memory, _prompts = harness
    block = memory.format_for_prompt([
        {"text": "I have a cat and I work at Canary Wharf", "mtype": "semantic"},
        {"text": "Pet-friendliness is a key requirement", "mtype": "reflection"},
    ])
    stated, inferred = block.split("The assistant's own inferences", 1)
    assert "[FACT] I have a cat" in stated and "Pet-friendliness" not in stated
    assert "NOT said or saved by the user" in inferred and "[INFERENCE] Pet-friendliness" in inferred
    assert "INSIGHT" not in block


def test_r2_06_a_reflection_is_never_restored_as_saved_criteria():
    from core.constraint_updates import restore_saved_criteria
    memories = [{"id": "r1", "mtype": "reflection", "created_at": "2026-10-19T21:47",
                 "text": "User has a fixed budget of around £2,300/month for a 1 bed flat"}]
    assert restore_saved_criteria({}, memories, "Use my saved preferences and search") == {}


def test_r2_20_recall_questions_are_recognised():
    import app as appmod
    for message in ("What do you remember about me?", "Do you store anything about me?",
                    "What have you saved about me so far?", "你记得我的哪些偏好？"):
        assert appmod._asks_what_is_remembered(message), message
    assert not appmod._asks_what_is_remembered("Find a 1-bed in Camden")
