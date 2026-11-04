"""The local OpenStreetMap POI index answers the app's Overpass queries offline.

On 2026-11-03 the reference Overpass server answered 502 to everyone and the mirrors either
timed out or held no London data, so every amenity question degraded to "temporarily
unavailable". These tests build a tiny extract with the real converter and check that the
app's own queries are answered from it, that anything it cannot answer completely still goes
to the network, and that an empty answer inside the extract is a real "none nearby".
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import core.maps_service as maps_service
import core.osm_poi_index as osm

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "_build_osm_poi_index", _ROOT / "scripts" / "build_osm_poi_index.py")
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)

CENTRE = (51.5235, -0.1197)          # Guilford Street, Bloomsbury
# A square boundary around central London: lon -0.20..-0.05, lat 51.48..51.56.
POLY = """test-area
1
   -0.20  51.48
   -0.05  51.48
   -0.05  51.56
   -0.20  51.56
   -0.20  51.48
END
END
"""


def _point(fid, lat, lon, **tags):
    return {"type": "Feature", "id": fid, "properties": tags,
            "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def _square(fid, lat, lon, half, **tags):
    ring = [[lon - half, lat - half], [lon + half, lat - half], [lon + half, lat + half],
            [lon - half, lat + half], [lon - half, lat - half]]
    return {"type": "Feature", "id": fid, "properties": tags,
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


FEATURES = [
    _point("n1", 51.5240, -0.1195, amenity="cafe", name="Near Cafe"),
    _point("n2", 51.5255, -0.1210, shop="supermarket", brand="Tesco", name="Tesco Express"),
    _point("n3", 51.5230, -0.1180, amenity="restaurant", cuisine="chinese;noodle", name="Noodles"),
    _point("n4", 51.5228, -0.1205, amenity="doctors", name="Surgery"),
    _point("n5", 51.5237, -0.1190, highway="bus_stop", name="Guilford Street"),
    _point("n6", 51.5236, -0.1196, entrance="yes"),            # referenced node, not a POI
    _point("n7", 51.5280, -0.1240, railway="station", station="subway", name="Russell Square"),
    _point("n8", 51.5500, -0.1197, amenity="cafe", name="Far Cafe"),       # ~2.9 km north
    _square("a20", 51.5245, -0.1220, 0.0008, leisure="park", name="Square Garden"),  # way 10
    _square("a11", 51.5220, -0.1170, 0.0005, leisure="park", name="Relation Park"),  # rel 5
    _point("n9", 51.5236, -0.1199, highway="crossing"),        # partial key, value not kept
]


@pytest.fixture
def index_file(tmp_path, monkeypatch):
    seq = tmp_path / "poi.geojsonseq"
    seq.write_text("".join("\x1e" + json.dumps(f) + "\n" for f in FEATURES), encoding="utf-8")
    poly = tmp_path / "region.poly"
    poly.write_text(POLY, encoding="utf-8")
    out = tmp_path / "poi.sqlite3"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stats = builder.build(seq, poly, out, source_url="test", source_md5="0" * 32,
                          source_timestamp=stamp, region="test-area")
    monkeypatch.setenv("OSM_POI_INDEX_PATH", str(out))
    monkeypatch.delenv("OSM_POI_INDEX_MAX_AGE_DAYS", raising=False)
    osm.reset_for_tests()
    yield out, stats
    osm.reset_for_tests()


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    def _refuse(*a, **k):
        calls.append(a)
        raise AssertionError("the network was used for a query the index covers")

    monkeypatch.setattr(maps_service.requests, "post", _refuse)
    return calls


def _around(selector, radius, lat=CENTRE[0], lon=CENTRE[1], out="out center;"):
    return (f"[out:json][timeout:25];\n(\n  node{selector}(around:{radius},{lat},{lon});\n"
            f"  way{selector}(around:{radius},{lat},{lon});\n"
            f"  relation{selector}(around:{radius},{lat},{lon});\n);\n{out}")


def _names(payload):
    return sorted(e["tags"].get("name", "") for e in payload["elements"])


# ── the build ─────────────────────────────────────────────────────────────────

def test_the_build_keeps_only_indexed_tags_and_decodes_area_ids(index_file):
    out, stats = index_file
    assert stats["skipped"] == 2                       # the entrance node and the crossing
    assert (stats["nodes"], stats["ways"], stats["relations"]) == (7, 1, 1)
    conn = sqlite3.connect(out)
    kinds = dict(conn.execute(
        "SELECT osm_type || osm_id, tags FROM poi WHERE osm_type != 'n'").fetchall())
    assert set(kinds) == {"w10", "r5"}
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta["schema_version"] == osm.SCHEMA_VERSION
    assert meta["filters"] == " ".join(osm.osmium_filter_expressions())


def test_the_build_script_takes_its_filters_from_the_index_module():
    script = (_ROOT / "deploy" / "build_osm_poi_index.sh").read_text(encoding="utf-8")
    assert "--print-osmium-filters" in script
    assert "md5sum" in script and "--network none" in script
    assert "debian@sha256:" in script
    assert "mv -f" in script                           # atomic replace of the live file


def test_a_way_is_reported_at_its_bounding_box_centre_like_overpass_center():
    lat, lon = builder.representative_point(_square("a2", 51.5, -0.1, 0.001)["geometry"])
    assert lat == pytest.approx(51.5) and lon == pytest.approx(-0.1)


# ── answering the app's own queries ───────────────────────────────────────────

def test_a_radius_query_returns_what_is_inside_and_nothing_beyond(index_file, no_network):
    payload = maps_service.overpass_request(_around('["amenity"="cafe"]', 500))
    assert _names(payload) == ["Near Cafe"]
    wide = maps_service.overpass_request(_around('["amenity"="cafe"]', 1500), timeout=5)
    assert _names(wide) == ["Near Cafe"]               # Far Cafe is ~2.9 km away
    assert not no_network


def test_elements_carry_the_shape_the_parsers_read(index_file, no_network):
    payload = maps_service.overpass_request(_around('["leisure"="park"]', 1000))
    by_type = {e["type"]: e for e in payload["elements"]}
    assert set(by_type) == {"way", "relation"}
    assert by_type["way"]["id"] == 10 and by_type["relation"]["id"] == 5
    assert set(by_type["way"]["center"]) == {"lat", "lon"}
    node = maps_service.overpass_request(_around('["amenity"="cafe"]', 500))["elements"][0]
    assert node["type"] == "node" and {"lat", "lon", "tags"} <= set(node)

    from core.amenity_map_generator import PropertyAmenityMapGenerator as AmenityMapGenerator
    gen = AmenityMapGenerator()
    amenity = gen._element_to_amenity(by_type["way"], *CENTRE)
    assert amenity["name"] == "Square Garden" and amenity["distance_m"] > 0


def test_every_poi_tool_category_is_answered_locally(index_file, no_network):
    from core.tools.search_nearby_pois import POI_TYPES
    for poi_type, spec in POI_TYPES.items():
        query = _around(spec["query"], 1000, out="out center body;")
        assert osm.parse_overpass_query(query) is not None, poi_type
        assert maps_service.overpass_request(query) is not None, poi_type
    assert not no_network


def test_the_poi_tool_finds_regex_categories_without_the_network(index_file, no_network,
                                                                   monkeypatch):
    from core.tools import search_nearby_pois as tool
    monkeypatch.setattr(tool, "get_from_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(tool, "set_to_cache", lambda *_a, **_k: None)
    gp = tool.query_osm_pois(*CENTRE, "gp", radius=800)
    assert [p["name"] for p in gp] == ["Surgery"]
    chinese = tool.query_osm_pois(*CENTRE, "chinese_restaurant", radius=800)
    assert [p["name"] for p in chinese] == ["Noodles"]
    assert not no_network


def test_the_map_generator_and_the_bbox_query_are_answered_locally(index_file, no_network):
    from core.amenity_map_generator import PropertyAmenityMapGenerator as AmenityMapGenerator
    gen = AmenityMapGenerator()
    assert osm.parse_overpass_query(gen._build_combined_query(*CENTRE)) is not None
    data = maps_service.overpass_request(gen._build_combined_query(*CENTRE), timeout=30,
                                         expect_nonempty=True)
    names = _names(data)
    assert "Tesco Express" in names and "Russell Square" in names
    bbox = ('[out:json];\n(\n  node["amenity"="cafe"](51.520000,-0.125000,51.527000,-0.115000);\n'
            '  way["amenity"="cafe"](51.520000,-0.125000,51.527000,-0.115000);\n);\nout center;')
    assert _names(maps_service.overpass_request(bbox, timeout=25)) == ["Near Cafe"]
    assert not no_network


def test_brand_and_partial_keys(index_file, no_network):
    brand = ('[out:json][timeout:10];\n(\n'
             f'  node["brand"="Tesco"]["shop"="supermarket"](around:800,{CENTRE[0]},{CENTRE[1]});\n'
             f'  way["brand"="Tesco"](around:800,{CENTRE[0]},{CENTRE[1]});\n);\nout center;')
    assert _names(maps_service.overpass_request(brand)) == ["Tesco Express"]
    stops = maps_service.overpass_request(_around('["highway"="bus_stop"]', 300))
    assert _names(stops) == ["Guilford Street"]


# ── what still goes to the network ────────────────────────────────────────────

def test_an_empty_answer_inside_the_extract_is_real_not_an_outage(index_file, no_network):
    payload = maps_service.overpass_request(_around('["amenity"="cinema"]', 300),
                                            expect_nonempty=True)
    assert payload["elements"] == []
    assert payload["generator"] == osm.GENERATOR
    assert not no_network


@pytest.mark.parametrize("lat,lon,radius", [
    (51.5700, -0.1197, 300),     # outside the boundary
    (51.5590, -0.1197, 500),     # inside, but the disc crosses the northern edge
])
def test_a_query_reaching_outside_the_extract_goes_to_the_network(index_file, lat, lon, radius):
    assert osm.answer_overpass_query(_around('["amenity"="cafe"]', radius, lat, lon)) is None


@pytest.mark.parametrize("query", [
    _around('["amenity"="cafe"]', 300, out="out geom;"),
    _around('["amenity"="cafe"]', 300, out="out count;"),
    _around('["highway"="primary"]', 300),            # a value the index does not keep
    _around('["highway"~"bus"]', 300),                # a regex on a partially kept key
    _around('["name"="Near Cafe"]', 300),             # no filter the index can vouch for
    _around('["amenity"!="cafe"]', 300),
    '[out:xml];node["amenity"="cafe"](around:300,51.52,-0.12);out;',
    'node["amenity"="cafe"](around:300,51.52,-0.12);out;',
    '[out:json];area["name"="London"]->.a;node(area.a)["amenity"="cafe"];out;',
])
def test_queries_outside_the_supported_subset_go_to_the_network(index_file, query):
    assert osm.parse_overpass_query(query) is None
    assert osm.answer_overpass_query(query) is None


def test_a_missing_index_falls_back_to_overpass(tmp_path, monkeypatch):
    monkeypatch.setenv("OSM_POI_INDEX_PATH", str(tmp_path / "nope.sqlite3"))
    osm.reset_for_tests()
    sent = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"elements": [{"type": "node", "id": 1, "lat": 51.5, "lon": -0.1,
                                  "tags": {"amenity": "cafe", "name": "From Network"}}]}

    monkeypatch.setattr(maps_service.requests, "post", lambda *a, **k: sent.append(1) or _Resp())
    maps_service.overpass_mirror_state_reset()
    payload = maps_service.overpass_request(_around('["amenity"="cafe"]', 300), timeout=5)
    assert _names(payload) == ["From Network"] and sent
    osm.reset_for_tests()


def test_a_stale_index_is_not_used(index_file, monkeypatch):
    out, _ = index_file
    conn = sqlite3.connect(out)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'source_epoch'",
                 (str(int(time.time() - 60 * 86400)),))
    conn.commit()
    conn.close()
    osm.reset_for_tests()
    assert osm.answer_overpass_query(_around('["amenity"="cafe"]', 300)) is None
    monkeypatch.setenv("OSM_POI_INDEX_MAX_AGE_DAYS", "90")
    osm.reset_for_tests()
    assert osm.answer_overpass_query(_around('["amenity"="cafe"]', 300)) is not None


def test_a_corrupt_index_is_not_used(tmp_path, monkeypatch):
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not a database")
    monkeypatch.setenv("OSM_POI_INDEX_PATH", str(bad))
    osm.reset_for_tests()
    assert osm.answer_overpass_query(_around('["amenity"="cafe"]', 300)) is None
    osm.reset_for_tests()


def test_the_index_is_opened_read_only(index_file):
    osm.answer_overpass_query(_around('["amenity"="cafe"]', 300))
    conn = osm._index()._conn()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM poi")


def test_a_rebuilt_file_is_picked_up_without_a_restart(index_file, tmp_path, monkeypatch):
    out, _ = index_file
    monkeypatch.setattr(osm, "_RECHECK_S", 0.0)
    assert _names(osm.answer_overpass_query(_around('["amenity"="cafe"]', 500))) == ["Near Cafe"]
    seq = tmp_path / "v2.geojsonseq"
    seq.write_text(json.dumps(_point("n1", 51.5240, -0.1195, amenity="cafe",
                                     name="Renamed Cafe")) + "\n", encoding="utf-8")
    poly = tmp_path / "v2.poly"
    poly.write_text(POLY, encoding="utf-8")
    fresh = tmp_path / "v2.sqlite3"
    builder.build(seq, poly, fresh, source_url="t", source_md5="0", region="t",
                  source_timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    os.replace(fresh, out)
    assert _names(osm.answer_overpass_query(_around('["amenity"="cafe"]', 500))) == ["Renamed Cafe"]


def test_coverage_honours_holes():
    coverage = osm.parse_poly(
        "x\n1\n -1 50\n 1 50\n 1 52\n -1 52\n -1 50\nEND\n!2\n -0.1 50.9\n 0.1 50.9\n"
        " 0.1 51.1\n -0.1 51.1\n -0.1 50.9\nEND\nEND\n")
    assert osm.circle_inside_coverage(51.5, 0.5, 100, coverage)
    assert not osm.circle_inside_coverage(51.0, 0.0, 100, coverage)      # inside the hole
    assert not osm.circle_inside_coverage(50.99, 0.0, 5000, coverage)    # disc reaches the hole
