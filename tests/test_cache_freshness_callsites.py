from __future__ import annotations

from types import SimpleNamespace


def _entry(status, value):
    return SimpleNamespace(status=status, value=value)


def test_web_search_labels_stale_fallback_when_refresh_has_no_results(monkeypatch):
    from core import web_search

    monkeypatch.setattr(
        web_search,
        "get_from_cache",
        lambda *args, **kwargs: _entry("stale", "[1] old result"),
    )
    monkeypatch.setattr(web_search._searxng_client, "search", lambda *a, **k: [])

    result = web_search.get_search_snippets("current rents")
    assert "possibly outdated" in result
    assert "live refresh returned no usable results" in result
    assert "[1] old result" in result


def test_geocode_labels_stale_fallback_when_both_providers_fail(monkeypatch):
    from core import maps_service

    stale = {
        "lat": 51.5,
        "lng": -0.1,
        "geocoder": "nominatim",
        "match_type": "suburb",
    }
    monkeypatch.setattr(
        maps_service,
        "get_from_cache",
        lambda *args, **kwargs: _entry("stale", stale),
    )
    monkeypatch.setattr(
        maps_service.requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(
            maps_service.requests.exceptions.RequestException("offline")
        ),
    )

    result = maps_service._free_geocode("Hackney")
    assert result["cache_status"] == "stale"
    assert result["possibly_outdated"] is True
    assert "refresh failed" in result["warning"]


def test_crime_labels_stale_fallback_and_does_not_recache_failure(monkeypatch):
    from core import maps_service

    stale = {
        "total_crimes_6m": 900,
        "months_covered": 3,
        "crimes_per_month": 300.0,
        "crime_trend": "stable",
    }
    monkeypatch.setattr(
        maps_service,
        "get_from_cache",
        lambda *args, **kwargs: _entry("stale", stale),
    )
    monkeypatch.setattr(
        maps_service,
        "_get_coordinates",
        lambda _address: {"lat": 51.5, "lng": -0.1},
    )
    monkeypatch.setattr(
        maps_service.requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(
            maps_service.requests.exceptions.RequestException("offline")
        ),
    )
    writes = []
    monkeypatch.setattr(maps_service, "set_to_cache", lambda *a, **k: writes.append(a))

    result = maps_service.get_crime_data_by_location("Hackney")
    assert result["cache_status"] == "stale"
    assert result["possibly_outdated"] is True
    assert "UK Police API refresh" in result["warning"]
    assert writes == []
