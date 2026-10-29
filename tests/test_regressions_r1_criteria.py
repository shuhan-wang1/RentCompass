"""Criteria-store regressions (R1-xx).

Each test reproduces a turn sequence run against the served loop, at the
unit level: the accumulated criteria before the turn, the user's message, and what the
criteria store must hold afterwards.
"""
from __future__ import annotations

import pytest

from core.langgraph_agent import _apply_explicit_criteria_updates

CW = "Canary Wharf, London E14 5AB"
WITH_CW = {"area": "Bethnal Green", "bedrooms": 1, "max_budget": 2200,
           "commute_destination": CW, "destination": CW}


# R1-01 ── a corrected workplace replaces the destination ─────────────────────────────
@pytest.mark.parametrize("message, destination", [
    ("Actually I work in King's Cross not Canary Wharf.", "King's Cross"),
    ("Actually, I work in Shoreditch rather than Canary Wharf.", "Shoreditch"),
    ("I work at Bank instead of Canary Wharf now.", "Bank"),
    ("Actually I work at King's Cross, not Canary Wharf.", "King's Cross"),
])
def test_workplace_correction_replaces_the_old_destination(message, destination):
    result = _apply_explicit_criteria_updates(dict(WITH_CW), message)
    assert result["commute_destination"] == destination
    assert result["destination"] == destination


def test_workplace_named_first_and_rejected_place_second_keeps_the_first():
    # The place after "not" is the rejected one, whichever was stored before.
    start = dict(WITH_CW, commute_destination="King's Cross", destination="King's Cross")
    result = _apply_explicit_criteria_updates(start, "My office is in Canary Wharf, not King's Cross.")
    assert result["commute_destination"] == CW


def test_place_names_containing_not_are_not_cut():
    result = _apply_explicit_criteria_updates({}, "I work in Notting Hill.")
    assert result["commute_destination"] == "Notting Hill"
