# -*- coding: utf-8 -*-
"""Focused tests for the map / POI pipeline fixes.

Covers (network mocked throughout — no live Overpass calls):
  * a descriptive User-Agent is sent on every Overpass request
  * mirror rotation on 4xx/5xx and OverpassError when all mirrors fail
  * the batched single-union query shape
  * the cache-hit path (second fetch does not touch the network)
  * the honest-degradation banner
"""
import json
import sys
from pathlib import Path

# The `core` package we test lives under app/. A stale tests/core/
# directory can otherwise shadow it (pytest prepends the tests dir), so pin the
# real package dir at the front of sys.path before importing.
_LOCAL_DATA_DEMO = str(Path(__file__).resolve().parents[1] / "app")
if _LOCAL_DATA_DEMO not in sys.path:
    sys.path.insert(0, _LOCAL_DATA_DEMO)
elif sys.path[0] != _LOCAL_DATA_DEMO:
    sys.path.remove(_LOCAL_DATA_DEMO)
    sys.path.insert(0, _LOCAL_DATA_DEMO)
for _mod in [m for m in list(sys.modules) if m == "core" or m.startswith("core.")]:
    if getattr(sys.modules[_mod], "__file__", "") and "tests" in (sys.modules[_mod].__file__ or ""):
        del sys.modules[_mod]

import pytest

from core import maps_service
from core.maps_service import overpass_request, OverpassError, OVERPASS_MIRRORS
from core import amenity_map_generator
from core.amenity_map_generator import PropertyAmenityMapGenerator

MANCHESTER = (53.4808, -2.2426)

_ELEMENTS = {
    "elements": [
        {"type": "node", "id": 1, "lat": 53.4809, "lon": -2.2427,
         "tags": {"name": "Tesco", "shop": "supermarket"}},
        {"type": "node", "id": 2, "lat": 53.4810, "lon": -2.2428,
         "tags": {"name": "Golden Dragon", "amenity": "restaurant", "cuisine": "chinese"}},
        {"type": "way", "id": 3, "center": {"lat": 53.4811, "lon": -2.2429},
         "tags": {"name": "Park", "leisure": "park"}},
    ]
}


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return json.loads(self._body)


def _make_post(recorder, script):
    """Build a fake requests.post. `script` is a list of (status, body) served
    in order; `recorder` collects (url, headers) for each call."""
    it = iter(script)

    def fake_post(url, data=None, headers=None, timeout=None):
        recorder.append((url, headers))
        status, body = next(it)
        return _Resp(status, body)

    return fake_post


def test_ua_sent_on_every_overpass_request(monkeypatch):
    calls = []
    monkeypatch.setattr(maps_service.requests, "post",
                        _make_post(calls, [(200, json.dumps(_ELEMENTS))]))
    data = overpass_request("[out:json];node(1);out;")
    assert len(calls) == 1
    _url, headers = calls[0]
    assert "uk-rent-recommendation" in headers["User-Agent"]
    assert len(data["elements"]) == 3


def test_mirror_rotation_on_server_error(monkeypatch):
    calls = []
    # First mirror 504 (busy), second mirror 200.
    monkeypatch.setattr(maps_service.requests, "post",
                        _make_post(calls, [(504, "busy"), (200, json.dumps(_ELEMENTS))]))
    monkeypatch.setattr(maps_service.time, "sleep", lambda *_: None)
    data = overpass_request("[out:json];out;")
    assert len(calls) == 2
    assert calls[0][0] == OVERPASS_MIRRORS[0]
    assert calls[1][0] == OVERPASS_MIRRORS[1]
    assert calls[0][0] != calls[1][0]
    assert len(data["elements"]) == 3


def test_overpass_error_when_all_mirrors_fail(monkeypatch):
    calls = []
    # Always 429 -> every mirror across every round fails.
    script = [(429, "rate limited")] * (len(OVERPASS_MIRRORS) * 2 + 2)
    monkeypatch.setattr(maps_service.requests, "post", _make_post(calls, script))
    monkeypatch.setattr(maps_service.time, "sleep", lambda *_: None)
    with pytest.raises(OverpassError):
        overpass_request("[out:json];out;", max_rounds=2)
    # Two full rounds over the whole pool.
    assert len(calls) == len(OVERPASS_MIRRORS) * 2


