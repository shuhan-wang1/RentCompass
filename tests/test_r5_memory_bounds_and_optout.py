"""Stress A-08 re-verified on b8d7632: background consolidation must not weaken a stated bound,
and a turn the user asked not to be remembered stores nothing.

Recorded (stress inst-A, alice, rid 94982f08…): after "my budget is at most £1,900 a month …" the
message below was consolidated into "Budget: up to £2,100 per month rent for the right place." and
the next "Use my saved defaults" search ran with max_budget 2100. On b8d7632 the explicitly saved
row is protected (R4-02), but an AUTO-extracted ceiling is still rewritten by the same UPDATE, and
the criteria parser turns the message into area Hackney -> Peckham and budget 1900 -> 2100, which
the deterministic supersede then wrote into the auto rows ("1-bed whole flat in Peckham").
r07 inst-A optout1: "Please do not store or remember anything about me in this chat — no memory
at all. …" was kept verbatim in the turn log, with nothing stopping fact extraction.
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
USER = "user-r5-a08"

A08_MESSAGE = (
    "More background, still nothing to search: I prefer the Overground to buses, I need fast "
    "broadband for streaming, I'd like a dishwasher, wooden floors are better for my dust allergy, "
    "my friend lives in Peckham, I'm moving in March, my budget could stretch to £2,100 for the "
    "right place, and I study part-time at Birkbeck.")
A08_SAVED = (
    "Budget: at most £1,900 per month rent. Needs a 1-bedroom whole flat (self-contained, not "
    "shared) in Hackney, London. Has a cat named Quillbert7F3A91, so pets (cats) must be allowed. "
    "Works at Canary Wharf, London E14 5AB, 3 days per week (hybrid).")
A08_CONSOLIDATED = "Budget: up to £2,100 per month rent for the right place."
A08_FACTS = ["User prefers Overground over buses.", "User has a friend living in Peckham.",
             "User's budget could stretch to £2,100 for the right place.",
             "User studies part-time at Birkbeck."]
# What b8d7632's own criteria parser derives from A08_MESSAGE when the conversation already held
# Hackney / £1,900 / Canary Wharf (core.langgraph_agent._apply_explicit_criteria_updates).
A08_CHANGES = {"area": {"from": ["Hackney"], "to": ["Peckham"]},
               "budget": {"from": 1900, "to": 2100},
               "commute_destination": {"from": "Canary Wharf, London E14 5AB",
                                       "to": "Malet Street, Bloomsbury, London WC1E 7HX"}}
OPTOUT_MESSAGES = (
    "Please do not store or remember anything about me in this chat — no memory at all. I am HIV "
    "positive and my wife Sarah is pregnant. I want a 1-bed in Camden under £2,000.",
    "I also have a criminal record from 2019. Please do not store that.",
    "Do not store any of that personal info please. Just search, do not keep anything about me.",
    "请不要记住这些：我想在Camden找一居室，预算2000英镑。",
)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    prompts: list[str] = []
    script: dict = {"facts": [], "ops": None}

    def fake_llm(prompt, *a, **k):
        prompts.append(prompt)
        if '"facts"' in prompt:
            return json.dumps({"facts": list(script["facts"])})
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
    return am_mod.AgentMemory(db_path=str(tmp_path / "mem")), prompts, script


def _semantic(memory):
    got = memory.col.get(where={"$and": [{"user_id": USER}, {"mtype": "semantic"}]})
    return {d: (m or {}) for d, m in zip(got["documents"], got["metadatas"])}


def _auto(memory, text):
    memory.add(text, "semantic", session_id=USER, user_id=USER,
               extra_meta={"source": am_mod.SOURCE_AUTO})


def _recorded_update(facts, existing):
    """The recorded consolidation: fold the stretch into the nearest budget row."""
    ops = [{"event": "UPDATE", "id": row["id"], "text": A08_CONSOLIDATED}
           for row in existing if "1,900" in row["text"]]
    return ops + [{"event": "ADD", "id": None, "text": f} for f in facts if "2,100" not in f]


@pytest.mark.parametrize("changes", [None, A08_CHANGES], ids=["new-conversation", "same-conversation"])
def test_a08_a_conditional_stretch_never_loosens_a_stored_ceiling(harness, changes):
    memory, _prompts, script = harness
    memory.add(A08_SAVED, "semantic", session_id=USER, user_id=USER,
               extra_meta={"source": am_mod.SOURCE_EXPLICIT})
    _auto(memory, "Budget is at most £1,900 a month")
    _auto(memory, "Wants a 1-bed whole flat in Hackney")
    script["facts"], script["ops"] = A08_FACTS, _recorded_update
    memory.remember_turn(A08_MESSAGE, "Noted.", session_id=USER, user_id=USER, turn_id="t12",
                         criteria_changes=changes)
    rows = _semantic(memory)
    assert A08_SAVED in rows and "Budget is at most £1,900 a month" in rows
    assert A08_CONSOLIDATED not in rows
    # the user's own words about the stretch are kept beside the ceiling, not instead of it
    assert "User's budget could stretch to £2,100 for the right place." in rows
    # "my friend lives in Peckham" is not a new search area
    assert "Wants a 1-bed whole flat in Hackney" in rows
    assert not any("Peckham" in d for d in rows if "friend" not in d)
    assert not any(m.get("updated_turn_id") for m in rows.values())


def test_a08_a_firmly_stated_new_ceiling_still_supersedes(harness):
    memory, _prompts, script = harness
    _auto(memory, "Budget is at most £1,900 a month")
    script["facts"] = ["Budget is at most £2,100 a month"]
    script["ops"] = lambda facts, existing: [
        {"event": "UPDATE", "id": row["id"], "text": "Budget is at most £2,100 a month"}
        for row in existing if "1,900" in row["text"]]
    memory.remember_turn("My budget is now £2,100 a month.", "", session_id=USER, user_id=USER,
                         turn_id="t2", criteria_changes={"budget": {"from": 1900, "to": 2100}})
    rows = _semantic(memory)
    assert "Budget is at most £2,100 a month" in rows and "Budget is at most £1,900 a month" not in rows
    assert rows["Budget is at most £2,100 a month"].get("updated_turn_id") == "t2"


def test_a08_a_ceiling_row_is_not_deleted_on_a_conditional_message(harness):
    memory, _prompts, script = harness
    _auto(memory, "Budget under £1,700")
    script["facts"] = ["Could go to £1,900 if the flat is perfect"]
    script["ops"] = lambda facts, existing: [
        {"event": "DELETE", "id": row["id"], "text": ""} for row in existing if "1,700" in row["text"]]
    memory.remember_turn("I could go to £1,900 if the flat is perfect", "", session_id=USER,
                         user_id=USER)
    assert "Budget under £1,700" in _semantic(memory)
    memory.remember_turn("No budget limit any more, show me everything", "", session_id=USER,
                         user_id=USER)
    assert "Budget under £1,700" not in _semantic(memory)


def test_a08_commute_minutes_are_a_bound_too():
    assert am_mod._loosened_bound("Maximum commute is 35 minutes", "Commute up to 45 minutes") == "minutes"
    assert am_mod._loosened_bound("Maximum commute is 35 minutes", "Commute under 30 minutes") is None
    assert am_mod._loosened_bound("Budget under £1,700", "Budget under £1,700, flexible") == "money"


_AREA = {"area": {"from": ["Hackney"], "to": ["Poplar"]}}
_RAISE = {"budget": {"from": 1900, "to": 2100}}


@pytest.mark.parametrize("message,changes,kept", [
    (A08_MESSAGE, A08_CHANGES, {"commute_destination"}),
    ("Make that Poplar, budget £1,500",
     {**_AREA, "budget": {"from": 1700, "to": 1500}}, {"area", "budget"}),
    ("My partner and I want to live in Poplar now", _AREA, {"area"}),
    ("ok what about Poplar then", _AREA, {"area"}),
    ("My sister lives in Poplar", _AREA, set()),
    ("I'd stretch to £2,100 at a push", _RAISE, set()),
    ("Raise the budget by £200", _RAISE, {"budget"}),
    ("Budget £2,100 now", _RAISE, {"budget"}),
])
def test_a08_only_the_users_own_criteria_changes_reach_memory(message, changes, kept):
    assert set(am_mod.user_stated_changes(changes, message)) == kept


@pytest.mark.parametrize("message", OPTOUT_MESSAGES)
def test_a_turn_the_user_asked_not_to_remember_stores_nothing(harness, message):
    memory, prompts, script = harness
    script["facts"] = ["Wants a 1-bed in Camden", "Budget under £2,000", "Has a criminal record"]
    assert memory.remember_turn(message, "I won't save anything.", session_id=USER, user_id=USER,
                                idempotency_key=f"k-{hash(message)}")
    got = memory.col.get(where={"user_id": USER})
    assert got["documents"] == [am_mod._OPTED_OUT_LOG]
    assert not any('"facts"' in p for p in prompts)
    # a retry of the same job is still recognised as done
    assert memory.remember_turn(message, "", session_id=USER, user_id=USER,
                                idempotency_key=f"k-{hash(message)}")
    assert len(memory.col.get(where={"user_id": USER})["ids"]) == 1


def test_a_named_field_in_a_do_not_remember_message_is_still_erased(harness):
    memory, _prompts, _script = harness
    memory.add("Budget is £1,400 a month", "semantic", session_id=USER, user_id=USER)
    memory.remember_turn("Please don't remember my budget any more.", "", session_id=USER,
                         user_id=USER)
    assert "Budget is £1,400 a month" not in _semantic(memory)


@pytest.mark.parametrize("message", [
    "I don't remember the postcode, somewhere near Angel",
    "Remember that I need parking",
    "Don't forget I have a cat",
    "Find a 1-bed in Camden; I want my details kept private? no, that's fine",
    "User asked: [personal detail not stored]. Find a step-free 1-bed",
])
def test_ordinary_messages_are_not_opt_outs(message):
    assert am_mod.do_not_remember_requested(message) is False


def test_bound_and_opt_out_parsing_stay_fast_on_long_runs():
    import time
    start = time.perf_counter()
    for text in ("1," * 30000, "1" * 60000, "£" * 30000 + "1", ". " + " " * 60000 + "x"):
        am_mod._row_bounds(text)
        am_mod._loosened_bound(text, text)
        am_mod._firm_bound_stated(text, "money", 2100)
        am_mod.do_not_remember_requested(text)
    assert time.perf_counter() - start < 2.0
