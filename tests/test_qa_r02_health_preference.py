"""A stored soft preference keeps the housing requirement, not the health reason (R2-04).

Live: after "...I have a health condition (asthma), so I need a mould-free flat...",
``view_state.criteria.soft_preferences`` was ["mould-free due to asthma"] while the memory layer
redacted the same clause and the assistant told the user asthma was "only reflected
indirectly".
"""
from __future__ import annotations

import uuid

import pytest

from rag.agent_memory import sanitize_soft_preferences, strip_health_reason


@pytest.mark.parametrize("text,kept", [
    ("mould-free due to asthma", "mould-free"),
    ("Mould-free flat because of my asthma", "Mould-free flat"),
    ("quiet flat (I have anxiety)", "quiet flat"),
    ("no damp or mould, since I have asthma", "no damp or mould"),
    ("mould-free due to asthma and close to a GP", "mould-free and close to a GP"),
    ("无霉菌，因为我有哮喘", "无霉菌"),
    ("near a park", "near a park"),                 # no health term: unchanged
    ("good for families", "good for families"),
])
def test_r2_04_health_reason_is_dropped_requirement_kept(text, kept):
    assert strip_health_reason(text) == kept


def test_r2_04_a_preference_that_is_only_health_is_dropped():
    assert strip_health_reason("I have asthma") is None
    assert sanitize_soft_preferences(["I have asthma", "mould-free due to asthma", "mould-free",
                                      "near a park"]) == ["mould-free", "near a park"]
    assert sanitize_soft_preferences("mould-free due to asthma") == ["mould-free"]


def test_r2_04_conversation_criteria_store_the_requirement_only(monkeypatch):
    import app as appmod
    from uk_rent_agent.web.session_store import SessionStore

    monkeypatch.setattr(appmod, "_session_store", SessionStore())
    user, conversation = "u" + uuid.uuid4().hex[:16], "c" + uuid.uuid4().hex[:16]
    criteria = {"area": "Hackney", "max_budget": 1500,
                "soft_preferences": ["mould-free due to asthma", "near a park"]}
    appmod._write_back_turn(user, conversation, "I need a mould-free flat (asthma)",
                            "Noted.", [], accumulated_search_criteria=criteria)
    stored = appmod._get_session(user, conversation).persistent_state["accumulated_search_criteria"]
    assert stored["soft_preferences"] == ["mould-free", "near a park"]
    assert stored["area"] == "Hackney" and stored["max_budget"] == 1500
    assert criteria["soft_preferences"] == ["mould-free due to asthma", "near a park"]  # not mutated
