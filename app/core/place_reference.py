"""Two location facts that were being left for the model to supply from memory.

1. NEAREST STATION
------------------
In a real session the same property (Tavistock Court, WC1H, Bloomsbury) was reported with
nearest station "Covent Garden" in one turn and "Russell Square" in another. Russell Square
is correct — TfL's StopPoint API puts it 214 m from that point; Covent Garden is ~1.3 km
away.

The string "Covent Garden" appears nowhere in this repo: not in a lookup table, a listing
field, a scraper, a prompt, or a dataset. So it was neither geocoding drift nor a lookup
bug. It was an INVENTION: no tool supplies a nearest station, so nothing constrained the
model, and nothing checked it afterwards (the programmatic critic validates money figures
only). The two turns disagreed because one of them happened to be near a real answer.

The fix is not another prompt line. ``nearest_stations`` makes the data layer supply the
answer, from TfL's own StopPoint index, with the distance attached — so the field exists,
is grounded, and its absence is explicit rather than an invitation to guess.

Note the deliberate difference from ``get_transport_info._resolve_station``: that one sorts
metro-before-rail because it is picking a FARE-CHARGEABLE station, and a farther Tube
station beating a nearer National Rail one is correct for pricing. "Nearest station" is a
different question and is sorted by distance, full stop.

2. WHAT THE DISTANCES ARE MEASURED FROM
---------------------------------------
POI lookups geocode the query string and measure from wherever that lands. Ask "how is
Hackney?" and the answer comes back "Tesco 110m" — 110 m from the geocoded centre of the
London Borough of Hackney, not from any home the user could take. The number is real; the
reference point is not the one a reader assumes. ``reference_point`` names it, and flags
whether the geocoder actually matched a building or only an area, so an answer can say
which.
"""

from __future__ import annotations

import re

# TfL stop types / modes that count as a station a person can walk to and board at.
_STATION_STOP_TYPES = "NaptanMetroStation,NaptanRailStation"
_STATION_MODES = "tube,dlr,elizabeth-line,overground,national-rail,tram"

# Default search radius. Russell Square sits 214 m from Tavistock Court, but an outer-London
# address can easily be a kilometre from anything, so the default is generous; callers get
# the measured distance back and can decide what "near" means.
DEFAULT_STATION_RADIUS_M = 1500

STATION_SOURCE = "TfL StopPoint API"

# Nominatim place_rank: 30 = individual building/house, 26-27 = street, <= 20 = a settlement
# / suburb / borough polygon. The boundary matters because everything at or below it is an
# AREA whose "centre" is a cartographic convenience, not a place anyone lives.
_PRECISE_MIN_PLACE_RANK = 26

_AREA_ADDRESSTYPES = {
    "suburb", "city", "town", "village", "borough", "city_district", "district",
    "county", "state", "region", "neighbourhood", "quarter", "municipality",
    "administrative", "postcode", "postal_code",
}


def nearest_stations(lat: float, lng: float, radius_m: int = DEFAULT_STATION_RADIUS_M,
                     limit: int = 3) -> list[dict] | None:
    """Stations near a point, nearest first, from TfL's StopPoint index.

    Returns a list of ``{name, distance_m, modes, naptan, source}``. An EMPTY list means
    "TfL has no station within the radius" — a real answer. ``None`` means the lookup did
    not run or failed, which is NOT the same thing and callers must not collapse the two:
    the first permits "there is no station within 1.5 km", the second permits only "I could
    not check".
    """
    import requests

    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return None

    from core.maps_service import TFL_APP_KEY

    params = {
        "lat": lat_f, "lon": lng_f, "radius": int(radius_m),
        "stopTypes": _STATION_STOP_TYPES, "modes": _STATION_MODES,
    }
    if TFL_APP_KEY:
        params["app_key"] = TFL_APP_KEY
    try:
        resp = requests.get("https://api.tfl.gov.uk/StopPoint", params=params, timeout=12)
        if resp.status_code != 200:
            return None
        stops = (resp.json() or {}).get("stopPoints") or []
    except Exception as e:  # network/JSON — unknown, not "no stations"
        print(f"  [TfL StopPoint] error: {e}")
        return None

    out = []
    for s in stops:
        dist = s.get("distance")
        name = s.get("commonName")
        if not name or dist is None:
            # A stop with no measured distance cannot be ranked as "nearest"; dropping it
            # is honest, keeping it with distance 0 (the old _resolve_station behaviour)
            # silently promotes it to the front.
            continue
        out.append({
            "name": name,
            "distance_m": int(round(float(dist))),
            "modes": [m for m in (s.get("modes") or [])],
            "naptan": s.get("naptanId") or s.get("id"),
            "source": STATION_SOURCE,
        })
    out.sort(key=lambda s: s["distance_m"])
    return out[:limit]


def nearest_station_for_address(address: str,
                                radius_m: int = DEFAULT_STATION_RADIUS_M) -> dict:
    """``nearest_station`` block for an address, ready to hand to a model.

    Always returns the three keys ``nearest_station`` / ``other_stations_nearby`` /
    ``note``. ``nearest_station`` is ``None`` when the answer is not known, and the note
    then says so in words — an absent station must read as "not established", never as a
    blank the model fills in.
    """
    from core.maps_service import _get_coordinates

    coords = _get_coordinates(address) if address else None
    if not coords:
        return {
            "nearest_station": None,
            "other_stations_nearby": [],
            "note": (f"The nearest station to {address!r} is NOT known: the address could not "
                     f"be geocoded. Do not name a station."),
        }

    found = nearest_stations(coords["lat"], coords["lng"], radius_m=radius_m)
    if found is None:
        return {
            "nearest_station": None,
            "other_stations_nearby": [],
            "note": (f"The nearest station to {address!r} could NOT be checked (TfL StopPoint "
                     f"lookup unavailable). Do not name a station."),
        }
    if not found:
        return {
            "nearest_station": None,
            "other_stations_nearby": [],
            "note": (f"TfL lists no tube/rail station within {radius_m} m of {address!r}. Say "
                     f"there is none nearby rather than naming one."),
        }
    top = found[0]
    return {
        "nearest_station": top,
        "other_stations_nearby": found[1:],
        "note": (f"Nearest station per {STATION_SOURCE}: {top['name']}, {top['distance_m']} m "
                 f"straight-line from the geocoded address. Name only stations from this "
                 f"result."),
    }


