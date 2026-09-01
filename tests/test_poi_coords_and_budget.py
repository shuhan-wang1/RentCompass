# -*- coding: utf-8 -*-
"""POI lookups for a LISTING: use the coordinates we already have, and stop spending the
tool budget re-deriving them.

The defect this pins, from one production turn that compared two focused listings and
reported "no supermarkets or convenience stores nearby" for both:

  * ``search_nearby_pois`` only ever took an ``address`` string, and the strings it gets are
    OnTheMarket DISPLAY names. "Rugby House 6 Great Ormond Street, Islington WC1N" geocoded
    to NOTHING (one comma, so the drop-the-building-name step was skipped; outward-only
    postcode, so the postcode step was skipped — the whole ladder was one failed lookup),
    and "Caledonian Road, London" geocoded to the middle of a 2 km road. Both listings had
    ``geo_location`` in the cache the entire time.
  * The model issued one call per POI type per listing — 12 calls, 15 geocodes of 5 distinct
    strings, and 9 of them killed by the 25s per-call cap. The two that survived returned
    restaurants and a tube station, which is exactly what the answer contained.

Network is never touched: the geocoder is monkeypatched and the agent_loop helpers are
AST-extracted (pure), so this holds wherever langgraph is absent.
"""
import ast
import os
import sys
from pathlib import Path

import pytest

_APP = str(Path(__file__).resolve().parents[1] / "app")
if _APP in sys.path:
    sys.path.remove(_APP)
sys.path.insert(0, _APP)
for _mod in [m for m in list(sys.modules) if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_mod], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_mod]

from core.tools import search_nearby_pois as poi  # noqa: E402

_LOOP_PATH = os.path.join(str(Path(__file__).resolve().parents[1]), "app", "core",
                          "agent_loop.py")


def _load_loop_symbols(wanted):
    with open(_LOOP_PATH, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=_LOOP_PATH)
    picked = [n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted]
    consts = [n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id.startswith("_POI_") for t in n.targets)]
    ns = {"AgentState": dict, "os": os}
    exec(compile(ast.Module(body=consts + picked, type_ignores=[]), _LOOP_PATH, "exec"), ns)
    missing = wanted - ns.keys()
    assert not missing, f"failed to extract {missing} from agent_loop.py"
    return ns


_LOOP = _load_loop_symbols({"_inject_poi_coords", "_listing_coords_for", "_known_listings",
                            "_canonical_poi_args", "_poi_types_of", "_sorted_poi_types",
                            "_prioritised_poi_types", "_focus_records", "_ref_matches_focus",
                            "_norm_ref"})
_inject_poi_coords = _LOOP["_inject_poi_coords"]
_canonical_poi_args = _LOOP["_canonical_poi_args"]

_RUGBY = {"address": "Rugby House 6 Great Ormond Street, Islington WC1N",
          "price": "£1500/month", "url": "https://otm/19116284/",
          "geo_location": "51.522441, -0.118633"}
_CALEDONIAN = {"address": "Caledonian Road, London", "price": "£1700/month",
               "url": "https://otm/16980291/", "geo_location": "51.539, -0.117"}


# ══════════════════════════════════════════════════════════════════════════
# 1) The geocode ladder — the address that produced exactly one failed attempt
# ══════════════════════════════════════════════════════════════════════════
def test_display_name_now_yields_street_and_postcode_variants():
    variants = poi.address_variants(_RUGBY["address"])
    assert variants[0] == _RUGBY["address"]                     # as given, first
    lowered = [v.lower() for v in variants]
    # The two that a live Nominatim actually resolves (the model reached them only by
    # rewriting the string itself, one 25s call at a time).
    assert any(v.startswith("6 great ormond street") for v in lowered)
    assert any(v.startswith("great ormond street") for v in lowered)
    assert any(v.startswith("wc1n") for v in lowered)           # outward-only postcode


