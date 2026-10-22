"""A question about one listing or street does not change the search area (r6d).

r20 acceptance, rid 1419b01e7ca742b2aeb0d83a904d5f93: in a Hackney search conversation,
"Tell me more about Arbutus Street in Haggerston." replaced the requested area with
Haggerston (view_state.criteria.area "Haggerston", and the shown Hackney listing then read
"area hackney is outside the requested areas"). Same on main. The parser's bare location cue
"in <place>" fired inside a question about a specific listing. A weak cue (in / near /
around) qualifying a street or listing address, or inside a question about one listing, is a
reference; explicit search or move wording still switches the area.
"""
from __future__ import annotations

import pytest

from core.langgraph_agent import _apply_explicit_criteria_updates
from core.refine_results import plan_refinement
from core.tools.search_properties import _extract_area

HACKNEY = "Find me a 1-bed in Hackney under £2,000."
R20 = "Tell me more about Arbutus Street in Haggerston."   # the recorded message


def _after(message):
    return _apply_explicit_criteria_updates(_apply_explicit_criteria_updates({}, HACKNEY), message)


@pytest.mark.parametrize("message", [
    R20,
    "What's the flat on Arbutus Street in Haggerston like?",
    "Is Jacob House in Hackney near a station?",
    "Is the Arbutus Street flat in Haggerston near a station?",
    "Can you tell me more about the one on Powell Road in Clapton?",
])
def test_a_listing_question_keeps_the_search_area(message):
    after = _after(message)
    assert after["area"] == "Hackney" and after["areas"] == ["Hackney"]
    assert after["constraint_sources"]["area"]["text"] == HACKNEY


def test_the_listing_question_neither_switches_nor_refines():
    assert _extract_area(R20) is None
    shown = [{"address": "Arbutus Street, London E8", "area": "Haggerston", "price": "£1900/month"},
             {"address": "Jacob House, London, E8", "area": "Hackney", "price": "£1950/month"}]
    assert plan_refinement(R20, shown) is None


@pytest.mark.parametrize("message, area", [
    ("search in Haggerston", "Haggerston"),
    ("what about Haggerston instead?", "Haggerston"),
    ("show me places in Haggerston", "Haggerston"),
    ("Find flats in Haggerston", "Haggerston"),
    ("Are there flats in Haggerston?", "Haggerston"),
    ("Let's look in Haggerston", "Haggerston"),
    ("I'd rather live in Haggerston", "Haggerston"),
    ("and Haggerston?", "Haggerston"),
    ("How about Islington?", "Islington"),
    ("studios in Islington under 1500", "Islington"),
])
def test_explicit_search_or_move_wording_still_switches(message, area):
    assert _after(message)["area"] == area


def test_a_mixed_criteria_and_listing_question_is_not_answered_as_a_refinement():
    # Recorded round message: the deterministic refinement used to answer only the criteria
    # half ("Refined the 3 listings ...") and drop the distance question.
    message = "I prefer Hackney, 2 bed, up to £2,200. How far is the first one from Liverpool Street?"
    shown = [{"address": "Mare St, Hackney E8", "area": "Hackney", "price": "£1500/month"},
             {"address": "Upper St, Islington N1", "area": "Islington", "price": "£1900/month"}]
    assert plan_refinement(message, shown) is None
    assert _apply_explicit_criteria_updates({}, message)["area"] == "Hackney"


def test_a_question_about_the_area_itself_still_sets_it():
    # Recorded round message: about renting in the area, not about one listing.
    message = "Tell me about renting in Hackney for a 1-bed under £2,300; my codeword is kiwi1."
    assert _apply_explicit_criteria_updates({}, message)["area"] == "Hackney"
