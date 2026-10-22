"""In-budget listings with an unverified commute must stay visible (2026-10-19 B-04).

"I want a 1-bed in Islington under £2,600 a month ... commuting to King's Cross, ideally under
20 minutes": the search returned King Square £1,800 and St Clements Street £2,500 (in budget,
commute lookup failed) plus Masons Yard £2,701 (over budget). The graph's own answer listed the
two as "Not verified", but ``_structured_search_tool_data`` built the panel from the over-budget
alternative ALONE whenever there was no eligible row, and app.py re-validates exactly that panel
— so the user was told no listing qualified and shown only the one they could not afford.
"""
from __future__ import annotations

from core.candidate_validation import render_candidate_status, validate_candidates
from core.langgraph_agent import _structured_search_tool_data

_CRITERIA = {"area": "Islington", "areas": ["Islington"], "max_budget": 2600, "bedrooms": 1,
             "commute_destination": "King's Cross", "max_commute_time": 20}


def _row(address, price, url):
    return {"address": address, "price": f"£{price} pcm", "price_numeric": price,
            "monthly_price": price, "bedrooms": 1, "area": "Islington", "url": url,
            "property_type": "flat"}


def _payload():
    return {"status": "found", "search_criteria": dict(_CRITERIA),
            "recommendations": [_row("King Square, London EC1V", 1800, "https://otm.example/1"),
                                _row("St. Clements Street, London N7", 2500,
                                     "https://otm.example/2")],
            "over_budget_alternatives": [_row("Masons Yard, London EC1V", 2701,
                                              "https://otm.example/3")]}


def test_the_panel_keeps_unverified_in_budget_listings_first():
    _, tool_data = _structured_search_tool_data(_payload(), {}, commute_evidence=[])
    addresses = [row["address"] for row in tool_data["recommendations"]]
    assert addresses == ["King Square, London EC1V", "St. Clements Street, London N7",
                         "Masons Yard, London EC1V"]
    statuses = [row.get("candidate_status") for row in tool_data["recommendations"]]
    assert statuses[:2] == ["unknown", "unknown"] and statuses[2] == "excluded"
    assert tool_data["eligible_recommendations"] == []      # nothing became "eligible"


def test_the_ledger_app_py_rebuilds_from_the_panel_still_says_not_verified():
    """The exact re-validation app.py runs on tool_data['recommendations']."""
    _, tool_data = _structured_search_tool_data(_payload(), {}, commute_evidence=[])
    ledger = validate_candidates(tool_data["recommendations"],
                                 dict(_CRITERIA, max_budget_monthly=2600),
                                 tool_data.get("commute_evidence") or [])
    text = render_candidate_status(ledger)
    assert "Not verified:" in text
    assert "King Square" in text and "St. Clements Street" in text
    assert "Masons Yard, London EC1V: monthly price 2701 exceeds budget 2600" in text
    assert not ledger["eligible"]


def test_an_over_budget_only_search_is_unchanged():
    payload = _payload()
    payload["recommendations"] = []
    _, tool_data = _structured_search_tool_data(payload, {}, commute_evidence=[])
    assert [row["address"] for row in tool_data["recommendations"]] == ["Masons Yard, London EC1V"]
