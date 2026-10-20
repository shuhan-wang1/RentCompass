"""Automatic memory must not keep what the assistant says it does not keep (2026-10-19 A-09).

The tester said: "For context only, no action needed: I earn £62,000 a year, I'm on a Skilled
Worker visa, I have asthma, my phone is 07700 900123 and my email is a.test@example.com." The
assistant replied that it would not store health, immigration, salary or contact details; the
episodic layer stored the message verbatim ("User asked: <first 300 chars>", importance 5, no
TTL, retrievable by ``retrieve()``), tagged only ``financial_preference``.
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

A09 = ("For context only, no action needed: I earn £62,000 a year, I'm on a Skilled Worker "
       "visa, I have asthma, my phone is 07700 900123 and my email is a.test@example.com.")
_LEAKS = ("62,000", "Skilled Worker", "visa", "asthma", "07700", "900123", "a.test@example.com",
          "mobility")


@pytest.fixture
def memory(tmp_path, monkeypatch):
    def fake_llm(prompt, *a, **k):
        if '"facts"' in prompt:
            # The extractor is the model: it may infer a sensitive fact anyway.
            return json.dumps({"facts": ["User earns £62,000 a year", "User has asthma",
                                         "User is on a Skilled Worker visa",
                                         "User wants a 1-bed flat in Hackney"]})
        if '"ops"' in prompt:
            facts = json.loads(prompt.split("NEW FACTS:\n", 1)[1].split("\n\nJSON:", 1)[0])
            return json.dumps({"ops": [{"event": "ADD", "id": None, "text": f} for f in facts]})
        if '"insights"' in prompt:
            return json.dumps({"insights": ["User's asthma suggests a ground-floor flat",
                                            "Needing a lift suggests a mobility impairment",
                                            "User prioritises Hackney"]})
        return "5"

    monkeypatch.setattr(am_mod, "call_ollama", fake_llm)
    return am_mod.AgentMemory(db_path=str(tmp_path / "mem"))


def _all_text(memory, user):
    got = memory.col.get(where={"user_id": user})
    return list(got.get("documents") or []), list(got.get("metadatas") or [])


def test_the_a09_message_leaves_no_sensitive_detail_in_any_layer(memory):
    assert memory.remember_turn(
        A09 + " I'm looking for a 1-bed flat in Hackney.", "Noted.", session_id="s1",
        user_id="u_a09")
    for index in range(3):
        memory.remember_turn(f"Still Hackney, 1-bed, under £1,900 a month ({index}).", "OK",
                             session_id="s1", user_id="u_a09")
    memory._accum[("u_a09", "s1")] = am_mod.REFLECT_IMPORTANCE_THRESHOLD   # force a reflection
    memory.maybe_reflect("s1", "u_a09")
    docs, metas = _all_text(memory, "u_a09")
    blob = "\n".join(docs)
    assert docs, "the housing parts must still be remembered"
    for leak in _LEAKS:
        assert leak.lower() not in blob.lower(), leak
    assert "Hackney" in blob
    assert any(m.get("mtype") == "reflection" for m in metas)     # the layer ran, filtered


@pytest.mark.parametrize("text,expected,categories", [
    # R4-08 (policy change): using a wheelchair is a statement about the person, not a
    # housing requirement, so automatic memory drops that clause; the step-free requirement
    # stays a search criterion of the turn.
    ("I want a 1-bed in Hackney under £1,900 a month, I use a wheelchair so I need "
     "step-free access",
     "I want a 1-bed in Hackney under £1,900 a month, [personal detail not stored]", ["disability"]),
    ("I want a 1-bed in Hackney under £1,900 a month, I need step-free access",
     "I want a 1-bed in Hackney under £1,900 a month, I need step-free access", []),
    ("My budget is £1,800 pcm, call me on +44 7700 900123",
     "My budget is £1,800 pcm, call me on [contact detail not stored]", ["contact"]),
    ("Find a flat near NW1 8NH for £2000", "Find a flat near NW1 8NH for £2000", []),
])
def test_redaction_keeps_housing_requirements_intact(text, expected, categories):
    redacted, found = am_mod.redact_sensitive(text)
    assert redacted == expected and found == categories


def test_the_categories_are_recorded_for_what_is_left():
    assert "health" in am_mod._classify_pii("I have asthma")
    assert am_mod.contains_sensitive("User is on a Skilled Worker visa")
    assert not am_mod.contains_sensitive("User wants a 1-bed flat in Hackney")


def test_a_tenancy_length_is_not_income_and_bracketed_phones_are_caught():
    assert am_mod.redact_sensitive("I want to rent for a year in Hackney, budget £1,800") == (
        "I want to rent for a year in Hackney, budget £1,800", [])
    redacted, found = am_mod.redact_sensitive("call (020) 7946 0958 please")
    assert "7946" not in redacted and found == ["contact"]


@pytest.mark.parametrize("text", ["a" * 60_000, "flat number " + "9" * 60_000, " " * 40_000 + "and I x",
                                  ("word " * 12_000) + "@"])
def test_redaction_is_linear_and_bounded(text):
    import time

    started = time.perf_counter()
    am_mod.redact_sensitive(text)
    assert time.perf_counter() - started < 0.5      # was 10.4 s for 60k chars
