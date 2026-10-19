"""Destination fragments and budget self-corrections (2026-10-19 A-03, A-05) — targeted only.

* A-03 — "I work at King's Cross, so I'd like a commute of under 30 minutes" stored the
  destination "King's Cross, so I'd like a commute of"; "I work from home so I don't commute at
  all" stored "all"; the correction "my workplace is King's Cross station" changed nothing. Every
  later commute lookup failed address resolution, 4 turns of zero results.
* A-05 — "Budget £1,500, no wait, £1,700." searched at £1,500. Part of the cause was general:
  a figure followed by a sentence-final full stop ("£1,700.") was invisible to the amount
  patterns, whose ``(?![\\d.])`` guard was meant for decimals.

* A-05 — "I changed my mind about the lift, not needed anymore" kept ``property_features =
  ["lift"]``: the retraction vocabulary lacked "not needed", and the same message then re-added
  the feature as a requirement.

"Go back to my original plan" and "also search in Hackney" from A-05 are NOT addressed here.
"""
from __future__ import annotations

import pytest

from core.langgraph_agent import _apply_explicit_criteria_updates
from core.tools.search_properties import _extract_budget

_PRIOR = {"commute_destination": "Old Place", "destination": "Old Place", "max_budget": 2000}


@pytest.mark.parametrize("message,expected", [
    ("I work at King's Cross, so I'd like a commute of under 30 minutes.", "King's Cross"),
    ("Sorry, to be clear: my workplace is King's Cross station.", "King's Cross station"),
    ("I work near Euston station, under 25 minutes please", "Euston station"),
    ("My office is in Shoreditch", "Shoreditch"),
])
def test_the_destination_ends_where_its_clause_ends(message, expected):
    updated = _apply_explicit_criteria_updates(dict(_PRIOR), message)
    assert updated["commute_destination"] == expected


def test_not_commuting_names_no_destination():
    updated = _apply_explicit_criteria_updates(
        dict(_PRIOR), "I work from home so I don't commute at all.")
    assert updated.get("commute_destination") != "all"
    assert updated.get("no_commute") is True


@pytest.mark.parametrize("message,expected", [
    ("Budget £1,500, no wait, £1,700.", 1700),
    ("My budget is £1,800 a month, actually make that £1,900", 1900),
    ("预算1500，不对，1700", 1700),
    ("My budget is £1,800.", 1800),            # the sentence-final full stop
    ("budget 2000.", 2000),
    ("Find a flat for £1,750.50 a month", 1750.5),
])
def test_the_last_correction_wins(message, expected):
    amount, period = _extract_budget(message)
    assert amount == expected and period == "month"
    assert _apply_explicit_criteria_updates(dict(_PRIOR), message)["max_budget"] == expected


def test_a_question_between_two_prices_is_still_not_a_budget():
    assert _extract_budget("flat at £1500 or £1700?") == (None, None)


@pytest.mark.parametrize("message", [
    "I changed my mind about the lift, not needed anymore.",
    "lift not needed anymore",
    "forget the lift",
    "drop the lift requirement",
    "I no longer need a lift",
])
def test_a_retracted_feature_is_removed_and_not_re_added(message):
    updated = _apply_explicit_criteria_updates({"area": "Camden", "property_features": ["lift"]},
                                               message)
    assert "lift" not in (updated.get("property_features") or [])


@pytest.mark.parametrize("message", [
    "I need a lift", "I can't do stairs any more, so I need a lift", "a lift is essential",
])
def test_a_stated_feature_is_still_required(message):
    assert "lift" in (_apply_explicit_criteria_updates({}, message).get("property_features") or [])


# ── review follow-ups ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "My budget is £1500 a month. The deposit is £2000, sorry, £2500.",
    "I earn 4000 a month, sorry 4500 after tax, and my budget is 1500",
    "Budget 1500, actually 1200 sq ft minimum",
    "Max £1500. If it were £1800, actually 2000 would be fine hypothetically",
    "My budget is 1500. If I could, I would pay less",
    "Budget 1500. Would you suggest areas if I were flexible?",
])
def test_only_a_corrected_rent_figure_replaces_the_budget(message):
    assert _extract_budget(message)[0] == 1500


@pytest.mark.parametrize("message", [
    "My job is remote, find me a 1-bed in Hackney under 1500",
    "My work is hybrid so find a 1-bed in Hackney",
    "My job is in finance; find me a flat in Hackney",
    "My job is stressful, so I want a quiet flat in Hackney",
])
def test_a_description_of_the_job_is_not_a_destination(message):
    assert _apply_explicit_criteria_updates({}, message).get("commute_destination") is None


@pytest.mark.parametrize("message", [
    "I need a lift and it's not necessary to have a balcony",
    "I need a lift, not needed: parking",
])
def test_a_retraction_of_something_else_keeps_the_lift(message):
    assert "lift" in (_apply_explicit_criteria_updates({}, message).get("property_features") or [])


@pytest.mark.parametrize("message", [
    "I do not need pets allowed any more", "pets allowed is no longer needed",
])
def test_a_pet_permission_withdrawal_removes_the_cat_filter(message):
    """A-07: the withdrawal said "pets", the filter is "cats allowed"; it stayed hard."""
    updated = _apply_explicit_criteria_updates({"property_features": ["cats allowed", "lift"]},
                                               message)
    assert updated["property_features"] == ["lift"]


def test_a_cat_permission_requirement_is_unchanged():
    assert "cats allowed" in _apply_explicit_criteria_updates(
        {}, "I have a cat, the landlord must allow cats")["property_features"]
    assert _apply_explicit_criteria_updates({}, "Do they allow pets?").get("property_features") is None


def test_forgetting_the_workplace_clears_it_instead_of_setting_it():
    """A-11: "forget that I work at Canary Wharf" SET the destination to Canary Wharf."""
    prior = {"commute_destination": "Canary Wharf, London E14 5AB",
             "destination": "Canary Wharf, London E14 5AB"}
    cleared = _apply_explicit_criteria_updates(dict(prior), "Forget that I work at Canary Wharf.")
    assert cleared.get("commute_destination") is None
    other = _apply_explicit_criteria_updates(dict(prior), "forget about my office in Shoreditch")
    assert other.get("commute_destination") == prior["commute_destination"]
    assert _apply_explicit_criteria_updates({}, "Forget that I work at Canary Wharf.").get(
        "commute_destination") is None
