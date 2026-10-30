"""Regression tests (V-xx ids in each test name)."""
from __future__ import annotations

import asyncio
import importlib
import uuid

import pytest


def _state_node(accumulated: dict, message: str, history=None) -> dict:
    from core.langgraph_agent import _make_extract_preferences_node
    state = {"user_preferences": {}, "accumulated_search_criteria": dict(accumulated),
             "extracted_context": {"current_message": message,
                                   "history": history or [{"user": "Find a flat", "assistant": "ok"}]}}
    update = _make_extract_preferences_node()(state)
    return update.get("accumulated_search_criteria", state["accumulated_search_criteria"])


_CAMDEN = {"area": "Camden", "areas": ["Camden"], "bedrooms": 1, "max_budget": 2200,
           "room_type": "whole", "max_travel_time": 30, "commute_destination": "King's Cross"}


# V-01 ── a place followed by "here" is no name
@pytest.mark.parametrize("message", [
    "Camden here", "Hi, Camden here", "Camden here, looking for a flat", "This is Hackney here",
    "This is Hackney", "King's Cross here", "Camden Town here", "Bow here", "Oxford here",
])
def test_v_01_a_curated_place_is_not_a_name(message):
    from rag.agent_memory import redact_sensitive
    assert redact_sensitive(message) == (message, [])


@pytest.mark.parametrize("message", [
    "Sarah here", "Hi, Sarah here, looking for a flat", "This is Sarah speaking",
    "I'm Chelsea, looking for a flat", "This is Chelsea speaking",
])
def test_v_01_a_person_stays_withheld(message):
    from rag.agent_memory import redact_sensitive
    text, found = redact_sensitive(message)
    assert "[personal detail not stored]" in text and found == ["identity"]


# V-02 ── a reset naming a place clears the rest and keeps the place
@pytest.mark.parametrize("message", [
    "Can we start from scratch with Hackney?", "Start from scratch with Hackney",
    "start over in Hackney", "start over from Hackney", "Start again with Hackney",
    "Could we restart with Hackney?", "restart the search in Hackney",
    "Let's start from scratch in Hackney",
])
def test_v_02_a_reset_keeps_the_place_it_names(message):
    out = _state_node(_CAMDEN, message)
    assert out["area"] == "Hackney" and out["areas"] == ["Hackney"]
    assert out["max_budget"] is None and out["bedrooms"] is None
    assert out["commute_destination"] is None and out["max_travel_time"] is None


@pytest.mark.parametrize("message", ["Start from scratch with a 2-bed", "Start over with a 2-bed"])
def test_v_02_what_follows_with_may_be_no_place(message):
    out = _state_node(_CAMDEN, message)
    assert out["area"] is None and out["bedrooms"] == 2


@pytest.mark.parametrize("message", ["I start again in May", "We start over in September"])
def test_v_02_a_date_after_start_again_is_no_reset(message):
    assert _state_node(_CAMDEN, message)["area"] == "Camden"
