"""R4-05: a new area or budget named in this turn's message replaces the old slot.

r07 B/D: "Search 1-bed flats in Holborn under £2,500" searched Camden/Edinburgh; "find a
studio in Bloomsbury" searched Oxford; "What about … at £900?" kept 1500; "预算改成2000，
再看Bloomsbury" kept 1600 (2000 was masked as a calendar year).
"""
from __future__ import annotations

import pytest

from core.langgraph_agent import _apply_explicit_criteria_updates


@pytest.mark.parametrize("before,message,area", [
    ("Camden", "Search 1-bed flats in Holborn under £2,500", "Holborn"),
    ("Edinburgh", "Search 1-bed flats in Holborn under £2,500", "Holborn"),
    ("Oxford", "Change of plan, find a studio in Bloomsbury under £2200", "Bloomsbury"),
    ("Oxford", "在Bloomsbury找个studio", "Bloomsbury"),
    ("Camden", "Now show me Islington ones", "Islington"),
    ("Edinburgh", "find a 1-bed in Bloomsbury", "Bloomsbury"),
])
def test_named_area_replaces_the_previous_one(before, message, area):
    out = _apply_explicit_criteria_updates({"area": before, "areas": [before]}, message)
    assert out["area"] == area and out["areas"] == [area]


@pytest.mark.parametrize("message", ["I work in Holborn", "take a bow"])
def test_non_search_mentions_do_not_move_the_area(message):
    out = _apply_explicit_criteria_updates({"area": "Camden", "areas": ["Camden"]}, message)
    assert out.get("area") == "Camden"


@pytest.mark.parametrize("before,message,budget", [
    (1500, "What about 3 bedrooms in Edinburgh at £900?", 900),
    (1500, "What about Camden at £900?", 900),
    (1600, "预算改成2000，再看Bloomsbury", 2000),
    (1600, "预算是2000", 2000),
])
def test_stated_budget_replaces_the_previous_one(before, message, budget):
    out = _apply_explicit_criteria_updates({"area": "Camden", "areas": ["Camden"], "max_budget": before},
                                           message)
    assert out["max_budget"] == budget


@pytest.mark.parametrize("message", ["Is the one at £2,000 still available?",
                                     "the flat at £1,850 looks good, what about pets?",
                                     "I moved to London in 2000"])
def test_listing_prices_and_years_are_not_budgets(message):
    out = _apply_explicit_criteria_updates({"area": "Camden", "areas": ["Camden"], "max_budget": 1500},
                                           message)
    assert out.get("max_budget") == 1500
