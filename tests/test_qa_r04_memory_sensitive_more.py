"""R4-08: automatic memory drops disability, state benefits and children's names/schools.

r07 inst-A stored 'Uses a wheelchair', 'User has a disability.', 'User is on Universal
Credit.', 'Has a son, Tom, who attends Oakfield Primary' and reflections such as
'Accessibility is likely a non-negotiable requirement…' / 'Affordability is tied to
Universal Credit…', all with pii_categories none; the episodic row kept "my son Tom attends
Oakfield Primary". Explicit remember stays allowed; a stated housing requirement (step-free
access, a lift) is not a sensitive category.
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
USER = "user-r4-08"
MESSAGE = ("I use a wheelchair, I am on Universal Credit; my son Tom attends Oakfield Primary. "
           "Find me a 2-bed in Hackney under £1,700.")
LEAKS = ("wheelchair", "Universal Credit", "Tom", "Oakfield", "disab")


@pytest.mark.parametrize("text,category", [
    ("Uses a wheelchair", "disability"),
    ("User has a disability.", "disability"),
    ("User is on Universal Credit.", "benefits"),
    ("I am on PIP and housing benefit", "benefits"),
    ("Has a son, Tom, who attends Oakfield Primary", "children"),
    ("我坐轮椅", "disability"),
])
def test_new_categories_are_sensitive(text, category):
    redacted, found = am_mod.redact_sensitive(text)
    assert category in found
    assert am_mod.contains_sensitive(text)
    for leak in ("wheelchair", "Universal Credit", "PIP", "Tom", "Oakfield", "轮椅"):
        assert leak not in redacted


@pytest.mark.parametrize("text", ["User wants step-free access", "Needs a wheelchair-accessible flat with a lift",
                                  "Wants a 2-bed near a good primary school", "The user has a son.",
                                  "My son is 5 and we need 2 bedrooms"])
def test_housing_requirements_and_household_size_stay(text):
    assert am_mod.redact_sensitive(text) == (text, [])


@pytest.fixture
def memory(tmp_path, monkeypatch):
    def fake_llm(prompt, *a, **k):
        if '"facts"' in prompt:
            return json.dumps({"facts": ["Uses a wheelchair", "User is on Universal Credit.",
                                         "Has a son, Tom, who attends Oakfield Primary",
                                         "Wants a 2-bed in Hackney under £1,700"]})
        if '"ops"' in prompt:
            facts = json.loads(prompt.split("NEW FACTS:\n", 1)[1].split("\n\nJSON:", 1)[0])
            return json.dumps({"ops": [{"event": "ADD", "id": None, "text": f} for f in facts]})
        if '"insights"' in prompt:
            return json.dumps({"insights": ["Accessibility is likely a non-negotiable requirement",
                                            "Affordability is tied to Universal Credit",
                                            "Hackney 2-beds under £1,700 are the focus"]})
        return "5"

    monkeypatch.setattr(am_mod, "call_ollama", fake_llm)
    return am_mod.AgentMemory(db_path=str(tmp_path / "mem"))


def test_no_layer_keeps_the_sensitive_details(memory):
    assert memory.remember_turn(MESSAGE, "Noted.", session_id="s1", user_id=USER)
    memory._accum[(USER, "s1")] = am_mod.REFLECT_IMPORTANCE_THRESHOLD
    for index in range(4):
        memory.remember_turn(f"Still Hackney 2-bed under £1,700 ({index}).", "OK", session_id="s1",
                             user_id=USER)
    memory._accum[(USER, "s1")] = am_mod.REFLECT_IMPORTANCE_THRESHOLD
    memory.maybe_reflect("s1", USER)
    got = memory.col.get(where={"user_id": USER})
    blob = "\n".join(got["documents"])
    for leak in LEAKS:
        assert leak.lower() not in blob.lower(), (leak, blob)
    assert "Hackney" in blob
    assert "Accessibility is likely" not in blob and "tied to Universal Credit" not in blob


def test_explicit_remember_is_still_allowed(tmp_path, monkeypatch):
    import core.tools.memory_tools as mt
    store = am_mod.AgentMemory(db_path=str(tmp_path / "mem2"))
    monkeypatch.setattr(mt, "_mem", lambda: store)
    monkeypatch.setattr(mt, "_turn_guard_flagged", lambda: False, raising=False)
    result = mt.remember_impl(content="I use a wheelchair", kind="semantic", session_id="s", user_id=USER)
    assert result["success"] is True
    assert "I use a wheelchair" in store.col.get(where={"user_id": USER})["documents"]
