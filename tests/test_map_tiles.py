"""Map tiles reach the sandboxed map iframe through this site.

The amenity map is shown in a sandboxed srcdoc iframe without allow-same-origin. Its document
has an opaque origin, so the browser sends no Referer with tile requests, and OSM's tile
servers answer those with an "Access blocked" image: the markers drew, the map did not
(2026-11-04). The generated map now asks this site for tiles (/tiles/osm/z/x/y.png); the edge
or app.py's fallback route fetches them with the site's identity.
"""
from __future__ import annotations

import html as _html
import os
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT / "src"), str(_ROOT / "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

os.environ.setdefault("CONVERSATION_DB_PATH", os.path.join(
    tempfile.mkdtemp(prefix="map_tiles_"), "conversations.sqlite3"))
os.environ.setdefault("USE_MCP_TOOLS", "0")
os.environ.setdefault("PROPERTY_SOURCE", "csv")

from core.amenity_map_generator import PropertyAmenityMapGenerator  # noqa: E402


def test_generated_map_asks_this_site_for_tiles():
    raw = PropertyAmenityMapGenerator().generate_map_html(
        {"address": "1 Test St", "geo_location": "51.5235, -0.1197"}, {})
    inner = _html.unescape(raw)          # folium wraps the document in an iframe srcdoc
    assert "/tiles/osm/{z}/{x}/{y}.png" in inner
    assert "tile.openstreetmap.org/{z}" not in inner
    assert "OpenStreetMap" in inner      # attribution stays on the map


@pytest.fixture()
def client(monkeypatch):
    import app as appmod
    appmod._osm_tile_cache.clear()
    appmod.app.config["TESTING"] = True
    return appmod, appmod.app.test_client()


class _Resp:
    def __init__(self, status=200, ctype="image/png", body=b"\x89PNG-tile"):
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.content = body


def test_tile_route_fetches_with_the_site_identity_and_caches(client, monkeypatch):
    appmod, c = client
    calls = []
    import requests

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, dict(headers or {})))
        return _Resp()

    monkeypatch.setattr(requests, "get", fake_get)
    first = c.get("/tiles/osm/15/16372/10895.png", headers={"Cookie": "session=secret"})
    second = c.get("/tiles/osm/15/16372/10895.png")
    assert first.status_code == 200 and second.status_code == 200
    assert first.mimetype == "image/png" and first.data == b"\x89PNG-tile"
    assert "max-age" in first.headers.get("Cache-Control", "")
    assert len(calls) == 1, "the second request must come from the cache"
    url, headers = calls[0]
    assert url.endswith("/15/16372/10895.png")
    assert headers.get("User-Agent", "").startswith("RentCompass/")
    assert headers.get("Referer", "").startswith("http")
    assert "Cookie" not in headers, "the visitor's cookies must never go upstream"


@pytest.mark.parametrize("path", [
    "/tiles/osm/20/0/0.png",          # beyond max zoom
    "/tiles/osm/3/8/0.png",           # x outside the 2**z grid
    "/tiles/osm/3/0/8.png",           # y outside the grid
    "/tiles/osm/3/-1/0.png",          # negative
    "/tiles/osm/3/0/0.jpg",           # other extension
    "/tiles/osm/../etc/passwd",
])
def test_tile_route_refuses_anything_outside_the_grid(client, monkeypatch, path):
    appmod, c = client
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("no upstream call expected"))
    assert c.get(path).status_code == 404


@pytest.mark.parametrize("resp", [_Resp(status=503), _Resp(ctype="text/html")])
def test_tile_route_reports_an_upstream_failure_as_502(client, monkeypatch, resp):
    appmod, c = client
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: resp)
    assert c.get("/tiles/osm/1/0/0.png").status_code == 502
    assert (1, 0, 0) not in appmod._osm_tile_cache


def test_tile_route_survives_a_network_error(client, monkeypatch):
    appmod, c = client
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "get", boom)
    assert c.get("/tiles/osm/1/0/0.png").status_code == 502
