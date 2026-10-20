"""R3-08: check_safety falls back to the postcode district or the area, honestly labelled.

r05 rid c7958a90: address "Highgate West Hill, London, N6", area "Highgate" -> "Could not
geocode address.", lat/lon null, no safety data at all.
"""
from __future__ import annotations

import core.tools.check_safety as cs

FAILED = {"error": "Could not geocode address.", "total_crimes_6m": "Unknown",
          "reference_point": {"lat": None, "lon": None}, "queried_months": [],
          "successful_months": [], "failed_months": []}


def _ok(query):
    return {"total_crimes_6m": 600, "crimes_per_month": 100.0, "months_covered": 6,
            "reference_point": {"lat": 51.57, "lon": -0.15, "precision": "postcode",
                                "query": query},
            "source": "data.police.uk", "successful_months": ["2026-08"], "failed_months": [],
            "data_months": ["2026-08"], "queried_months": ["2026-08"]}


def test_ungeocodable_address_falls_back_to_the_postcode_district(monkeypatch):
    seen = []

    def fake(query, latitude=None, longitude=None):
        seen.append(query)
        return FAILED if query == "Highgate West Hill, London, N6" else _ok(query)

    monkeypatch.setattr(cs, "get_crime_data_by_location", fake)
    out = cs.check_safety_impl(address="Highgate West Hill, London, N6", area="Highgate")
    assert seen[:2] == ["Highgate West Hill, London, N6", "N6, London"]
    assert out["safety_score"] is not None
    ref = out["reference_point"]
    assert ref["fallback_kind"] == "postcode_district" and ref["is_specific_address"] is False
    assert "not the listing's own location" in ref["measured_from"]
    assert out["address"] == "Highgate West Hill, London, N6"


def test_then_the_area(monkeypatch):
    def fake(query, latitude=None, longitude=None):
        return _ok(query) if query == "Highgate, London" else FAILED

    monkeypatch.setattr(cs, "get_crime_data_by_location", fake)
    out = cs.check_safety_impl(address="Highgate West Hill, London, N6", area="Highgate")
    assert out["reference_point"]["fallback_kind"] == "area"
    assert out["reference_point"]["queried_location"] == "Highgate, London"


def test_nothing_resolves_keeps_the_honest_failure(monkeypatch):
    monkeypatch.setattr(cs, "get_crime_data_by_location", lambda *a, **k: dict(FAILED))
    out = cs.check_safety_impl(address="Highgate West Hill, London, N6", area="Highgate")
    assert out["safety_score"] is None
    assert out["crime_data"]["error"] == "Could not geocode address."