def test_overpass_remark_rotates_mirror(monkeypatch):
    calls = []
    # First mirror answers HTTP 200 but with a runtime-timeout *remark* and an
    # empty body (a busy server). That must NOT be trusted -> rotate to the next
    # mirror, which returns a real result.
    remark_body = json.dumps({
        "version": 0.6,
        "remark": 'runtime error: Query timed out in "query" at line 1',
        "elements": [],
    })
    monkeypatch.setattr(maps_service.requests, "post",
                        _make_post(calls, [(200, remark_body), (200, json.dumps(_ELEMENTS))]))
    monkeypatch.setattr(maps_service.time, "sleep", lambda *_: None)
    data = overpass_request("[out:json];out;")
    assert len(calls) == 2
    assert calls[0][0] == OVERPASS_MIRRORS[0]
    assert calls[1][0] == OVERPASS_MIRRORS[1]
    assert calls[0][0] != calls[1][0]        # rotated off the remarking mirror
    assert len(data["elements"]) == 3        # real body from the second mirror


def test_all_mirrors_remark_raises_overpass_error(monkeypatch):
    calls = []
    remark_body = json.dumps({"remark": "runtime error: Query ran out of memory",
                              "elements": []})
    script = [(200, remark_body)] * (len(OVERPASS_MIRRORS) * 2 + 2)
    monkeypatch.setattr(maps_service.requests, "post", _make_post(calls, script))
    monkeypatch.setattr(maps_service.time, "sleep", lambda *_: None)
    with pytest.raises(OverpassError):
        overpass_request("[out:json];out;", max_rounds=2)
    assert len(calls) == len(OVERPASS_MIRRORS) * 2


def test_expect_nonempty_rotates_past_empty_200(monkeypatch):
    calls = []
    # First mirror answers HTTP 200 with an EMPTY elements list and NO remark
    # (a busy mirror, e.g. overpass.osm.ch). With expect_nonempty=True this is an
    # outage of that mirror -> rotate to the next, which has the real data.
    empty_body = json.dumps({"version": 0.6, "elements": []})
    monkeypatch.setattr(maps_service.requests, "post",
                        _make_post(calls, [(200, empty_body), (200, json.dumps(_ELEMENTS))]))
    monkeypatch.setattr(maps_service.time, "sleep", lambda *_: None)
    data = overpass_request("[out:json];out;", expect_nonempty=True)
    assert len(calls) == 2
    assert calls[0][0] != calls[1][0]        # rotated off the empty mirror
    assert len(data["elements"]) == 3


def test_empty_200_is_ok_when_not_expecting_results(monkeypatch):
    # The default (single-type callers) must still accept an empty 200 as a
    # legitimate "none nearby" and NOT rotate/raise.
    calls = []
    empty_body = json.dumps({"version": 0.6, "elements": []})
    monkeypatch.setattr(maps_service.requests, "post",
                        _make_post(calls, [(200, empty_body)]))
    data = overpass_request("[out:json];out;")   # expect_nonempty defaults False
    assert len(calls) == 1                        # no rotation
    assert data["elements"] == []


