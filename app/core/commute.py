"""Shared commute-time estimation.

Single source of truth for "how many minutes from A to B", used by both the
property search tool (step 4 annotation/filter) and the area recommender.

Two estimation paths, mirroring the original in-tool logic:
  * Inside Greater London we trust the TfL Journey Planner
    (maps_service.calculate_travel_time), falling back to a coordinate estimate
    when TfL returns nothing usable.
  * Outside London (Manchester, Leeds, ...) TfL has no route and street-address
    geocoding is unreliable, so we estimate straight from exact lat/lon and only
    fall back to the address-based figure if we have no coordinates.

Coordinate dicts use the {"lat": .., "lng": ..} shape returned by
maps_service.geocode_address.
"""

from __future__ import annotations

import math
import re

# Any address-based commute estimate above this (minutes) is treated as a
# geocoding glitch and replaced by the coordinate-based estimate.
COMMUTE_SANITY_CAP = 240

# Greater-London bounding box — inside it we trust TfL Journey Planner; outside
# it TfL has no route and street-address geocoding is unreliable, so we estimate
# from exact lat/lon instead. (lat_min, lat_max, lng_min, lng_max)
LONDON_BBOX = (51.28, 51.70, -0.55, 0.30)


def parse_geo(geo) -> tuple[float, float] | None:
    """'53.4415, -2.2159' -> (53.4415, -2.2159); tolerant of blanks/junk.

    Also accepts a (lat, lng) tuple/list or a {"lat":.., "lng":..} dict.
    """
    if geo is None:
        return None
    if isinstance(geo, dict):
        la, lo = geo.get("lat"), geo.get("lng")
        if la is None or lo is None:
            return None
        try:
            lat, lng = float(la), float(lo)
        except (TypeError, ValueError):
            return None
        return (lat, lng) if (-90 <= lat <= 90 and -180 <= lng <= 180) else None
    if isinstance(geo, (tuple, list)) and len(geo) >= 2:
        try:
            lat, lng = float(geo[0]), float(geo[1])
        except (TypeError, ValueError):
            return None
        return (lat, lng) if (-90 <= lat <= 90 and -180 <= lng <= 180) else None
    m = re.findall(r"-?\d+\.?\d*", str(geo))
    if len(m) < 2:
        return None
    try:
        lat, lng = float(m[0]), float(m[1])
    except ValueError:
        return None
    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return (lat, lng)
    return None


def in_london(coords: dict | tuple | list | None) -> bool:
    """True when the coordinate falls inside the Greater-London bounding box."""
    p = parse_geo(coords)
    if not p:
        return False
    la, lo = p
    return LONDON_BBOX[0] <= la <= LONDON_BBOX[1] and LONDON_BBOX[2] <= lo <= LONDON_BBOX[3]


def coord_commute_minutes(origin_geo, dest_coords: dict | None) -> int | None:
    """Distance-based transit estimate (minutes) from an origin's exact
    coordinates to the destination. Mirrors maps_service.estimate_travel_time_simple
    (1.3x route factor, 20 km/h transit, short wait) but uses exact lat/lon
    directly, so it never depends on flaky street-address geocoding."""
    o = parse_geo(origin_geo)
    d = parse_geo(dest_coords)
    if not o or not d:
        return None
    R = 6371.0
    dlat = math.radians(d[0] - o[0])
    dlng = math.radians(d[1] - o[1])
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(o[0])) * math.cos(math.radians(d[0])) * math.sin(dlng / 2) ** 2)
    dist_km = R * 2 * math.asin(math.sqrt(a))
    actual = dist_km * 1.3
    return int((actual / 20.0) * 60 + min(10, dist_km * 2))


def commute_minutes(
    dest_target: str,
    dest_coords: dict | None,
    *,
    origin_address: str | None = None,
    origin_geo=None,
    london: bool | None = None,
) -> int | None:
    """Best-effort commute minutes from an origin to ``dest_target``.

    Reproduces the property-search step-4 logic so both callers agree:

    * ``london`` (whether the destination is inside London) is computed from
      ``dest_coords`` when not passed explicitly.
    * London destinations: try TfL via ``calculate_travel_time(origin_address,
      dest_target)``; if that is missing/absurd, fall back to the coordinate
      estimate from ``origin_geo``.
    * Non-London destinations: use the coordinate estimate; if we have no
      coordinates, fall back to the address-based figure.

    Returns integer minutes, or ``None`` when nothing usable can be produced.
    Never raises.
    """
    # Import lazily so importing this module never drags in the maps stack.
    from core.maps_service import calculate_travel_time

    if london is None:
        london = in_london(dest_coords)

    travel_time = None
    if london:
        tfl = None
        if origin_address:
            try:
                tfl = calculate_travel_time(origin_address, dest_target)
            except Exception:
                tfl = None
        if isinstance(tfl, (int, float)) and 0 < tfl <= COMMUTE_SANITY_CAP:
            travel_time = tfl
        else:
            travel_time = coord_commute_minutes(origin_geo, dest_coords)
    else:
        travel_time = coord_commute_minutes(origin_geo, dest_coords)
        if travel_time is None and origin_address:
            try:
                tt = calculate_travel_time(origin_address, dest_target)
                if isinstance(tt, (int, float)) and 0 < tt <= COMMUTE_SANITY_CAP:
                    travel_time = tt
            except Exception:
                travel_time = None

    if travel_time is None:
        return None
    try:
        return int(travel_time)
    except (TypeError, ValueError):
        return None
