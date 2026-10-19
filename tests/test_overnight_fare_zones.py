"""Fare zones come from the district's stations, not from its number (2026-10-19 B-03).

``_get_zone_from_postcode`` derived a zone from the district NUMBER ("E 9-12 -> zone 4"). Postcode
districts are numbered alphabetically by post town, so 11 of the 18 districts the tester checked
were 1-3 zones out, and Stratford -> Waterloo was priced as a Zone 1-5 Travelcard (£293.40)
instead of Zone 1-3 (£201.60). The expected zones below are the tester's (inst-B
offline-evidence §3); a boundary district may answer either of its zones.
"""
from __future__ import annotations

import pytest

from core.tools import calculate_commute_cost as commute_cost

_REAL = {
    "E14 5AB": {2}, "E15 1DA": {2, 3}, "E17 7JN": {3}, "E8 3DL": {2}, "E9 5HB": {2},
    "N16 8AA": {2}, "N15 4AA": {3}, "SE15 5DW": {2}, "SE16 4DG": {2}, "W12 8PH": {2},
    "NW6 2HH": {2}, "NW1 8NH": {1, 2}, "E1 6AN": {1, 2}, "E3 4AA": {2, 3}, "SW9 8HE": {2},
    "SE1 9SP": {1}, "N7 0EG": {2}, "N1 9AP": {1, 2},
}


@pytest.mark.parametrize("postcode", sorted(_REAL))
def test_district_zone_matches_its_stations(postcode):
    assert commute_cost._get_zone_from_postcode(postcode) in _REAL[postcode]


@pytest.mark.parametrize("postcode", ["WD17 1EU", "KT1 1AA", "HA1 1AA", "M1 1AE", "", "N99 9ZZ"])
def test_an_unknown_district_stays_unpriced(postcode):
    assert commute_cost._get_zone_from_postcode(postcode) is None


def test_stratford_to_waterloo_is_a_zone_1_3_travelcard(monkeypatch):
    from core import maps_service as maps

    monkeypatch.setattr(maps, "calculate_travel_basis", lambda *args: {
        "duration_minutes": 19, "source": "TfL Journey Planner",
        "basis": "tfl_journey_plan", "basis_note": "Provider journey"})
    # Real postcode districts, resolved by the real table (no geocoding).
    monkeypatch.setattr(commute_cost, "_get_zone_from_address", lambda address:
                        commute_cost._get_zone_from_postcode(address.rsplit(",", 1)[-1]))
    route = commute_cost.calculate_commute_cost_impl(
        "Stratford, E15 1DA", "Waterloo, SE1 7LY", travel_type="adult", office_days_per_week=5)
    priced = route["transport_cost"]
    assert priced["recommended_pass"].startswith("Zone 1-3"), priced["recommended_pass"]
    assert (priced["from_zone"], priced["to_zone"]) == (3, 1)
