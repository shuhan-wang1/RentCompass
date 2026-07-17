"""Regression test: map-popup HTML must escape attacker-controlled values.

Leaflet renders popup/tooltip content as innerHTML. OSM POI names/cuisine/
opening_hours are publicly editable, and the property address is client-supplied,
so amenity_map_generator must HTML-escape every value it interpolates into popup
HTML. A POI named `<img src=x onerror=alert(1)>` must come out escaped in the
generated map HTML — never as a live tag.

Note on the two sinks:
  * create_map_for_property() writes a STANDALONE document — the escaped popup
    HTML appears verbatim, so we assert the escaped form directly.
  * generate_map_html() returns folium's _repr_html_(), which embeds the whole
    document inside an <iframe srcdoc="...">, HTML-encoding it a SECOND time.
    We html.unescape() once to peel that wrapper back off before asserting.
"""

import html as _html

from core.amenity_map_generator import PropertyAmenityMapGenerator

_XSS = '<img src=x onerror=alert(document.domain)>'
_ESCAPED = '&lt;img src=x onerror=alert(document.domain)&gt;'


def _gen_html(property_data, amenities):
    return PropertyAmenityMapGenerator().generate_map_html(property_data, amenities)


def test_malicious_poi_name_is_escaped():
    raw = _gen_html(
        {"address": "1 Test St", "geo_location": "51.5074, -0.1278"},
        {"cafes": [{"name": _XSS, "lat": 51.5075, "lon": -0.1279, "distance_m": 120}]},
    )
    inner = _html.unescape(raw)  # peel the iframe srcdoc encoding
    # The live tag never appears; the escaped form does.
    assert '<img src=x onerror' not in raw
    assert '<img src=x onerror' not in inner
    assert _ESCAPED in inner


def test_malicious_cuisine_and_address_are_escaped():
    raw = _gen_html(
        {"address": '<script>steal()</script>', "geo_location": "51.5074, -0.1278"},
        {
            "restaurants_chinese": [
                {
                    "name": "OK Diner",
                    "cuisine": '<svg onload=alert(1)>',
                    "lat": 51.5075,
                    "lon": -0.1279,
                    "distance_m": 90,
                }
            ]
        },
    )
    inner = _html.unescape(raw)
    assert '<script>steal()</script>' not in raw
    assert '<svg onload=alert(1)>' not in raw
    assert '<script>steal()</script>' not in inner
    assert '<svg onload=alert(1)>' not in inner
    assert '&lt;script&gt;steal()&lt;/script&gt;' in inner
    assert '&lt;svg onload=alert(1)&gt;' in inner


def test_create_map_for_property_popup_is_escaped(tmp_path):
    out = tmp_path / "map.html"
    ok = PropertyAmenityMapGenerator().create_map_for_property(
        {"address": _XSS, "geo_location": "51.5074, -0.1278"},
        {"parks": [{"name": _XSS, "lat": 51.5075, "lon": -0.1279, "distance_m": 50}]},
        str(out),
    )
    assert ok is True
    html = out.read_text(encoding="utf-8")
    # Standalone document: escaped popup HTML appears verbatim, live tag never.
    assert '<img src=x onerror' not in html
    assert _ESCAPED in html


def test_benign_values_render_readably():
    raw = _gen_html(
        {"address": "221B Baker Street", "geo_location": "51.5074, -0.1278"},
        {"cafes": [{"name": "Nero & Sons", "lat": 51.5075, "lon": -0.1279, "distance_m": 75}]},
    )
    inner = _html.unescape(raw)
    # Benign ampersand escaped (safe) but the text still present and readable.
    assert "Nero &amp; Sons" in inner
    assert "221B Baker Street" in inner
