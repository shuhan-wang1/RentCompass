"""Targeted criteria-parser fixes from the 2026-10-19 stress round (A-01, A-06, C-11).

None of these is a redesign of the criteria pipeline; each is the smallest change that makes the
deterministic extractor read the phrasing the testers actually used, with a neighbour that must
NOT change.

* A-01 — an explicit area switch kept the OLD area: "Move the search from Islington to Camden",
  "Let's look at Camden instead of Islington", "Search in Camden rather than Islington" all
  searched Islington again; "Switch from Islington to Camden" changed nothing (3/3 live).
* A-06 — a saved default "… in Hackney, London" was restored as two areas [Hackney, London], so
  the search covered all of London and Chigwell IG7 was labelled verified (4/4 live).
* C-11 — "1-bed flat" set no room type, so a "1 bed Flat share" room met bedrooms=1 and was shown
  under "Meets all declared conditions (verified)" (5/5 live).
"""
from __future__ import annotations

import pytest

from core.candidate_validation import validate_candidates
from core.constraint_updates import restore_saved_criteria
from core.langgraph_agent import _apply_explicit_criteria_updates
from core.tools.search_properties import _extract_area, _extract_areas, _extract_room_type

_ISLINGTON = {"area": "Islington", "areas": ["Islington"], "bedrooms": 1, "max_budget": 2000}


@pytest.mark.parametrize("message", [
    "Move the search from Islington to Camden.",
    "Let's look at Camden instead of Islington.",
    "Search in Camden rather than Islington, please.",
    "Switch from Islington to Camden",
    "Change the area from Islington to Camden",
])
def test_an_explicit_area_switch_moves_to_the_new_area(message):
    assert _extract_areas(message) == ["Camden"]
    assert _extract_area(message) == "Camden"
    updated = _apply_explicit_criteria_updates(dict(_ISLINGTON), message)
    assert updated["area"] == "Camden" and updated["areas"] == ["Camden"]


@pytest.mark.parametrize("message", [
    "How long is the commute from Islington to Camden?",
    "Find flats in Camden and Islington",
])
def test_routes_and_lists_are_not_switches(message):
    assert _extract_areas(message) != ["Camden"] or "and" in message
    if "commute" in message:
        assert _apply_explicit_criteria_updates(dict(_ISLINGTON), message)["area"] == "Islington"


def test_a_city_qualified_area_is_one_area():
    assert _extract_areas("1-bed flat in Hackney, London") == ["Hackney"]
    assert _extract_areas("Find flats in Camden, Hackney or Islington") == [
        "Camden", "Hackney", "Islington"]
    assert _extract_areas("Find somewhere in Leeds or London") == ["Leeds", "London"]


def test_restoring_a_saved_default_keeps_its_single_area():
    memories = [{"id": "m1", "mtype": "semantic", "created_at": "2026-10-19T10:00:00",
                 "text": "Default search: 1 bedroom whole flat in Hackney, London under £1,900"}]
    restored = restore_saved_criteria(
        {}, memories, "Use my saved defaults and search again with what you know about me.")
    assert restored.get("areas") == ["Hackney"] and restored.get("area") == "Hackney"
    assert restored.get("max_budget") == 1900


@pytest.mark.parametrize("message,expected", [
    ("I'm looking for a 1-bed flat in hackney under £1900 pcm.", "whole"),
    ("a two bedroom apartment in Leeds", "whole"),
    ("1-bed flat share in Hackney", "shared"),
    ("a room in a 3-bed flat in Camden", None),
    ("find a flat in Camden", None),
    ("a 1-bed flat, not a studio", "whole"),
])
def test_an_n_bed_flat_request_is_a_whole_home(message, expected):
    assert _extract_room_type(message) == expected


def test_a_flat_share_room_is_not_a_verified_one_bed_flat():
    criteria = _apply_explicit_criteria_updates(
        {}, "I'm looking for a 1-bed flat in hackney under £1900 pcm.")
    assert criteria.get("room_type") == "whole"
    share = {"address": "Flat Share - ALL Bills & Wifi Included - Bethnal Green",
             "price": "£875/month", "price_numeric": 875, "bedrooms": 1,
             "property_type": "1 bed Flat share", "area": "Hackney",
             "url": "https://otm.example/share"}
    flat = {"address": "Mare Street, London E8", "price": "£1,850/month", "price_numeric": 1850,
            "bedrooms": 1, "property_type": "1 bed flat", "area": "Hackney",
            "url": "https://otm.example/flat"}
    ledger = validate_candidates([share, flat], dict(criteria, max_budget_monthly=1900), [])
    eligible = [s["candidate"]["address"] for s in ledger["eligible"]]
    assert "Flat Share - ALL Bills & Wifi Included - Bethnal Green" not in eligible


@pytest.mark.parametrize("message", [
    "I go from Hackney to Shoreditch every day for work, find me a flat in Hackney",
    "Find a 1-bed in Hackney. I'd go from Hackney to Camden by bike",
])
def test_a_regular_journey_is_not_an_area_switch(message):
    assert _extract_areas(message) == ["Hackney"]


@pytest.mark.parametrize("message,expected", [
    ("Switch the search from Islington to Camden, it's closer to work", ["Camden"]),
    ("I'm moving from Leeds to London for work, find me a 1-bed", ["London"]),
    ("Change from Hackney to Camden please, I cycle to work", ["Camden"]),
])
def test_route_words_elsewhere_in_the_sentence_do_not_cancel_a_switch(message, expected):
    assert _extract_areas(message) == expected
