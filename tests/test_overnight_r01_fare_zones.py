"""Zone 1 stations must not be priced as Zone 2 (overnight round r01: R1-09, ledger B-03).

calculate_commute_cost King's Cross / Euston Station / Paddington Station -> Canary Wharf
returned from_zone 2 and recommended a "Zone 2 Monthly Travelcard" (£128.70), which does not
cover a Zone 1 terminus; the same turn's PAYG cap was already the Zone 1-2 cap.
"""
from __future__ import annotations

import pytest

from core.tools import calculate_commute_cost as commute_cost


@pytest.mark.parametrize("address", [
    "King's Cross", "Kings Cross, London", "King's Cross St. Pancras", "St Pancras International",
    "Euston Station", "Euston Station, London NW1 2RT", "Paddington Station", "London Paddington",
    "Waterloo Station, London SE1 8SW", "Bank", "Angel, London", "Liverpool Street station",
])
def test_r1_09_named_zone_one_station(address):
    assert commute_cost._zone_from_station_name(address) == 1
    assert commute_cost._get_zone_from_address(address) == 1


@pytest.mark.parametrize("address", [
    "Victoria Road, Ilford IG1 1AA", "Euston Road, London NW1", "Angel Lane, Stratford E15",
    "King's Cross Road", "Canary Wharf", "Highgate West Hill, London N6",
])
def test_r1_09_street_names_are_not_stations(address):
    assert commute_cost._zone_from_station_name(address) is None


@pytest.mark.parametrize("postcode", ["N1C 4QL", "N1 9AP", "NW1 2RT", "NW1 6JJ", "W2 1HB"])
def test_r1_09_terminus_districts_answer_the_covering_zone(postcode):
    assert commute_cost._get_zone_from_postcode(postcode) == 1


@pytest.mark.parametrize("origin", ["King's Cross", "Euston Station", "Paddington Station"])
def test_r1_09_zone_one_origin_is_never_sold_a_zone_two_pass(monkeypatch, origin):
    from core import maps_service

    monkeypatch.setattr(maps_service, "calculate_travel_basis", lambda *a, **k: {
        "duration_minutes": 25, "source": "tfl_journey_planner", "basis": "measured"})
    monkeypatch.setattr(maps_service, "geocode_address", lambda address: {"postcode": "E14 5AB"})
    priced = commute_cost.calculate_commute_cost_impl(origin, "Canary Wharf", travel_type="adult")
    blob = repr(priced)
    assert "'from_zone': 1" in blob and "'to_zone': 2" in blob, blob[:400]
    assert "Zone 2 Monthly" not in blob and "Zone 2 PAYG" not in blob
    assert "Zone 1-2" in blob
