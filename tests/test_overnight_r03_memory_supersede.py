"""R3-02: an elliptical correction supersedes the stored criterion instead of sitting beside it.

r05 g11: "Find a studio in Whitechapel under £1,700" then "Make that Poplar, budget £1,500"
left 'Find a studio in Whitechapel', 'Budget under £1,500' and a separate 'Poplar' in
semantic memory, and the next conversation recalled "a studio in Whitechapel; budget under
£1,500" — a combination the user never asked for. R2-06 stands: nothing is extracted from
assistant prose; the turn's criteria CHANGES (system state) are labelled context.
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
USER = "user-r3-02-supersede"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    prompts: list[str] = []

    def fake_llm(prompt, *a, **k):
        prompts.append(prompt)
        if '"facts"' in prompt:
            user = prompt.rsplit("User: ", 1)[1].split("\n\nJSON:", 1)[0]
            if "Whitechapel" in user:
                return json.dumps({"facts": ["Find a studio in Whitechapel", "Budget under £1,700"]})
            if "Poplar" in user:
                # the extractor still sees only the elliptical message's words
                return json.dumps({"facts": ["Poplar", "Budget under £1,500"]})
            if "dog" in user:
                return json.dumps({"facts": ["Has a dog"]})
            return json.dumps({"facts": []})
        if '"ops"' in prompt:
            facts = json.loads(prompt.split("NEW FACTS:\n", 1)[1].split("\n\nJSON:", 1)[0])
            existing = json.loads(prompt.split("EXISTING:\n", 1)[1].split("\n\nNEW FACTS:", 1)[0])
            ops = []
            for fact in facts:
                if any(fact.lower() in e["text"].lower() for e in existing):
                    ops.append({"event": "NOOP", "id": None, "text": fact})
                else:
                    ops.append({"event": "ADD", "id": None, "text": fact})
            return json.dumps({"ops": ops})
        if '"insights"' in prompt:
            return json.dumps({"insights": []})
        return "5"

    monkeypatch.setattr(am_mod, "call_ollama", fake_llm)
    return am_mod.AgentMemory(db_path=str(tmp_path / "mem")), prompts


def _semantic(memory):
    got = memory.col.get(where={"$and": [{"user_id": USER}, {"mtype": "semantic"}]})
    return dict(zip(got["ids"], zip(got["documents"], got["metadatas"])))


def test_criteria_changes_are_derived_deterministically():
    before = {"area": "Whitechapel", "areas": ["Whitechapel"], "max_budget": 1700, "room_type": "studio"}
    after = {"area": "Poplar", "areas": ["Poplar"], "max_budget": 1500, "room_type": "studio"}
    assert am_mod.criteria_changes(before, after) == {
        "area": {"from": ["Whitechapel"], "to": ["Poplar"]},
        "budget": {"from": 1700, "to": 1500},
    }
    # a newly set slot supersedes nothing
    assert am_mod.criteria_changes({}, after) == {}
    assert am_mod.criteria_changes(None, None) == {}


def test_elliptical_correction_replaces_the_old_area_and_budget(harness):
    memory, prompts = harness
    memory.remember_turn("Find a studio in Whitechapel under £1,700", "", user_id=USER,
                         turn_id="t1")
    changes = am_mod.criteria_changes(
        {"areas": ["Whitechapel"], "max_budget": 1700}, {"areas": ["Poplar"], "max_budget": 1500})
    memory.remember_turn("Make that Poplar, budget £1,500", "Sure — here are studios in Poplar.",
                         user_id=USER, turn_id="t2", criteria_changes=changes)
    docs = [doc for doc, _ in _semantic(memory).values()]
    joined = " | ".join(docs)
    assert "Whitechapel" not in joined, docs
    assert "£1,700" not in joined, docs
    assert any("studio in Poplar" in d for d in docs), docs
    # the rewritten rows say when, and by which turn
    updated = [meta for doc, meta in _semantic(memory).values() if "studio in Poplar" in doc]
    assert updated and updated[0].get("updated_at") and updated[0].get("updated_turn_id") == "t2"
    # R2-06: the assistant's text never reaches the extractor; the change block is labelled
    extract = [p for p in prompts if '"facts"' in p][-1]
    assert "here are studios" not in extract
    assert "RESOLVED CRITERIA CHANGES THIS TURN" in extract and "Whitechapel -> Poplar" in extract
    consolidate = [p for p in prompts if '"ops"' in p][-1]
    assert "SEARCH CRITERIA CHANGED THIS TURN" in consolidate


def test_added_area_does_not_supersede_the_old_one(harness):
    memory, _ = harness
    memory.remember_turn("Find a studio in Whitechapel under £1,700", "", user_id=USER)
    changes = am_mod.criteria_changes({"areas": ["Whitechapel"]}, {"areas": ["Whitechapel", "Poplar"]})
    memory.remember_turn("Also look in Poplar please", "", user_id=USER, criteria_changes=changes)
    assert any("Whitechapel" in doc for doc, _ in _semantic(memory).values())


def test_without_changes_nothing_is_rewritten(harness):
    memory, prompts = harness
    memory.remember_turn("Find a studio in Whitechapel under £1,700", "", user_id=USER)
    memory.remember_turn("Make that Poplar, budget £1,500", "", user_id=USER)
    assert all("RESOLVED CRITERIA" not in p for p in prompts)


@pytest.mark.parametrize("message", ["I have a dog", "I am vegan", "I'm a student", "My cat is old"])
def test_short_first_person_statement_is_not_trivial(message):
    assert am_mod._is_trivial_for_extraction(message) is False


@pytest.mark.parametrize("message", ["ok thanks", "hello there", "sounds good"])
def test_short_acknowledgements_stay_trivial(message):
    assert am_mod._is_trivial_for_extraction(message) is True


def test_memory_job_payload_carries_criteria_changes():
    import app as app_module
    changes = app_module._memory_criteria_changes(
        {"areas": ["Whitechapel"], "max_budget": 1700}, {"areas": ["Poplar"], "max_budget": 1500})
    assert changes["area"] == {"from": ["Whitechapel"], "to": ["Poplar"]}
    assert app_module._memory_criteria_changes(object(), None) == {}
