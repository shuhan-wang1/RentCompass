"""Deterministic geographic validation for property search results.

Search providers occasionally return sponsored or loosely related listings well
outside the requested area. The search layer must therefore treat provider
location scoping as candidate generation, not as proof that a listing is nearby.
"""

from __future__ import annotations

import math
import re
from typing import Iterable, Mapping


EARTH_RADIUS_MILES = 3958.7613

# Network-free anchors for common/high-risk searches. Unknown areas are geocoded
# by the caller before filtering.
_AREA_CENTROIDS = {
    "\u8c61\u5821": (51.4943, -0.1001),
    "\u5927\u8c61\u57ce\u5821": (51.4943, -0.1001),
    "elephant and castle": (51.4943, -0.1001),
    "elephant & castle": (51.4943, -0.1001),
    "elephant-and-castle": (51.4943, -0.1001),
    "camden": (51.5390, -0.1426),
    "islington": (51.5380, -0.1027),
    "bloomsbury": (51.5215, -0.1255),
    "london": (51.5074, -0.1278),
    "manchester": (53.4808, -2.2426),
}


def _normalise_area(value: object) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"\s+", " ", text)


def known_area_centroid(area: object) -> tuple[float, float] | None:
    """Return a curated area centroid, if one is available."""
    return _AREA_CENTROIDS.get(_normalise_area(area))


def parse_coordinates(value: object) -> tuple[float, float] | None:
    """Parse a supported coordinate payload and reject impossible values."""
    lat = lon = None
    try:
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",")]
            if len(parts) == 2:
                lat, lon = float(parts[0]), float(parts[1])
        elif isinstance(value, Mapping):
            lat = float(value.get("lat"))
            raw_lon = value.get("lng")
            if raw_lon is None:
                raw_lon = value.get("lon")
            lon = float(raw_lon)
        elif isinstance(value, (tuple, list)) and len(value) == 2:
            lat, lon = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None

    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def haversine_miles(origin: object, destination: object) -> float | None:
    """Great-circle distance in miles, or None for invalid coordinates."""
    start = parse_coordinates(origin)
    end = parse_coordinates(destination)
    if start is None or end is None:
        return None

    lat1, lon1 = map(math.radians, start)
    lat2, lon2 = map(math.radians, end)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + (
        math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(a)))


def filter_properties_by_radius(
    properties: Iterable[dict],
    area_centres: Mapping[str, object],
    radius_miles: float,
) -> tuple[list[dict], list[dict]]:
    """Keep only listings whose coordinates prove they are within the radius.

    Each listing is matched to the centroid for its _search_area tag. Missing or
    invalid listing/area coordinates fail closed: an unverifiable listing must
    never be represented to the user as being near the requested place.
    """
    try:
        radius = float(radius_miles)
    except (TypeError, ValueError):
        radius = 2.0
    if not math.isfinite(radius) or radius <= 0:
        radius = 2.0

    centres = {
        _normalise_area(area): parse_coordinates(coords)
        for area, coords in (area_centres or {}).items()
    }
    kept: list[dict] = []
    rejected: list[dict] = []

    for original in properties or []:
        row = dict(original)
        area_key = _normalise_area(row.get("_search_area"))
        centre = centres.get(area_key)
        listing_geo = row.get("geo_location") or row.get("Geo_Location")
        distance = haversine_miles(centre, listing_geo)

        if centre is None:
            row["_geo_rejection"] = "area_unresolved"
            rejected.append(row)
        elif distance is None:
            row["_geo_rejection"] = "listing_unresolved"
            rejected.append(row)
        elif distance > radius:
            row["_geo_rejection"] = "outside_radius"
            row["distance_miles"] = round(distance, 2)
            rejected.append(row)
        else:
            row["distance_miles"] = round(distance, 2)
            kept.append(row)

    return kept, rejected
