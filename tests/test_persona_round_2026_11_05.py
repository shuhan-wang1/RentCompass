"""Four-persona browser round of 2026-11-05 (live on f2f3358).

- A couple asked for a whole 1-bed; "Water Lane, 1 bed Terraced house" was a double bedroom in
  a shared house with three shared bathrooms, and its card stayed after the answer said so.
- Two sharers clarified "£1,200 per person, so a combined maximum of £2,400 per month for the
  whole flat": the budget stayed £1,200 and the £2,400 became a rent-plus-travel cap, so every
  card read "monthly travel cost is not verified" with no commute stated.
- A student said "预算提高到每周 £340，其他要求不变" and was asked to confirm a £300 / £340
  "conflict" because the first message's commute limit read "不超过".
- A family needed a private garden ("essential"); no card said which homes lacked it.
"""
from __future__ import annotations

import pytest

from core.candidate_validation import (hard_constraints, render_candidate_status,
                                       revalidate_view_results, validate_candidates,
                                       with_detail_page_text)
from core.context_assembler import detect_history_conflicts
from core.listing_conditions import check_listing_conditions, garden_condition
from core.tools.search_properties import _extract_budget, _extract_total_budget

WATER_LANE = (
    "A spacious double bedroom is available within a well-presented shared house, conveniently "
    "located within walking distance of Maryland Station. The property offers generous communal "
    "accommodation, including an open-plan kitchen, dining and living area. Residents also benefit "
    "from a large rear garden. The house has three shared bathrooms.")
WHOLE_1BED = {"room_type": "whole", "bedrooms": 1, "max_budget": 2000, "areas": ["canary wharf"]}


def _row(address, price, description, **extra):
    row = {"address": address, "price": f"£{price}/month", "bedrooms": 1, "room_type": "whole",
           "property_type": "Flat", "area": "canary wharf", "description": description,
           "url": f"https://www.onthemarket.com/details/{abs(hash(address)) % 10**8}/"}
    row.update(extra)
    return row


# ── a whole home, read from the listing's own words ──────────────────────────────────────

def test_a_room_in_a_shared_house_is_not_a_whole_one_bed():
    ledger = validate_candidates([_row("Water Lane, London, E15 4NL", 900, WATER_LANE,
                                       property_type="1 bed Terraced house")], WHOLE_1BED)
    assert [s["status"] for s in ledger["statuses"]] == ["excluded"]
    text = render_candidate_status(ledger)
    assert "a room in a shared home, not a whole property" in text
    assert "Listing says: “A spacious double bedroom is available within a well-presented shared house" in text
    zh = render_candidate_status(ledger, language="zh")
    assert "房源原文说明这是合租房里的一个房间，不是整租" in zh


@pytest.mark.parametrize("page", [
    "Modern one bedroom flat with a communal garden and concierge. Shared entrance hall.",
    "Self-contained one bedroom flat; residents' garden and a shared roof terrace.",
    "One bedroom apartment. No shared bathroom, no flatmates.",
])
def test_shared_parts_of_a_building_are_not_a_shared_home(page):
    rows = check_listing_conditions({"description": page}, hard_constraints(WHOLE_1BED))
    assert not [r for r in rows if r["condition"] == "whole_property"]


@pytest.mark.parametrize("page", [
    "Double room to rent in a friendly flat share in Poplar.",
    "Bedroom in a shared house, bills included, shared kitchen.",
    "Large room in an HMO with two housemates.",
])
def test_a_shared_home_in_other_words(page):
    rows = check_listing_conditions({"description": page}, hard_constraints(WHOLE_1BED))
    assert [r["status"] for r in rows if r["condition"] == "whole_property"] == ["not_met"]


def test_the_page_read_to_answer_goes_onto_the_card_and_excludes_it():
    card = _row("Water Lane, London, E15 4NL", 900, "", property_type="1 bed Terraced house",
                url="https://www.onthemarket.com/details/20444990/")
    other = _row("Westferry Road, E14", 1950, "Modern one bedroom flat.")
    shown = revalidate_view_results([card, other], WHOLE_1BED)
    assert shown[0]["qualification"]["status"] != "excluded"
    artifacts = [{"tool": "get_property_details", "raw_data": {
        "found": True, "description_source": "listing detail page (cached copy)",
        "property": {"url": "https://www.onthemarket.com/details/20444990", "description": WATER_LANE,
                     "available_from": "Available now"}}}]
    paged = with_detail_page_text(shown, artifacts)
    rechecked = revalidate_view_results(paged, WHOLE_1BED)
    assert [row["address"] for row in rechecked] == ["Westferry Road, E14", "Water Lane, London, E15 4NL"]
    assert rechecked[1]["qualification"]["status"] == "excluded"
    assert any("shared home" in reason for reason in rechecked[1]["qualification"]["reasons"])


def test_a_one_line_cache_row_does_not_replace_the_card_text():
    artifacts = [{"tool": "get_property_details", "raw_data": {
        "found": True, "description_source": "search card (one line)",
        "property": {"url": "https://www.onthemarket.com/details/1/", "description": "Studio"}}}]
    assert with_detail_page_text([{"url": "https://www.onthemarket.com/details/1/"}], artifacts) is None


