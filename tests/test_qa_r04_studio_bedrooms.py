"""R4-07: a studio satisfies a bedrooms=0 (studio) search.

r07 B 6e1e1661: recommendations had bedrooms 'Studio', property_type 'Studio', match_type
perfect, in budget; every one was filed "Not verified: bedroom count is not verified".
"""
from __future__ import annotations

from core.candidate_validation import render_candidate_status, validate_candidates

CRITERIA = {"area": "Bloomsbury", "areas": ["Bloomsbury"], "max_budget": 2200, "bedrooms": 0,
            "room_type": "studio"}


def _row(address, **over):
    row = {"address": address, "price": "£1,650/month", "bedrooms": "Studio",
           "property_type": "Studio", "match_type": "perfect", "area": "Bloomsbury"}
    row.update(over)
    return row


def test_studio_rows_are_eligible_for_a_studio_search():
    validation = validate_candidates([_row("Tavistock Place, WC1H"), _row("Judd Street, WC1H")],
                                     CRITERIA, [])
    assert not validation.get("unknown"), validation.get("unknown")
    text = render_candidate_status(validation, language="en")
    assert "bedroom count is not verified" not in text


def test_studio_by_property_type_when_bedrooms_missing():
    validation = validate_candidates([_row("Marchmont Street, WC1N", bedrooms="N/A")], CRITERIA, [])
    assert not validation.get("unknown")


def test_one_bed_is_still_not_a_studio():
    validation = validate_candidates([_row("Gower Street, WC1E", bedrooms=1, property_type="Flat")],
                                     CRITERIA, [])
    assert validation.get("excluded")


def test_unknown_bedrooms_on_a_flat_stay_unverified():
    validation = validate_candidates([_row("Store Street, WC1E", bedrooms="N/A", property_type="Flat")],
                                     dict(CRITERIA, bedrooms=1, room_type="whole"), [])
    assert validation.get("unknown")