def test_empty_total_raises_overpass_error_and_is_not_cached(monkeypatch):
    # A busy mirror answers 200 with an EMPTY elements list but NO remark. For a
    # batched all-selector query this is an outage, not a real "nothing nearby":
    # fetch_all_amenities must raise OverpassError (so the caller shows the
    # banner) and must NOT poison the cache with a zero-amenity entry.
    store = {}
    monkeypatch.setattr(amenity_map_generator, "get_from_cache", store.get)
    monkeypatch.setattr(amenity_map_generator, "set_to_cache",
                        lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(amenity_map_generator, "overpass_request",
                        lambda *a, **k: {"elements": []})

    gen = PropertyAmenityMapGenerator(radius_km=1.5)
    with pytest.raises(OverpassError):
        gen.fetch_all_amenities(*MANCHESTER)
    assert store == {}                       # nothing cached -> no poison


def test_cached_zero_total_is_ignored_and_refetched(monkeypatch):
    # A previously-poisoned (all-zero) cache entry must be ignored on read and a
    # live re-fetch attempted, so a single bad response can't degrade the cell
    # for the whole 7-day TTL.
    import time as _time
    store = {}
    monkeypatch.setattr(amenity_map_generator, "get_from_cache", store.get)
    monkeypatch.setattr(amenity_map_generator, "set_to_cache",
                        lambda k, v: store.__setitem__(k, v))
    gen = PropertyAmenityMapGenerator(radius_km=1.5)
    key = amenity_map_generator.create_cache_key(
        'amenity_map_pois_v1', round(MANCHESTER[0], 3), round(MANCHESTER[1], 3),
        int(gen.radius_m))
    store[key] = {"fetched_at": _time.time(),
                  "amenities": {k: [] for k in gen.amenity_config}}  # fresh but poisoned

    monkeypatch.setattr(amenity_map_generator, "overpass_request",
                        lambda *a, **k: _ELEMENTS)
    res = gen.fetch_all_amenities(*MANCHESTER)
    assert sum(len(v) for v in res.values()) == 3          # live data, not the cached zero
    assert sum(len(v) for v in store[key]["amenities"].values()) == 3  # cache overwritten


def test_batched_query_is_single_union():
    gen = PropertyAmenityMapGenerator(radius_km=1.5)
    q = gen._build_combined_query(*MANCHESTER)
    # One statement per DISTINCT tag selector. The three restaurant categories
    # share amenity=restaurant, so 10 categories collapse to 8 selectors.
    assert q.count("nwr[") == 8
    assert q.count("out center;") == 1
    assert 'nwr["amenity"="restaurant"]' in q


def test_cache_hit_path_skips_network(monkeypatch):
    # In-memory cache so the test is hermetic and independent of runtime_cache.sqlite3.
    store = {}
    monkeypatch.setattr(amenity_map_generator, "get_from_cache", store.get)
    monkeypatch.setattr(amenity_map_generator, "set_to_cache",
                        lambda k, v: store.__setitem__(k, v))

    hits = {"n": 0}

    def fake_overpass(query, timeout=30, max_rounds=2, expect_nonempty=False):
        hits["n"] += 1
        return _ELEMENTS

    monkeypatch.setattr(amenity_map_generator, "overpass_request", fake_overpass)

    gen = PropertyAmenityMapGenerator(radius_km=1.5)
    first = gen.fetch_all_amenities(*MANCHESTER)
    second = gen.fetch_all_amenities(*MANCHESTER)

    assert hits["n"] == 1  # second call served from cache
    assert first["supermarkets"] and first["restaurants_chinese"]
    assert sum(len(v) for v in first.values()) == sum(len(v) for v in second.values())


def test_cuisine_split_and_classification(monkeypatch):
    monkeypatch.setattr(amenity_map_generator, "get_from_cache", lambda k: None)
    monkeypatch.setattr(amenity_map_generator, "set_to_cache", lambda k, v: None)
    monkeypatch.setattr(amenity_map_generator, "overpass_request",
                        lambda *a, **k: _ELEMENTS)
    gen = PropertyAmenityMapGenerator(radius_km=1.5)
    res = gen.fetch_all_amenities(*MANCHESTER)
    assert len(res["supermarkets"]) == 1
    assert len(res["restaurants_chinese"]) == 1   # cuisine=chinese matched
    assert len(res["restaurants_italian"]) == 0   # not matched
    assert len(res["parks"]) == 1                 # from a 'way' element


def test_degradation_banner_present_only_when_unavailable():
    gen = PropertyAmenityMapGenerator(radius_km=1.5)
    prop = {"address": "Test", "geo_location": f"{MANCHESTER[0]}, {MANCHESTER[1]}"}
    html_down = gen.generate_map_html(prop, {}, amenities_unavailable=True)
    html_zero = gen.generate_map_html(prop, {}, amenities_unavailable=False)
    assert "temporarily unavailable" in html_down
    assert "temporarily unavailable" not in html_zero
