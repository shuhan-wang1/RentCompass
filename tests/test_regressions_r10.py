"""Regression tests (G-xx ids in each test name)."""
from __future__ import annotations

import json

import pytest


def _state_node(accumulated: dict, message: str, history=None) -> dict:
    from core.langgraph_agent import _make_extract_preferences_node
    state = {"user_preferences": {}, "accumulated_search_criteria": dict(accumulated),
             "extracted_context": {"current_message": message,
                                   "history": history or [{"user": "Find a flat", "assistant": "ok"}]}}
    update = _make_extract_preferences_node()(state)
    return update.get("accumulated_search_criteria", state["accumulated_search_criteria"])


# G-01 ── a weekly rent is weekly however it is written
@pytest.mark.parametrize("text,expected", [
    ("Find me a 1-bed in Hackney, £450pw", (450, "week")),
    ("£600pw max in Camden", (600, "week")),
    ("£450 pcw", (450, "week")),
    ("450 pcw", (450, "week")),
    ("max £300 p.w.", (300, "week")),
    ("max £300 p.w. Looking in Camden.", (300, "week")),
    ("£300 p.c.w. in Hackney", (300, "week")),
    ("£350pw all bills", (350, "week")),
    ("£120 per wk", (120, "week")),
    ("£120 a wk", (120, "week")),
    ("£120 weekly", (120, "week")),
    ("450 weekly", (450, "week")),
    ("my weekly budget is £300", (300, "week")),
    ("£450 p/w", (450, "week")),
    ("£400 per week", (400, "week")),
    ("每周£350", (350, "week")),
    ("350镑/周", (350, "week")),
])
def test_g_01_a_weekly_figure_reads_weekly(text, expected):
    from core.tools.search_properties import _extract_budget
    assert _extract_budget(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("£1800 p.c.m.", (1800, "month")),
    ("£1,800pcm", (1800, "month")),
    ("£1500/month", (1500, "month")),
    ("1500 pm", (1500, "month")),
    ("under 1800 pcm", (1800, "month")),
    ("budget £1,800 pcm, gym once a week", (1800, "month")),
    ("budget £1500 and I go to the gym weekly", (1500, "month")),
    ("budget £1500, gym twice a week", (1500, "month")),
    ("£2000 a month, three days a week in the office", (2000, "month")),
    ("预算每月1500，我每周去两次健身房", (1500, "month")),
])
def test_g_01_a_monthly_figure_stays_monthly(text, expected):
    from core.tools.search_properties import _extract_budget
    assert _extract_budget(text) == expected


def test_g_01_a_weekly_correction_moves_the_ceiling():
    after = _state_node({"area": "Hackney", "areas": ["Hackney"], "max_budget": 1500, "budget_period": "month"},
                        "Actually I can stretch to £480pw.")
    assert after["max_budget"] == 2080


@pytest.mark.parametrize("text,expected", [
    ("Actually I can stretch to £2,000.", (2000, "month")),
    ("I can stretch to £480pw", (480, "week")),
    # A stretch beside a ceiling, or with a condition, stays a note.
    ("My budget is £1,900, but I can stretch to £2,100.", (1900, "month")),
    ("I can stretch to £2,000 if needed", (None, None)),
    ("I could stretch to £2,000", (None, None)),
])
def test_g_01_a_plain_can_stretch_is_the_new_ceiling(text, expected):
    from core.tools.search_properties import _extract_budget
    assert _extract_budget(text) == expected


@pytest.mark.parametrize("text", ["raise it by £50pw", "raise it by £50 p.w.", "raise it by £50 per wk"])
def test_g_01_a_weekly_delta_is_converted(text):
    from core.search_readiness import detect_relative_budget_delta
    assert detect_relative_budget_delta(text) == pytest.approx(216.67, abs=0.01)
