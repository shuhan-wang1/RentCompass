"""R3-08 / R4-03: check_safety falls back to the postcode district or the area, honestly.

r05 rid c7958a90: address "Highgate West Hill, London, N6", area "Highgate" -> "Could not
geocode address.", no safety data at all. The first fallback (486a652) geocoded the free
text "N6, London" and landed at Heathrow (r06 e88ce84d, "Very Safe 92/100", precision
"address"). The district now comes from an outcode lookup, and an area fallback must
resolve to a place that names the area.
"""
from __future__ import annotations

import core.maps_service as maps_service
import core.tools.check_safety as cs

ADDRESS = "Highgate West Hill, London, N6"
FAILED = {"error": "Could not geocode address.", "total_crimes_6m": "Unknown",
          "reference_point": {"lat": None, "lon": None}, "queried_months": [],
          "successful_months": [], "failed_months": []}


def _ok(query, resolved="", lat=51.57, lon=-0.15, precision="supplied_coordinates"):
    return {"total_crimes_6m": 600, "crimes_per_month": 100.0, "months_covered": 6,
            "reference_point": {"lat": lat, "lon": lon, "precision": precision,
                                "resolved_name": resolved, "query": query},
            "source": "data.police.uk", "successful_months": ["2026-08"], "failed_months": [],
            "data_months": ["2026-08"], "queried_months": ["2026-08"]}


def test_district_comes_from_an_outcode_lookup(monkeypatch):
    seen = []

    def crime(query, latitude=None, longitude=None):
        seen.append((query, latitude, longitude))
        return FAILED if query == ADDRESS else _ok(query, lat=latitude, lon=longitude)

    monkeypatch.setattr(cs, "get_crime_data_by_location", crime)
    monkeypatch.setattr(maps_service, "geocode_outcode",
                        lambda code: {"lat": 51.571, "lng": -0.148, "outcode": code,
                                      "geocoder": "postcodes.io/outcodes"} if code == "N6" else None)
    out = cs.check_safety_impl(address=ADDRESS, area="Highgate")
    assert seen[1] == ("N6 postcode district", 51.571, -0.148)
    ref = out["reference_point"]
    assert ref["fallback_kind"] == "postcode_district" and ref["precision"] == "postcode_district"
    assert ref["is_specific_address"] is False
    assert "not the listing's own location" in ref["measured_from"]
    assert out["address"] == ADDRESS


def test_free_text_district_is_never_geocoded(monkeypatch):
    """The r06 Heathrow case: no outcode result -> no "N6, London" text geocode."""
    seen = []

    def crime(query, latitude=None, longitude=None):
        seen.append(query)
        if query == "N6, London":
            return _ok(query, resolved="N6, Airside Road Tunnel, London Heathrow, TW6 1PZ",
                       lat=51.465, lon=-0.465, precision="address")
        return FAILED

    monkeypatch.setattr(cs, "get_crime_data_by_location", crime)
    monkeypatch.setattr(maps_service, "geocode_outcode", lambda code: None)
    out = cs.check_safety_impl(address=ADDRESS, area="Highgate")
    assert "N6, London" not in seen
    assert out["safety_score"] is None
    assert out["crime_data"]["error"] == "Could not geocode address."


def test_area_fallback_must_name_the_area(monkeypatch):
    def crime(query, latitude=None, longitude=None):
        if query == "Highgate, London":
            return _ok(query, resolved="Highgate, London Borough of Haringey, London, N6", precision="area")
        return FAILED

    monkeypatch.setattr(cs, "get_crime_data_by_location", crime)
    monkeypatch.setattr(maps_service, "geocode_outcode", lambda code: None)
    out = cs.check_safety_impl(address=ADDRESS, area="Highgate")
    assert out["reference_point"]["fallback_kind"] == "area"
    assert out["reference_point"]["precision"] == "area"


def test_implausible_area_result_is_rejected(monkeypatch):
    def crime(query, latitude=None, longitude=None):
        if query == "Highgate, London":
            return _ok(query, resolved="Heathrow Airport, London, TW6", precision="address")
        return FAILED

    monkeypatch.setattr(cs, "get_crime_data_by_location", crime)
    monkeypatch.setattr(maps_service, "geocode_outcode", lambda code: None)
    out = cs.check_safety_impl(address=ADDRESS, area="Highgate")
    assert out["safety_score"] is None


def test_geocode_outcode_checks_the_returned_district(monkeypatch):
    class _Resp:
        status_code = 200

        def __init__(self, outcode):
            self._outcode = outcode

        def json(self):
            return {"result": {"outcode": self._outcode, "latitude": 51.57, "longitude": -0.15}}

    monkeypatch.setattr(maps_service.requests, "get", lambda url, timeout=None: _Resp("N6"))
    assert maps_service.geocode_outcode("n6")["lat"] == 51.57
    monkeypatch.setattr(maps_service.requests, "get", lambda url, timeout=None: _Resp("TW6"))
    assert maps_service.geocode_outcode("N6") is None
    assert maps_service.geocode_outcode("Highgate") is None