def test_ladder_never_offers_a_variant_that_relocates_the_search():
    # Dropping the first part of "Caledonian Road, London" leaves "London", which geocodes
    # SUCCESSFULLY to the centre of the city. A silent 10 km relocation is worse than a miss.
    assert poi.address_variants("Caledonian Road, London") == ["Caledonian Road, London"]
    assert poi.address_variants("London") == []
    assert poi.address_variants("London, UK") == []


def test_ladder_keeps_the_building_name_case_it_already_handled():
    variants = poi.address_variants("Tufnell House, 144 Huddleston Road, London N7 0EG, UK")
    assert "144 Huddleston Road, London N7 0EG, UK" in variants
    assert "N7 0EG" in variants


# ══════════════════════════════════════════════════════════════════════════
# 2) Geocode memo — the same string is not re-derived per call
# ══════════════════════════════════════════════════════════════════════════
class _CountingGeocoder:
    """Stands in for Nominatim; counts network attempts."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def geocode(self, query, **_kw):
        self.calls.append(query)
        hit = self.answers.get(query)
        if hit is None:
            return None
        return type("Loc", (), {"latitude": hit[0], "longitude": hit[1]})()


@pytest.fixture(autouse=True)
def _clear_geocode_cache(monkeypatch):
    # query_osm_pois imports the process-wide persistent cache functions directly.
    # Give this module a fresh in-memory cell store per test: cache-hit behaviour
    # remains covered inside each test, while a previous pytest process can no
    # longer short-circuit mocked Overpass calls or mutate the application cache.
    poi_result_cache = {}
    monkeypatch.setattr(
        poi,
        "get_from_cache",
        lambda key, **_kwargs: poi_result_cache.get(key),
    )
    monkeypatch.setattr(
        poi,
        "set_to_cache",
        lambda key, value, **_kwargs: poi_result_cache.__setitem__(key, value),
    )
    poi.geocode_cache_clear()
    yield
    poi.geocode_cache_clear()


def _patch_geocoder(monkeypatch, answers):
    g = _CountingGeocoder(answers)
    monkeypatch.setattr(poi, "Nominatim", lambda **kw: g)
    monkeypatch.setattr(poi.time, "sleep", lambda *_a, **_k: None)
    return g


def test_repeated_geocode_of_the_same_address_hits_the_cache(monkeypatch):
    g = _patch_geocoder(monkeypatch, {"Great Ormond Street, London WC1N": (51.5224, -0.1186)})
    first = poi.geocode_address("Great Ormond Street, London WC1N")
    assert first == (51.5224, -0.1186)
    before = len(g.calls)
    for _ in range(4):
        assert poi.geocode_address("great ormond street,  LONDON WC1N") == first
    assert len(g.calls) == before, "a cached address must not touch the geocoder again"


def test_failures_are_cached_too(monkeypatch):
    g = _patch_geocoder(monkeypatch, {})
    assert poi.geocode_address("Nowhere Street, ZZ99") is None
    attempts = len(g.calls)
    assert attempts >= 1
    assert poi.geocode_address("Nowhere Street, ZZ99") is None
    assert len(g.calls) == attempts, "a known-failing ladder must not be re-walked"


def test_geocode_stops_at_the_deadline(monkeypatch):
    g = _patch_geocoder(monkeypatch, {})           # every variant misses
    assert poi.geocode_address(_RUGBY["address"], deadline=poi.time.monotonic() - 1) is None
    assert g.calls == [], "no variant may be attempted after the deadline"
    # A cut-short ladder is not a verdict: it must not poison the cache.
    g2 = _patch_geocoder(monkeypatch, {_RUGBY["address"]: (51.5224, -0.1186)})
    assert poi.geocode_address(_RUGBY["address"]) == (51.5224, -0.1186)
    assert g2.calls, "the truncated attempt must not have been cached as a failure"


# ══════════════════════════════════════════════════════════════════════════
# 3) Coordinates we already own — parsing, injection, and skipping geocoding
# ══════════════════════════════════════════════════════════════════════════
def test_parse_geo_location_accepts_both_scraper_shapes_and_rejects_junk():
    assert poi.parse_geo_location("51.522441, -0.118633") == (51.522441, -0.118633)
    assert poi.parse_geo_location({"lat": 51.5, "lng": -0.12}) == (51.5, -0.12)
    assert poi.parse_geo_location("0, 0") is None            # outside the UK box
    assert poi.parse_geo_location("") is None
    assert poi.parse_geo_location("not, coords") is None


def test_supplied_coordinates_skip_geocoding_entirely(monkeypatch):
    g = _patch_geocoder(monkeypatch, {})     # any geocode attempt would return None -> error
    monkeypatch.setattr(poi, "query_osm_pois", lambda *a, **k: [])
    out = poi.search_nearby_pois_impl(address=_RUGBY["address"], poi_type="supermarket",
                                      latitude=51.522441, longitude=-0.118633)
    assert out["success"] is True, "the address that cannot be geocoded must still work"
    assert g.calls == []
    ref = out["reference_point"]
    assert ref["is_specific_address"] is True
    assert "supplied with the request" in ref["measured_from"]


def test_junk_coordinates_fall_back_to_geocoding(monkeypatch):
    g = _patch_geocoder(monkeypatch, {"Great Ormond Street, London WC1N": (51.5224, -0.1186)})
    monkeypatch.setattr(poi, "query_osm_pois", lambda *a, **k: [])
    out = poi.search_nearby_pois_impl(address="Great Ormond Street, London WC1N",
                                      poi_type="supermarket", latitude=0, longitude=0)
    assert out["success"] is True
    assert g.calls, "coordinates outside the UK box are not usable; geocoding must run"


def test_injection_fills_coords_from_the_focused_listing():
    state = {"extracted_context": {"focus_stack": [_CALEDONIAN, _RUGBY]}}
    args = _inject_poi_coords({"address": _RUGBY["address"], "poi_type": "supermarket"}, state)
    assert (args["latitude"], args["longitude"]) == (51.522441, -0.118633)


def test_injection_reads_last_results_and_the_registry():
    for key, rec in (("last_results", _RUGBY), ("recommended_registry", _RUGBY)):
        args = _inject_poi_coords({"address": _RUGBY["address"]},
                                  {"extracted_context": {key: [rec]}})
        assert (args["latitude"], args["longitude"]) == (51.522441, -0.118633), key


def test_injection_never_borrows_another_listings_coordinates():
    state = {"extracted_context": {"focus_stack": [_RUGBY]}}
    # A different building on the same street is NOT the focused listing.
    args = _inject_poi_coords({"address": "Rugby House 9 Great Ormond Street, WC1N"}, state)
    assert "latitude" not in args
    # An area question is not a listing question either.
    assert "latitude" not in _inject_poi_coords({"address": "Hackney"}, state)


def test_injection_never_overrides_model_supplied_coordinates():
    state = {"extracted_context": {"focus_stack": [_RUGBY]}}
    args = _inject_poi_coords({"address": _RUGBY["address"], "latitude": 51.5,
                               "longitude": -0.1}, state)
    assert (args["latitude"], args["longitude"]) == (51.5, -0.1)


def test_injection_is_a_noop_without_coordinates_in_context():
    args = _inject_poi_coords({"address": _RUGBY["address"]},
                              {"extracted_context": {"focus_stack": [
                                  {k: v for k, v in _RUGBY.items() if k != "geo_location"}]}})
    assert "latitude" not in args


# ══════════════════════════════════════════════════════════════════════════
# 4) One call per address instead of one per type
# ══════════════════════════════════════════════════════════════════════════
def test_multi_type_poi_type_is_parsed_as_a_list():
    assert poi._requested_types("supermarket,convenience,tube_station") == [
        "supermarket", "convenience", "tube_station"]
    assert poi._requested_types("restaurant, cafe") == ["restaurant", "cafe"]
    assert poi._requested_types("supermarket") == ["supermarket"]
    # unknown / "all" keep the tool's own inference and fuzzy matching
    assert poi._requested_types("all") == []
    assert poi._requested_types("grocery store") == []


def test_one_geocode_and_one_deadline_cover_every_requested_type(monkeypatch):
    queried = []
    monkeypatch.setattr(poi, "query_osm_pois",
                        lambda lat, lon, ptype, *a, **k: queried.append(ptype) or [])
    monkeypatch.setattr(poi, "_resolve_nearest_station", lambda *a, **k: None)
    g = _patch_geocoder(monkeypatch, {})
    out = poi.search_nearby_pois_impl(address=_RUGBY["address"],
                                      poi_type="supermarket,convenience,restaurant",
                                      latitude=51.522441, longitude=-0.118633)
    assert out["success"] is True
    assert queried == ["supermarket", "convenience", "restaurant"]
    assert g.calls == []


def test_per_type_fanout_collapses_to_one_call_per_address():
    batch = [
        {"name": "search_nearby_pois", "args": {"address": _RUGBY["address"],
                                                "poi_type": "supermarket", "radius": 500}},
        {"name": "search_nearby_pois", "args": {"address": _RUGBY["address"],
                                                "poi_type": "convenience", "radius": 300}},
        {"name": "search_nearby_pois", "args": {"address": _RUGBY["address"],
                                                "poi_type": "tube_station"}},
        {"name": "search_nearby_pois", "args": {"address": _CALEDONIAN["address"],
                                                "poi_type": "supermarket"}},
        {"name": "get_property_details", "args": {"property_url": "https://otm/1/"}},
    ]
    canon = _canonical_poi_args(batch)
    key = " ".join(_RUGBY["address"].split()).lower()
    merged = canon[key]
    assert merged["poi_type"] == "supermarket,convenience,tube_station"  # POI_TYPES order
    assert merged["radius"] == 500              # widest wins
    # Caledonian Road had a single type in this batch: nothing to merge, left as issued.
    assert " ".join(_CALEDONIAN["address"].split()).lower() not in canon


def test_merged_calls_share_one_digest_so_duplicates_are_not_paid_for_twice():
    # This is the mechanism: identical args -> identical digest -> the existing no-progress
    # guard answers calls 2..N from the first result.
    batch = [
        {"name": "search_nearby_pois", "args": {"address": _RUGBY["address"],
                                                "poi_type": t}}
        for t in ("supermarket", "convenience", "cafe")
    ]
    canon = _canonical_poi_args(batch)
    key = " ".join(_RUGBY["address"].split()).lower()
    assert len({repr(sorted(canon[key].items())) for _ in batch}) == 1
    assert canon[key]["poi_type"] == "supermarket,convenience,cafe"


def test_an_all_call_plus_explicit_types_names_every_type():
    batch = [
        {"name": "search_nearby_pois", "args": {"address": _RUGBY["address"],
                                                "poi_type": "all",
                                                "user_query": "附近有超市吗"}},
        {"name": "search_nearby_pois", "args": {"address": _RUGBY["address"],
                                                "poi_type": "tube_station"}},
        {"name": "search_nearby_pois", "args": {"address": _RUGBY["address"],
                                                "poi_type": "pharmacy"}},
    ]
    merged = _canonical_poi_args(batch)[" ".join(_RUGBY["address"].split()).lower()]
    assert merged["poi_type"] == "pharmacy,tube_station"  # POI_TYPES order
    assert merged["user_query"] == "附近有超市吗"


def test_a_single_poi_call_is_left_untouched():
    batch = [{"name": "search_nearby_pois",
              "args": {"address": _RUGBY["address"], "poi_type": "all",
                       "user_query": "附近有什么"}}]
    assert _canonical_poi_args(batch) == {}


# ══════════════════════════════════════════════════════════════════════════
# 5) Round two — what the first fix left behind, measured in production
#
# With coordinates injected and the calls merged, the turn still answered "查询周边设施时
# 超时了". The logs said why:
#   * the merged call carried EIGHT types; the internal budget covered three, printed
#     "预算已用尽，跳过剩余类型: pharmacy, gym, park, bus_stop, tube_station", and the
#     per-call cap then killed the call — discarding the 1 and 2 supermarkets it HAD found.
#   * a bare "Stratford" geocoded to 52.192780, -1.706340 — Stratford-upon-Avon, 150 km from
#     the listing — and burned a whole 25s slot describing Warwickshire.
# ══════════════════════════════════════════════════════════════════════════
def test_bare_london_area_is_asked_for_as_london_first():
    variants = poi.address_variants("Stratford")
    assert variants[0] == "Stratford, London, UK", variants
    assert "Stratford" in variants          # the bare form is still tried, just not first


def test_london_bias_applies_only_to_the_curated_london_half():
    # Manchester is a city in its own right: it must never be asked for as a London area.
    assert poi.address_variants("Manchester") == ["Manchester"]
    assert not any("London" in v for v in poi.address_variants("Manchester"))
    # A full address is not an area name; the ladder is unchanged for it.
    assert poi.address_variants("Caledonian Road, London")[0] == "Caledonian Road, London"


def test_known_london_areas_come_from_the_one_area_table():
    from core.tools.search_properties import LONDON_AREAS, _KNOWN_AREAS
    assert LONDON_AREAS["stratford"] == "Stratford"
    assert "manchester" not in LONDON_AREAS
    # LONDON_AREAS is the London half of the same table, not a second copy of it.
    assert set(LONDON_AREAS) <= set(_KNOWN_AREAS)


def test_geocode_asks_for_gb_only(monkeypatch):
    seen = {}

    class _G:
        def geocode(self, query, **kw):
            seen.update(kw)
            return type("Loc", (), {"latitude": 51.5, "longitude": -0.1})()

    monkeypatch.setattr(poi, "Nominatim", lambda **kw: _G())
    monkeypatch.setattr(poi.time, "sleep", lambda *_a, **_k: None)
    assert poi.geocode_address("Somewhere Road, N7") == (51.5, -0.1)
    assert seen.get("country_codes") == "gb"


def test_merged_type_union_is_capped():
    many = ["supermarket", "convenience", "cafe", "pharmacy", "gym", "park", "bus_stop",
            "tube_station"]
    batch = [{"name": "search_nearby_pois",
              "args": {"address": _RUGBY["address"], "poi_type": t}} for t in many]
    merged = _canonical_poi_args(batch)[" ".join(_RUGBY["address"].split()).lower()]
    types = merged["poi_type"].split(",")
    assert len(types) == _LOOP["_POI_MERGE_MAX_TYPES"], merged["poi_type"]
    assert all(t in many for t in types)


def test_the_users_own_words_decide_which_types_survive_the_cap():
    # "超市、便利店" -> supermarket + convenience must be in the surviving four even though
    # the model also asked for a gym, a park and a bank on its own initiative.
    batch = [
        {"name": "search_nearby_pois", "args": {"address": _RUGBY["address"],
                                                "poi_type": "gym"}},
        {"name": "search_nearby_pois", "args": {"address": _RUGBY["address"],
                                                "poi_type": "park"}},
        {"name": "search_nearby_pois", "args": {"address": _RUGBY["address"],
                                                "poi_type": "bank"}},
        {"name": "search_nearby_pois", "args": {"address": _RUGBY["address"],
                                                "poi_type": "convenience"}},
        {"name": "search_nearby_pois", "args": {"address": _RUGBY["address"],
                                                "poi_type": "supermarket",
                                                "user_query": "周边有什么超市，便利店"}},
    ]
    merged = _canonical_poi_args(batch)[" ".join(_RUGBY["address"].split()).lower()]
    types = merged["poi_type"].split(",")
    assert types[:2] == ["supermarket", "convenience"], types
    assert len(types) == _LOOP["_POI_MERGE_MAX_TYPES"]


# ── the per-type result cache: work already paid for is not re-fetched ────────
def _overpass_elements(name="Tesco Express", shop="convenience"):
    return {"elements": [{"type": "node", "id": 1, "lat": 51.5225, "lon": -0.1187,
                          "tags": {"name": name, "shop": shop}}]}


def test_a_found_type_is_served_from_cache_on_the_next_call(monkeypatch):
    calls = []

    def fake_overpass(query, **kw):
        calls.append(query)
        return _overpass_elements()

    monkeypatch.setattr(poi, "overpass_request", fake_overpass)
    # A distinctive centre so this test cannot collide with a neighbour's cache cell.
    lat, lon = 51.987654, -0.123456
    first = poi.query_osm_pois(lat, lon, "convenience", radius=500)
    assert first and len(calls) == 1
    again = poi.query_osm_pois(lat, lon, "convenience", radius=500)
    assert [p["name"] for p in again] == [p["name"] for p in first]
    assert len(calls) == 1, "a cached type must not hit Overpass again"
    # A DIFFERENT type at the same centre is a different cell and must still be fetched.
    monkeypatch.setattr(poi, "overpass_request",
                        lambda q, **kw: calls.append(q) or _overpass_elements("Tesco", "supermarket"))
    poi.query_osm_pois(lat, lon, "supermarket", radius=500)
    assert len(calls) == 2


def test_an_overpass_failure_is_never_cached(monkeypatch):
    def boom(query, **kw):
        raise poi.OverpassError("all mirrors down")

    monkeypatch.setattr(poi, "overpass_request", boom)
    lat, lon = 51.876543, -0.234567
    with pytest.raises(RuntimeError):
        poi.query_osm_pois(lat, lon, "supermarket", radius=500)
    hits = []
    monkeypatch.setattr(poi, "overpass_request",
                        lambda q, **kw: hits.append(q) or _overpass_elements("Tesco", "supermarket"))
    assert poi.query_osm_pois(lat, lon, "supermarket", radius=500)
    assert len(hits) == 1, "the failed call must not have poisoned the cell"


# ══════════════════════════════════════════════════════════════════════════
# 6) Round three — shrink the retrieval footprint, and never read a rate-limited
#    mirror's empty body as "none nearby".
#
# Measured from the production host on 2026-07-29, after a day of POI traffic:
#   overpass-api.de / lz4 / z  -> ConnectionError within ~1s (DNS resolved fine)
#   overpass.kumi.systems      -> ReadTimeout at 30s
#   overpass.osm.ch            -> HTTP 200 in 0.15s with elements=0, even for cafés
#                                 within 1 km of Leicester Square
# Every query re-walked that pool from the top, so one turn spent 82s and completed nothing.
# ══════════════════════════════════════════════════════════════════════════
from core import maps_service  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_mirror_state():
    maps_service.overpass_mirror_state_reset()
    yield
    maps_service.overpass_mirror_state_reset()


class _Resp:
    def __init__(self, status=200, payload=None, exc=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"elements": []}
        self.exc = exc
        self.content = b"{}"

    def json(self):
        return self._payload


def _patch_post(monkeypatch, behaviour):
    """behaviour: {host -> _Resp | Exception}. Records the hosts hit, in order."""
    hits = []

    def fake_post(url, **kw):
        host = url.split('/')[2]
        hits.append(host)
        outcome = behaviour.get(host, _Resp(payload={"elements": [
            {"type": "node", "id": 1, "lat": 51.5225, "lon": -0.1187,
             "tags": {"name": "Sainsbury's Local", "shop": "convenience"}}]}))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(maps_service.requests, "post", fake_post)
    monkeypatch.setattr(maps_service.time, "sleep", lambda *_a, **_k: None)
    return hits


def test_a_failed_mirror_is_skipped_on_the_next_request(monkeypatch):
    import requests as _rq
    dead = _rq.exceptions.ConnectionError("refused")
    hits = _patch_post(monkeypatch, {"overpass-api.de": dead})
    maps_service.overpass_request("q")           # walks past the dead one, succeeds on #2
    assert hits[0] == "overpass-api.de" and len(hits) == 2
    hits.clear()
    maps_service.overpass_request("q")           # the dead one is in cooldown now
    assert "overpass-api.de" not in hits, hits


def test_the_last_working_mirror_is_tried_first(monkeypatch):
    import requests as _rq
    hits = _patch_post(monkeypatch, {
        "overpass-api.de": _rq.exceptions.ConnectionError("refused"),
        "overpass.kumi.systems": _rq.exceptions.ReadTimeout("30s"),
    })
    maps_service.overpass_request("q")
    good = hits[-1]
    hits.clear()
    maps_service.overpass_request("q")
    assert hits[0] == good, hits


def test_every_mirror_penalised_still_asks_rather_than_giving_up(monkeypatch):
    import requests as _rq
    behaviour = {url.split('/')[2]: _rq.exceptions.ConnectionError("refused")
                 for url in maps_service.OVERPASS_MIRRORS}
    _patch_post(monkeypatch, behaviour)
    with pytest.raises(maps_service.OverpassError):
        maps_service.overpass_request("q")
    # All five are now in cooldown; the pool must not become empty.
    assert len(maps_service._mirrors_to_try()) == len(maps_service.OVERPASS_MIRRORS)


def test_requests_are_paced(monkeypatch):
    slept = []
    monkeypatch.setattr(maps_service.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(maps_service, "OVERPASS_MIN_INTERVAL_S", 1.0)
    _patch_post(monkeypatch, {})
    monkeypatch.setattr(maps_service.time, "sleep", lambda s: slept.append(s))
    maps_service.overpass_request("q")
    maps_service.overpass_request("q")          # immediately after -> must wait
    assert any(s > 0 for s in slept), slept


def test_an_empty_200_is_not_reported_as_none_nearby(monkeypatch):
    """The osm.ch case: a rate-limited mirror answers 200/elements=0. The tool must ask
    another mirror rather than telling the user there are no supermarkets."""
    empty, full = _Resp(payload={"elements": []}), _Resp(payload={
        "elements": [{"type": "node", "id": 7, "lat": 51.5225, "lon": -0.1187,
                      "tags": {"name": "Sainsbury's Local", "shop": "convenience"}}]})
    hits = _patch_post(monkeypatch, {"overpass-api.de": empty,
                                     "overpass.kumi.systems": full})
    pois = poi.query_osm_pois(51.111111, -0.111111, "convenience", radius=300)
    assert [p["name"] for p in pois] == ["Sainsbury's Local"], pois
    assert hits[0] == "overpass-api.de" and len(hits) >= 2


def test_a_confirmed_empty_cell_is_still_reported_as_empty(monkeypatch):
    # No gym within 300 m is a real answer. When every mirror says empty, say empty.
    _patch_post(monkeypatch, {url.split('/')[2]: _Resp(payload={"elements": []})
                              for url in maps_service.OVERPASS_MIRRORS})
    assert poi.query_osm_pois(51.222222, -0.222222, "gym", radius=300) == []


def test_an_unconfirmed_empty_is_not_cached(monkeypatch):
    behaviour = {url.split('/')[2]: _Resp(payload={"elements": []})
                 for url in maps_service.OVERPASS_MIRRORS}
    hits = _patch_post(monkeypatch, behaviour)
    lat, lon = 51.333333, -0.333333
    assert poi.query_osm_pois(lat, lon, "gym", radius=300) == []
    before = len(hits)
    # A second call must go back to the network: one bad minute must not answer for 15.
    poi.query_osm_pois(lat, lon, "gym", radius=300)
    assert len(hits) > before


# ── one radius, smaller, with a ceiling ──────────────────────────────────────
def test_one_radius_default_everywhere():
    import inspect
    assert poi.DEFAULT_RADIUS == 300
    sig = inspect.signature(poi.search_nearby_pois_impl)
    assert sig.parameters["radius"].default == poi.DEFAULT_RADIUS
    assert (poi.search_nearby_pois_tool.parameters["properties"]["radius"]["default"]
            == poi.DEFAULT_RADIUS)
    assert inspect.signature(poi.query_osm_pois).parameters["radius"].default == poi.DEFAULT_RADIUS


def test_radius_is_clamped():
    assert poi.clamp_radius(None) == poi.DEFAULT_RADIUS
    assert poi.clamp_radius("nonsense") == poi.DEFAULT_RADIUS
    assert poi.clamp_radius(0) == poi.DEFAULT_RADIUS
    assert poi.clamp_radius(150) == 150
    assert poi.clamp_radius(5000) == poi.MAX_RADIUS


def test_the_impl_applies_the_ceiling(monkeypatch):
    seen = []
    monkeypatch.setattr(poi, "query_osm_pois",
                        lambda lat, lon, ptype, radius, **k: seen.append(radius) or [])
    poi.search_nearby_pois_impl(address="x", poi_type="supermarket", radius=5000,
                                latitude=51.5224, longitude=-0.1186)
    assert seen == [poi.MAX_RADIUS]


def test_merged_union_now_capped_at_three():
    batch = [{"name": "search_nearby_pois", "args": {"address": _RUGBY["address"],
                                                     "poi_type": t}}
             for t in ("supermarket", "convenience", "cafe", "pharmacy", "gym")]
    merged = _canonical_poi_args(batch)[" ".join(_RUGBY["address"].split()).lower()]
    assert len(merged["poi_type"].split(",")) == 3, merged["poi_type"]


# ── the walk itself must respect the deadline ─────────────────────────────────
def test_the_mirror_walk_stops_at_the_deadline(monkeypatch):
    """Measured before this: a rate-limited pool turned one POI call into 85s, because the
    deadline was only checked BETWEEN types while a single type could walk five mirrors twice
    with a 30s read timeout on each."""
    import requests as _rq
    hits = _patch_post(monkeypatch, {url.split('/')[2]: _rq.exceptions.ConnectionError("x")
                                     for url in maps_service.OVERPASS_MIRRORS})
    with pytest.raises(maps_service.OverpassError):
        maps_service.overpass_request("q", deadline=maps_service.time.monotonic() - 1)
    assert hits == [], "no mirror may be tried after the deadline"


def test_each_attempt_is_clamped_to_the_remaining_time(monkeypatch):
    seen = []

    def fake_post(url, **kw):
        seen.append(kw.get("timeout"))
        raise __import__("requests").exceptions.ConnectionError("x")

    monkeypatch.setattr(maps_service.requests, "post", fake_post)
    monkeypatch.setattr(maps_service.time, "sleep", lambda *_a, **_k: None)
    with pytest.raises(maps_service.OverpassError):
        maps_service.overpass_request("q", timeout=30,
                                      deadline=maps_service.time.monotonic() + 5)
    assert seen, "expected at least one attempt"
    assert all(t <= 5.0 for t in seen), seen      # never the full 30s


def test_all_mirrors_empty_raises_overpass_empty_with_the_payload(monkeypatch):
    _patch_post(monkeypatch, {url.split('/')[2]: _Resp(payload={"elements": []})
                              for url in maps_service.OVERPASS_MIRRORS})
    with pytest.raises(maps_service.OverpassEmpty) as ei:
        maps_service.overpass_request("q", expect_nonempty=True)
    assert ei.value.payload == {"elements": []}
    # A subclass, so every existing `except OverpassError` handler still degrades honestly.
    assert isinstance(ei.value, maps_service.OverpassError)


def test_one_walk_not_two_when_every_mirror_is_empty(monkeypatch):
    hits = _patch_post(monkeypatch, {url.split('/')[2]: _Resp(payload={"elements": []})
                                     for url in maps_service.OVERPASS_MIRRORS})
    assert poi.query_osm_pois(51.444444, -0.444444, "gym", radius=300) == []
    # 5 mirrors x 2 rounds, ONCE. The first version of this confirmation logic asked a second
    # time to find out whether the emptiness was real, doubling an outage's cost.
    assert len(hits) <= len(maps_service.OVERPASS_MIRRORS) * 2, hits