# ── per-person and whole-flat amounts in one sentence ────────────────────────────────────

@pytest.mark.parametrize("message", [
    "Sorry, to clarify: £1,200 per person, so a combined maximum of £2,400 per month for the whole flat.",
    "£1,200 each, so £2,400 for the whole flat",
    "Our budget is £1,200 per person, £2,400 combined for the flat between us",
    "我们每人£1200，整套合计£2400每月",
    "£2,400 between the two of us",
    "Whole flat max £2,400 a month.",
])
def test_the_whole_flat_figure_is_the_ceiling_and_no_travel_cap(message):
    assert _extract_budget(message) == (2400, "month")
    assert _extract_total_budget(message) is None


@pytest.mark.parametrize("message,total", [
    ("My total budget is £2,000 for rent plus travel", 2000),
    ("combined rent and travel cap of £2,100", 2100),
])
def test_a_rent_and_travel_cap_is_still_one(message, total):
    assert _extract_total_budget(message) == total


def test_the_clarification_replaces_the_ceiling_in_the_criteria():
    from core.langgraph_agent import _apply_explicit_criteria_updates as update
    criteria = update({}, "Two of us want a 2-bed flat in Clapham. Our budget is £1,200 per month.")
    assert criteria["max_budget"] == 1200
    criteria = update(dict(criteria), "Sorry, to clarify: £1,200 per person, so a combined maximum of "
                                      "£2,400 per month for the whole flat.")
    assert criteria["max_budget"] == 2400
    assert not criteria.get("monthly_total_budget")
    # No travel cost is required of any listing when no rent-and-travel cap was stated.
    ledger = validate_candidates([{"address": "Clapham Road, SW9", "price": "£2300/month",
                                   "bedrooms": 2, "room_type": "whole", "area": "Clapham"}], criteria)
    assert not any("travel cost" in r for s in ledger["statuses"] for r in s["unknown_reasons"])


# ── an announced change is no conflict ───────────────────────────────────────────────────

@pytest.mark.parametrize("first", [
    "我在Imperial读书，想找en-suite，包bills，通勤不超过35分钟，预算每周£300",
    "预算每周最多£300，通勤35分钟以内",
    "My budget is at most £300 a week",
])
@pytest.mark.parametrize("later", [
    "预算提高到每周 £340，其他要求不变",
    "Raise my budget to £340 a week, everything else the same",
    "把预算调到每周£340",
])
def test_raising_the_budget_is_an_update_not_a_conflict(first, later):
    assert detect_history_conflicts([{"user": first, "assistant": "ok"}], later) == []


def test_a_hard_ceiling_broken_without_a_change_word_is_still_flagged():
    conflicts = detect_history_conflicts([{"user": "My budget is at most £300 a week", "assistant": "ok"}],
                                         "My budget is £400 a week")
    assert [c["kind"] for c in conflicts] == ["absolute_violated"]


# ── an essential garden, per listing ─────────────────────────────────────────────────────

FAMILY = {"room_type": "whole", "bedrooms": 3, "max_budget": 2800, "areas": ["greenwich"],
          "no_commute": True, "soft_preferences": ["Must have garden"],
          "constraint_sources": {"soft_preferences": {
              "text": "a 3-bed whole house in Greenwich with a private garden — the garden is essential"}}}


def test_each_card_says_what_it_shows_of_the_private_garden():
    homes = [
        {"address": "A Road, SE10", "price": "£2700/month", "bedrooms": 3, "room_type": "whole",
         "area": "greenwich", "description": "Three bedroom house with a south facing rear garden."},
        {"address": "B Road, SE10", "price": "£2600/month", "bedrooms": 3, "room_type": "whole",
         "area": "greenwich", "description": "Three bedroom flat with access to a communal garden."},
        {"address": "C Road, SE10", "price": "£2500/month", "bedrooms": 3, "room_type": "whole",
         "area": "greenwich", "description": "Three bedroom maisonette near the park."},
    ]
    ledger = validate_candidates(homes, FAMILY)
    assert [s["status"] for s in ledger["statuses"]] == ["eligible", "excluded", "unknown"]
    assert ledger["unchecked"] == []
    card = revalidate_view_results(homes[2:], FAMILY)[0]
    assert "尚未确认是否有私人花园" in card["qualification"]["reason_messages"]["zh"]
    assert "private garden is not confirmed" in card["qualification"]["reason_messages"]["en"]


def test_a_garden_wish_is_not_a_need():
    constraints = hard_constraints({"soft_preferences": ["Would like garden"]})
    assert constraints["garden"] is None


@pytest.mark.parametrize("text,private,status", [
    ("No garden, but a balcony.", False, "not_met"),
    ("Communal gardens to the rear.", False, "met"),
    ("Communal gardens to the rear.", True, "not_met"),
    ("Private garden of 40 ft.", True, "met"),
    ("Close to Greenwich Park.", True, "unknown"),
])
def test_garden_condition(text, private, status):
    assert garden_condition(text, private=private)["status"] == status
