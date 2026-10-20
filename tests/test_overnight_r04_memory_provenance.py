"""R4-02: background consolidation never rewrites explicitly saved or third-party facts.

Regression from b19298f (R3-02). r07 A-F9: "Please remember that my guarantor is in
Manchester", later "my partner works in Canary Wharf" -> the saved row became "The user's
guarantor is in Canary Wharf." A-F12: a session budget change (-> £2,000) rewrote the
explicitly saved standing default (£1,900). The R3-02 g11 supersession of AUTO rows stays.
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
USER = "user-r4-02-provenance"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    prompts: list[str] = []
    script: dict = {"facts": {}, "ops": None}

    def fake_llm(prompt, *a, **k):
        prompts.append(prompt)
        if '"facts"' in prompt:
            user = prompt.rsplit("User: ", 1)[1].split("\n\nJSON:", 1)[0]
            for key, facts in script["facts"].items():
                if key in user:
                    return json.dumps({"facts": facts})
            return json.dumps({"facts": []})
        if '"ops"' in prompt:
            facts = json.loads(prompt.split("NEW FACTS:\n", 1)[1].split("\n\nJSON:", 1)[0])
            existing = json.loads(prompt.split("EXISTING:\n", 1)[1].split("\n\nNEW FACTS:", 1)[0])
            if script["ops"] is not None:
                return json.dumps({"ops": script["ops"](facts, existing)})
            return json.dumps({"ops": [{"event": "ADD", "id": None, "text": f} for f in facts]})
        if '"insights"' in prompt:
            return json.dumps({"insights": []})
        return "5"

    monkeypatch.setattr(am_mod, "call_ollama", fake_llm)
    memory = am_mod.AgentMemory(db_path=str(tmp_path / "mem"))
    return memory, prompts, script


def _docs(memory):
    got = memory.col.get(where={"$and": [{"user_id": USER}, {"mtype": "semantic"}]})
    return {i: (d, m) for i, d, m in zip(got["ids"], got["documents"], got["metadatas"])}


def test_explicit_third_party_fact_survives_an_area_change(harness):
    memory, _, script = harness
    memory.add("The user's guarantor is in Manchester.", "semantic", session_id=USER, user_id=USER,
               extra_meta={"source": am_mod.SOURCE_EXPLICIT})
    script["facts"] = {"partner": ["The user's partner works in Canary Wharf."]}
    changes = am_mod.criteria_changes({"areas": ["Manchester"]}, {"areas": ["Canary Wharf"]})
    memory.remember_turn("my partner works in Canary Wharf", "", user_id=USER, turn_id="t2",
                         criteria_changes=changes)
    docs = [d for d, _ in _docs(memory).values()]
    assert "The user's guarantor is in Manchester." in docs
    assert "The user's guarantor is in Canary Wharf." not in docs


def test_auto_third_party_fact_is_not_rewritten_by_a_criteria_change(harness):
    memory, _, script = harness
    memory.add("The user's guarantor is in Manchester.", "semantic", session_id=USER, user_id=USER,
               extra_meta={"source": am_mod.SOURCE_AUTO})
    changes = am_mod.criteria_changes({"areas": ["Manchester"]}, {"areas": ["Canary Wharf"]})
    memory.remember_turn("ok what about Canary Wharf then", "", user_id=USER, criteria_changes=changes)
    assert "The user's guarantor is in Manchester." in [d for d, _ in _docs(memory).values()]


def test_consolidator_cannot_update_or_delete_an_explicit_row(harness):
    memory, _, script = harness
    memory.add("My standing default budget is £1,900 a month.", "semantic", session_id=USER,
               user_id=USER, extra_meta={"source": am_mod.SOURCE_EXPLICIT})
    script["facts"] = {"2,000": ["Budget for this search is £2,000 a month."]}

    def ops(facts, existing):
        saved = [e for e in existing if "1,900" in e["text"]]
        assert saved and saved[0]["kind"] == "saved"
        return [{"event": "UPDATE", "id": saved[0]["id"], "text": "My standing default budget is £2,000 a month."},
                {"event": "DELETE", "id": saved[0]["id"], "text": ""}]
    script["ops"] = ops
    changes = am_mod.criteria_changes({"max_budget": 1900}, {"max_budget": 2000})
    memory.remember_turn("for this search use £2,000", "", user_id=USER, criteria_changes=changes)
    docs = [d for d, _ in _docs(memory).values()]
    assert "My standing default budget is £1,900 a month." in docs
    assert "My standing default budget is £2,000 a month." not in docs


def test_g11_auto_rows_are_still_superseded(harness):
    memory, _, script = harness
    script["facts"] = {"Whitechapel": ["Find a studio in Whitechapel", "Budget under £1,700"],
                       "Poplar": ["Poplar", "Budget under £1,500"]}

    def ops(facts, existing):
        return [{"event": "NOOP" if any(f.lower() in e["text"].lower() for e in existing) else "ADD",
                 "id": None, "text": f} for f in facts]
    script["ops"] = ops
    memory.remember_turn("Find a studio in Whitechapel under £1,700", "", user_id=USER, turn_id="t1")
    changes = am_mod.criteria_changes({"areas": ["Whitechapel"], "max_budget": 1700},
                                      {"areas": ["Poplar"], "max_budget": 1500})
    memory.remember_turn("Make that Poplar, budget £1,500", "", user_id=USER, turn_id="t2",
                         criteria_changes=changes)
    rows = _docs(memory)
    joined = " | ".join(d for d, _ in rows.values())
    assert "Whitechapel" not in joined and "£1,700" not in joined
    assert all(m.get("source") == am_mod.SOURCE_AUTO for _, m in rows.values())


def test_remember_tool_marks_its_row_explicit(tmp_path, monkeypatch):
    import core.tools.memory_tools as mt
    memory = am_mod.AgentMemory(db_path=str(tmp_path / "mem2"))
    monkeypatch.setattr(mt, "_mem", lambda: memory)
    monkeypatch.setattr(mt, "_turn_guard_flagged", lambda: False, raising=False)
    result = mt.remember_impl(content="My guarantor is in Manchester", kind="semantic", session_id=USER, user_id=USER)
    assert result["success"] is True
    rows = memory.col.get(where={"user_id": USER})
    assert rows["metadatas"][0]["source"] == am_mod.SOURCE_EXPLICIT