_FULL_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2}\b", re.IGNORECASE)
_OUTWARD_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}[0-9][A-Z0-9]?\b", re.IGNORECASE)
_HOUSE_NUMBER_RE = re.compile(r"\b\d+[a-z]?(\s*[-/]\s*\d+[a-z]?)?\s+[A-Za-z]", re.IGNORECASE)


def query_reference(address: str) -> dict:
    """Network-free reading of what a query string points AT, for callers that already
    have coordinates and only need to say what they measured from.

    Deliberately a string classifier and nothing else: it must add zero latency to the POI
    hot path. It answers the one question that changes how a distance should be read — did
    the user name a place someone could live at, or a district? — and errs toward "area",
    because over-claiming precision is the failure being fixed.
    """
    text = (address or "").strip()
    if not text:
        return {"query": address, "precision": "unknown", "is_specific_address": False,
                "distance_basis": "straight_line",
                "measured_from": "an unspecified point; do not quote distances from it."}

    if _FULL_POSTCODE_RE.search(text) or _HOUSE_NUMBER_RE.search(text):
        precision = "address" if _HOUSE_NUMBER_RE.search(text) else "postcode"
        measured_from = (
            f"the geocoded location of {text!r}. Distances are straight-line (as the crow "
            f"flies), not walking distance.")
    elif _OUTWARD_POSTCODE_RE.search(text) and len(text.split()) <= 3:
        precision = "postcode_district"
        measured_from = (
            f"the geocoded centre of the postcode district {text!r} — a district covers roughly "
            f"a square kilometre, so these straight-line distances describe the district, not "
            f"the walk from any particular home.")
    else:
        precision = "area"
        measured_from = (
            f"the geocoded CENTRE of the area {text!r} — not a property address. Distances are "
            f"straight-line from that centre, so they describe the area, not the walk from any "
            f"home the user might take. Say so when quoting them.")

    return {
        "query": text,
        "precision": precision,
        "is_specific_address": precision in ("address", "postcode"),
        "distance_basis": "straight_line",
        "measured_from": measured_from,
    }


def _classify_precision(geo: dict) -> str:
    """'address' | 'street' | 'postcode' | 'area' | 'unknown' for a geocoder hit."""
    if not isinstance(geo, dict):
        return "unknown"
    provider = (geo.get("geocoder") or "").lower()
    if provider.startswith("postcodes.io"):
        # A postcode-unit centroid: a handful of addresses, not a building and not an area.
        return "postcode"
    addresstype = (geo.get("match_type") or "").lower()
    rank = geo.get("place_rank")
    if addresstype in _AREA_ADDRESSTYPES:
        return "area"
    if isinstance(rank, (int, float)):
        if rank >= 29:
            return "address"
        if rank >= _PRECISE_MIN_PLACE_RANK:
            return "street"
        return "area"
    if addresstype in ("building", "house", "residential", "amenity", "shop", "office"):
        return "address"
    if addresstype in ("road", "street", "footway", "pedestrian"):
        return "street"
    return "unknown"


def reference_point(address: str) -> dict:
    """What a set of distances for ``address`` is actually measured FROM.

    Returns ``{query, lat, lng, resolved_name, precision, is_specific_address, geocoder,
    measured_from}``, or ``{query, error}`` when the address does not geocode.

    ``measured_from`` is a sentence an answer can quote verbatim. For an area query it says
    outright that the origin is a cartographic centre and not a home, because "Tesco 110m"
    read against an area centroid means something quite different from "Tesco 110m" read
    against a front door.
    """
    from core.maps_service import _free_geocode

    geo = _free_geocode(address) if address else None
    if not geo:
        return {"query": address, "error": "could not be geocoded",
                "measured_from": (f"{address!r} could not be geocoded, so no distance from it "
                                  f"can be quoted.")}

    precision = _classify_precision(geo)
    resolved = geo.get("resolved_name") or address
    is_specific = precision in ("address", "street", "postcode")

    if precision == "area":
        measured_from = (
            f"the geocoded CENTRE of the area {resolved!r} — not a property address. "
            f"Distances below are straight-line from that centre, so they describe the area, "
            f"not the walk from any particular home. Say which when quoting them.")
    elif precision == "postcode":
        measured_from = (
            f"the centroid of postcode {geo.get('postcode') or resolved!r} — the postcode unit, "
            f"not a specific building. Distances below are straight-line from that centroid.")
    elif precision in ("address", "street"):
        measured_from = (
            f"{resolved!r} (geocoded to {precision} level). Distances below are straight-line, "
            f"not walking distance.")
    else:
        measured_from = (
            f"{resolved!r}, whose precision the geocoder did not report — it may be a building "
            f"or an area centre. Distances below are straight-line; do not imply they are from "
            f"a specific home.")

    return {
        "query": address,
        "lat": geo.get("lat"),
        "lng": geo.get("lng"),
        "resolved_name": resolved,
        "precision": precision,
        "is_specific_address": is_specific,
        "geocoder": geo.get("geocoder"),
        "distance_basis": "straight_line",
        "measured_from": measured_from,
    }
