"""Weather geocoding resolves UK places first (QA round r01: R1-12).

get_weather("Richmond") returned Richmond, Virginia (37.55, -77.46) with success=true, while
"Reading, UK", "Newcastle upon Tyne, UK", "Boston, Lincolnshire, UK", "Newport, UK" and
"Richmond, London" failed to resolve: the Open-Meteo geocoder matches a bare place NAME, so a
qualifier made the name unmatchable, and without a country filter the first worldwide hit won.
No network: the geocoder and forecast endpoints are faked below.
"""
from __future__ import annotations

import pytest

from core.tools import get_weather as weather

_GEO = {
    "Richmond": [
        {"name": "Richmond", "latitude": 37.55, "longitude": -77.46, "country_code": "US",
         "admin1": "Virginia"},
        {"name": "Richmond", "latitude": 51.46, "longitude": -0.30, "country_code": "GB",
         "admin1": "England", "admin2": "Greater London", "admin3": "Richmond upon Thames"},
        {"name": "Richmond", "latitude": 54.40, "longitude": -1.74, "country_code": "GB",
         "admin1": "England", "admin2": "North Yorkshire"},
    ],
    "Reading": [{"name": "Reading", "latitude": 51.45, "longitude": -0.97, "country_code": "GB",
                 "admin1": "England", "admin2": "Reading"}],
    "Newcastle upon Tyne": [{"name": "Newcastle upon Tyne", "latitude": 54.97,
                             "longitude": -1.61, "country_code": "GB", "admin1": "England",
                             "admin2": "Tyne and Wear"}],
    "Boston": [
        {"name": "Boston", "latitude": 42.36, "longitude": -71.06, "country_code": "US",
         "admin1": "Massachusetts"},
        {"name": "Boston", "latitude": 52.98, "longitude": -0.03, "country_code": "GB",
         "admin1": "England", "admin2": "Lincolnshire"},
    ],
    "Newport": [{"name": "Newport", "latitude": 51.59, "longitude": -2.99, "country_code": "GB",
                 "admin1": "Wales", "admin2": "Newport"}],
}


class _Response:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_get(url, params=None, timeout=None):
        seen.append((url, dict(params or {})))
        if "geocoding-api" in url:
            results = _GEO.get(params["name"], [])
            if params.get("countryCode"):
                results = [r for r in results if r["country_code"] == params["countryCode"]]
            return _Response({"results": results} if results else {})
        return _Response({"current": {"temperature_2m": 12.0, "relative_humidity_2m": 70,
                                      "apparent_temperature": 11.0, "precipitation": 0.0,
                                      "weather_code": 3, "wind_speed_10m": 9.0,
                                      "time": "2026-10-19T19:00"},
                          "hourly": {"time": ["2026-10-19T19:00"], "uv_index": [0.0]}})

    monkeypatch.setattr(weather.requests, "get", fake_get)
    return seen


def _forecast_coords(seen):
    (_, params), = [c for c in seen if "api.open-meteo.com/v1/forecast" in c[0]]
    return params["latitude"], params["longitude"]


@pytest.mark.parametrize("location, coords", [
    ("Richmond", (51.46, -0.30)),
    ("Richmond, London", (51.46, -0.30)),
    ("Reading, UK", (51.45, -0.97)),
    ("Newcastle upon Tyne, UK", (54.97, -1.61)),
    ("Boston, Lincolnshire, UK", (52.98, -0.03)),
    ("Newport, UK", (51.59, -2.99)),
])
def test_r1_12_uk_place_first(calls, location, coords):
    result = weather.get_weather_impl(location)
    assert result["success"] is True, result
    assert _forecast_coords(calls) == coords
    geocode = [p for u, p in calls if "geocoding-api" in u][0]
    assert geocode["countryCode"] == "GB" and "," not in geocode["name"]


def test_r1_12_resolved_place_is_named(calls):
    result = weather.get_weather_impl("Richmond, London")
    assert "Greater London" in result["resolved_place"]
    assert result["requested_location"] == "Richmond, London"


def test_r1_12_unknown_uk_place_fails_honestly(calls):
    result = weather.get_weather_impl("Atlantis, UK")
    assert result["success"] is False
    assert not [u for u, _ in calls if "forecast" in u]


def test_r1_12_an_explicit_foreign_country_is_searched_worldwide(calls):
    result = weather.get_weather_impl("Richmond, Virginia, USA")
    assert result["success"] is True
    assert _forecast_coords(calls) == (37.55, -77.46)
