"""A journey planner endpoint must not resolve outside its stated postcode district (R2-09).

Live rid d44a3e5a: "Powerscroft Road, London E5" geocoded to the Powerscroft Road in Sidcup,
snapped to St Mary Cray Rail Station (Bromley) and returned success with a 54-minute Orpington
route and no low-confidence flag.
"""
from __future__ import annotations

from core.tools import get_transport_info as gti


def _patch(monkeypatch, geocodes):
    monkeypatch.setattr(gti, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(gti, "_cache_set", lambda *a, **k: None)
    monkeypatch.setattr(gti, "_geocode", lambda text: geocodes.get(text))

    def fake_tfl(path, params, **_k):
        if path == "/StopPoint" and params.get("lat", 0) > 51.5:
            return 200, {"stopPoints": [{"id": "910GCLAPTON", "commonName": "Clapton Rail Station",
                                         "stopType": "NaptanRailStation", "distance": 600}]}
        return 200, {"stopPoints": [{"id": "910GSTMRYC", "commonName": "St Mary Cray Rail Station",
                                     "stopType": "NaptanRailStation", "distance": 900}]}
    monkeypatch.setattr(gti, "_tfl_get", fake_tfl)


def test_r2_09_a_wrong_district_geocode_falls_back_to_the_stated_district(monkeypatch):
    _patch(monkeypatch, {
        "Powerscroft Road, London E5": {"lat": 51.396, "lng": 0.106, "postcode": "DA14 6AB"},
        "E5, London": {"lat": 51.556, "lng": -0.054, "postcode": "E5 0AA"},
    })
    resolved = gti._resolve_station("Powerscroft Road, London E5")
    assert resolved["name"] == "Clapton Rail Station"
    assert resolved["approximate"] == "postcode district E5"


def test_r2_09_unresolvable_district_fails_honestly(monkeypatch):
    _patch(monkeypatch, {
        "Powerscroft Road, London E5": {"lat": 51.396, "lng": 0.106, "postcode": "DA14 6AB"},
    })
    resolved = gti._resolve_station("Powerscroft Road, London E5")
    assert resolved["token"] is None
    assert "E5" in resolved["unresolved_reason"]


def test_r2_09_a_matching_district_is_unchanged(monkeypatch):
    _patch(monkeypatch, {
        "Kingsland Road, London E8": {"lat": 51.546, "lng": -0.075, "postcode": "E8 2AA"},
    })
    resolved = gti._resolve_station("Kingsland Road, London E8")
    assert resolved["name"] == "Clapton Rail Station" and "approximate" not in resolved
