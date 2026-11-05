"""The amenity map tells the user when its base map fails, and keeps its markers readable.

A tester opened "Map" on a listing card (2026-11-05): the base map was an "Access blocked"
tile (fixed separately: tiles now come through this site) and hundreds of overlapping
amenity markers covered the page. Now:
  * a tileerror, or no tile at all within the timeout, shows a notice with a Retry button
    that redraws the tile layer in place, in the UI language;
  * amenity markers sit in one cluster layer per category, and only a few categories
    start switched on; the rest stay in the layer control;
  * the listing's own marker is never clustered and is drawn above everything.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT / "src"), str(_ROOT / "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

os.environ.setdefault("CONVERSATION_DB_PATH", os.path.join(
    tempfile.mkdtemp(prefix="map_markers_"), "conversations.sqlite3"))
os.environ.setdefault("USE_MCP_TOOLS", "0")
os.environ.setdefault("PROPERTY_SOURCE", "csv")

import core.amenity_map_generator as amg  # noqa: E402
from core.amenity_map_generator import PropertyAmenityMapGenerator  # noqa: E402

PROPERTY = {"address": "1 Test St", "geo_location": "51.5235, -0.1197", "price": "£1,800 pcm"}
LAT, LON = 51.5235, -0.1197


def _amenities(gen, per_category=6):
    return {key: [{"name": f"{key} {i}", "lat": LAT + 0.0004 * (i + 1), "lon": LON,
                   "distance_m": 40 * (i + 1)} for i in range(per_category)]
            for key in gen.amenity_config}


def _inner(**kwargs):
    gen = PropertyAmenityMapGenerator()
    amenities = kwargs.pop("amenities", None)
    raw = gen.generate_map_html(PROPERTY, _amenities(gen) if amenities is None else amenities,
                                **kwargs)
    return _html.unescape(raw)        # folium wraps the document in an iframe srcdoc


def _script(inner):
    return inner[inner.rindex("<script>"):inner.rindex("</script>")]


_NOTICE_CALL = re.compile(
    r"\(function \(map, layer, text\) \{(?P<body>.*?)\}\)\("
    r"(?P<map>map_\w+), (?P<layer>tile_layer_\w+), (?P<text>\{.*?\})\);", re.S)


def _notice(inner):
    match = _NOTICE_CALL.search(_script(inner))
    assert match, "the tile-failure notice script is missing"
    return match


# --- base map failure notice ---------------------------------------------------------------

def test_the_notice_listens_to_the_real_tile_layer_and_retries_in_place():
    inner = _inner()
    script = _script(inner)
    match = _notice(inner)
    assert f"var {match['map']} = L.map(" in script
    assert f"var {match['layer']} = L.tileLayer(" in script
    assert "/tiles/osm/{z}/{x}/{y}.png" in script
    body = match["body"]
    for event in ("tileerror", "tileload", "load", "loading"):
        assert f"layer.on('{event}'" in body, event
    # Retry redraws the tile layer; nothing reloads the page or the iframe.
    assert "layer.redraw()" in body
    assert "location.reload" not in script and "location.href" not in script
    # A batch where nothing has loaded within the timeout is reported too.
    assert "setTimeout(" in body and "text.timeout_ms" in body
    assert json.loads(match["text"])["timeout_ms"] == amg.TILE_TIMEOUT_MS


def test_the_notice_runs_before_any_amenity_layer_is_built():
    # The map script is one block: if a later part throws, an earlier notice still works.
    script = _script(_inner())
    notice_at = _NOTICE_CALL.search(script).start()
    assert script.index("L.tileLayer(") < notice_at < script.index("L.markerClusterGroup(")


def test_the_notice_is_visible_and_leaves_the_markers_usable():
    inner = _inner()
    match = _notice(inner)
    text = json.loads(match["text"])
    body = match["body"]
    assert "role', 'alert'" in body
    assert "button.type = 'button'" in body
    assert "L.DomEvent.on(button, 'click', retry)" in body
    assert "disableClickPropagation(box)" in body
    # The notice stack lets clicks through except on the notices themselves.
    stack = re.search(r'<div id="rc-map-notices" style="([^"]*)"', inner)
    assert stack and "pointer-events: none" in stack.group(1)
    assert "pointer-events: auto" in text["box_style"]
    # It is moved inside the map container, so it stays on screen in full-screen mode.
    assert "map.getContainer().appendChild(stack)" in body


@pytest.mark.parametrize("lang,expected", [
    ("en", {"tiles_failed": "The base map could not be loaded. The markers still work.",
            "retry": "Retry"}),
    ("zh", {"tiles_failed": "底图未能加载，标记仍可正常使用。", "retry": "重试"}),
    ("zh-CN", {"retry": "重试"}),
    ("fr", {"retry": "Retry"}),
    (None, {"retry": "Retry"}),
])
def test_the_notice_speaks_the_ui_language(lang, expected):
    kwargs = {} if lang is None else {"lang": lang}
    text = json.loads(_notice(_inner(**kwargs))["text"])
    for key, value in expected.items():
        assert text[key] == value
    assert text["tiles_partly_failed"] and text["tiles_retrying"]


def test_the_amenity_banner_shares_the_notice_stack_and_is_localized():
    en = _inner(amenities={}, amenities_unavailable=True)
    zh = _inner(amenities={}, amenities_unavailable=True, lang="zh")
    stack_en = en[en.index('<div id="rc-map-notices"'):]
    assert stack_en.index("temporarily unavailable") < stack_en.index("</div></div>")
    assert "周边设施数据暂时无法获取" in zh
    assert "temporarily unavailable" not in _inner(amenities={})


# --- marker pile-up ----------------------------------------------------------------------------

def _overlays(script):
    block = re.search(r"overlays :\s*\{(.*?)\},", script, re.S).group(1)
    return {json.loads(label): var
            for label, var in re.findall(r'("[^"]+") : (marker_cluster_\w+),', block)}


def test_amenities_are_clustered_per_category_and_only_a_few_start_on():
    gen = PropertyAmenityMapGenerator()
    script = _script(_inner())
    overlays = _overlays(script)
    names = {cfg["name"]: key for key, cfg in gen.amenity_config.items()}
    assert set(overlays) == set(names), "every category stays in the layer control"
    shown = {names[label] for label, var in overlays.items()
             if re.search(rf"\b{var}\.addTo\(map_\w+\)", script)}
    assert shown == set(amg.DEFAULT_VISIBLE_CATEGORIES)
    assert len(shown) < len(gen.amenity_config) / 2
    # No amenity marker goes straight onto the map: each one joins its cluster.
    direct = re.findall(r"var (marker_\w+) = L\.marker\(\s*\[([^\]]+)\],[^)]*\)\.addTo\(map_\w+\)",
                        script, re.S)
    assert len(direct) == 1, direct
    assert len(re.findall(r"\)\.addTo\(marker_cluster_\w+\)", script)) == 6 * len(names)


def test_clusters_split_at_street_level_and_say_which_category_they_are():
    gen = PropertyAmenityMapGenerator()
    script = _script(_inner())
    assert script.count("L.markerClusterGroup(") == len(gen.amenity_config)
    assert script.count(f'"disableClusteringAtZoom": {amg.CLUSTER_OFF_AT_ZOOM}') == len(gen.amenity_config)
    for cfg in gen.amenity_config.values():
        assert re.fullmatch(r"#[0-9a-f]{6}", cfg["cluster_color"])
        assert f"background-color: {cfg['cluster_color']};" in script


def test_empty_categories_add_no_layer():
    script = _script(_inner(amenities={"supermarkets": [
        {"name": "Tesco", "lat": LAT + 0.001, "lon": LON, "distance_m": 100}]}))
    assert script.count("L.markerClusterGroup(") == 1
    assert set(_overlays(script)) == {"Supermarkets"}


def test_the_listing_marker_stays_unclustered_and_on_top():
    script = _script(_inner(lang="zh"))
    match = re.search(r"var (marker_\w+) = L\.marker\(\s*\[([^\]]+)\],\s*(\{[^}]*\})\s*\)"
                      r"\.addTo\(map_\w+\)", script, re.S)
    assert match, "the listing's marker must be added to the map itself"
    lat, lon = (float(v) for v in match.group(2).split(","))
    assert (lat, lon) == (LAT, LON)
    assert f'"zIndexOffset": {amg.PROPERTY_MARKER_Z_OFFSET}' in match.group(3)
    tooltip = script[script.index(f"{match.group(1)}.bindTooltip("):]
    tooltip = tooltip[:tooltip.index(");")]
    assert "本房源" in tooltip and '"permanent": true' in tooltip
    # Only the listing uses the red marker colour.
    assert script.count('"markerColor": "red"') == 1


def test_cluster_assets_load_the_way_foliums_own_assets_do():
    # The map runs in a sandboxed srcdoc iframe; folium's Leaflet, jQuery and
    # awesome-markers come from absolute https CDN URLs in the document head, and those
    # load there (the markers drew). The cluster plugin must arrive the same way, from a
    # host folium already depends on.
    import folium.folium as ff
    inner = _inner()
    head = inner[:inner.index("</head>")]
    folium_hosts = {urlparse(url).netloc for _, url in ff._default_js + ff._default_css}
    cluster_urls = re.findall(r'(?:src|href)="([^"]*markercluster[^"]*)"', head, re.I)
    assert {u.rsplit("/", 1)[-1] for u in cluster_urls} == {
        "leaflet.markercluster.js", "MarkerCluster.css", "MarkerCluster.Default.css"}
    for url in cluster_urls:
        parsed = urlparse(url)
        assert parsed.scheme == "https" and parsed.netloc in folium_hosts, url


def test_the_standalone_map_uses_the_same_builder(tmp_path):
    out = tmp_path / "map.html"
    gen = PropertyAmenityMapGenerator()
    assert gen.create_map_for_property(PROPERTY, _amenities(gen), str(out), lang="zh") is True
    page = out.read_text(encoding="utf-8")
    assert "L.markerClusterGroup(" in page and "layer.redraw()" in page
    assert "\\u91cd\\u8bd5" in page          # "重试" inside the JSON notice text


# --- the route and the page pass the UI language ----------------------------------------------

@pytest.fixture()
def client():
    import app as appmod
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


@pytest.mark.parametrize("ui_language,expected", [("zh", "重试"), ("en", "Retry"), (None, "Retry")])
def test_the_map_route_passes_the_ui_language(client, monkeypatch, ui_language, expected):
    monkeypatch.setattr(PropertyAmenityMapGenerator, "fetch_all_amenities",
                        lambda self, lat, lon: {"supermarkets": [
                            {"name": "Tesco", "lat": lat + 0.001, "lon": lon, "distance_m": 90}]})
    body = {"address": "1 Test St", "geo_location": "51.5235, -0.1197"}
    if ui_language:
        body["ui_language"] = ui_language
    response = client.post("/api/generate_map", json=body)
    assert response.status_code == 200
    text = json.loads(_notice(_html.unescape(response.get_data(as_text=True)))["text"])
    assert text["retry"] == expected


def test_the_page_sends_its_language_with_the_map_request():
    page = (_ROOT / "app" / "unified-ui.html").read_text(encoding="utf-8")
    body = page[page.index("async function openMap(rec, btn)"):]
    body = body[:body.index("\n        }\n")]
    assert "'/api/generate_map'" in body and "ui_language: currentLang" in body
