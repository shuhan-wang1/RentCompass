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


# folium puts popup and tooltip HTML inside JavaScript template literals (`...`), where
# HTML escaping is no protection: `${...}` is evaluated and a backtick closes the
# literal. An OSM name like the ones below ran as script in the map (2026-11-05), and
# a stray backslash escape (\xZZ) stopped the whole map script with a syntax error.
_TEMPLATE_BREAKERS = ["Cafe ${globalThis.pwned = 1}", "Cafe `+(globalThis.pwned = 2)+`",
                      "Bad \\xZZ escape"]


def _template_literal_safe(text):
    return "${" not in text and "`+(" not in text and "\\xZZ" not in text


def test_template_literal_breakers_in_amenity_fields_are_inert():
    for hostile in _TEMPLATE_BREAKERS:
        raw = _gen_html(
            {"address": "1 Test St", "geo_location": "51.5074, -0.1278"},
            {"supermarkets": [{"name": hostile, "cuisine": hostile, "opening_hours": hostile,
                               "address": hostile, "lat": 51.5075, "lon": -0.1279,
                               "distance_m": 120}]},
        )
        inner = _html.unescape(raw)
        assert _template_literal_safe(inner), hostile
    inner = _html.unescape(_gen_html(
        {"address": "1 Test St", "geo_location": "51.5074, -0.1278"},
        {"cafes": [{"name": _TEMPLATE_BREAKERS[0], "lat": 51.5075, "lon": -0.1279}]}))
    # Still readable: Leaflet renders the entities back to the same characters.
    assert "Cafe &#36;{globalThis.pwned = 1}" in inner


def test_template_literal_breakers_in_the_property_fields_are_inert():
    for hostile in _TEMPLATE_BREAKERS:
        inner = _html.unescape(_gen_html(
            {"address": hostile, "price": hostile, "travel_time": hostile,
             "geo_location": "51.5074, -0.1278"}, {}))
        assert _template_literal_safe(inner), hostile
