"""Regression tests for area aliases and result-level location validation."""

import os
import sys


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _path in sys.path:
        sys.path.remove(_path)
    sys.path.insert(0, _path)

from core.geography import (  # noqa: E402
    filter_properties_by_radius,
    known_area_centroid,
)
from core.scraping.on_demand import resolve_location  # noqa: E402
from core.tools.search_properties import _extract_area  # noqa: E402


def test_elephant_and_castle_chinese_alias_is_deterministic():
    alias = "\u8c61\u5821"
    assert _extract_area(f"{alias}\u9644\u8fd1\u7684\u623f\u6e90") == "Elephant and Castle"
    assert resolve_location(alias) == ("elephant-and-castle", "london")
    assert known_area_centroid(alias) == (51.4943, -0.1001)


def test_elephant_and_castle_radius_rejects_reported_bad_listings():
    area = "Elephant and Castle"
    rows = [
        {
            "Address": "New Kent Road, London SE1",
            "geo_location": "51.494302, -0.096841",
            "_search_area": area,
        },
        {
            "Address": "Watford Way, London NW4",
            "geo_location": "51.5890, -0.2260",
            "_search_area": area,
        },
        {
            "Address": "Berberis House, Feltham",
            "geo_location": "51.4496, -0.4089",
            "_search_area": area,
        },
    ]

    kept, rejected = filter_properties_by_radius(
        rows, {area: known_area_centroid(area)}, radius_miles=2.0
    )

    assert [row["Address"] for row in kept] == ["New Kent Road, London SE1"]
    assert {row["Address"] for row in rejected} == {
        "Watford Way, London NW4",
        "Berberis House, Feltham",
    }
    assert all(row["_geo_rejection"] == "outside_radius" for row in rejected)
    assert kept[0]["distance_miles"] < 0.5


def test_unverifiable_listing_fails_closed():
    area = "Elephant and Castle"
    kept, rejected = filter_properties_by_radius(
        [{"Address": "Unknown", "geo_location": "", "_search_area": area}],
        {area: known_area_centroid(area)},
        radius_miles=2.0,
    )
    assert kept == []
    assert rejected[0]["_geo_rejection"] == "listing_unresolved"
