"""
Tool: Get Transport Info (live TfL Unified API)

Answers real London public-transport questions with LIVE data from the free
TfL Unified API (https://api.tfl.gov.uk, no key required at low volume):

  * journey planning between two places / postcodes / stations
    (duration + route legs + pay-as-you-go single FARE when available),
  * fare lookup between two stations,
  * monthly / weekly Travelcard prices by zone (reuses the shared 2025 fare table),
  * line status ("are there delays on the Victoria line?").

Conventions are shared with the existing London-commute path (maps_service):
  * the same optional free ``TFL_APP_KEY`` (raises rate limits; not required),
  * the same free geocoder (Postcodes.io / OSM Nominatim) via
    ``maps_service.geocode_address``,
  * the same persistent ``cache_service`` (here wrapped with a per-entry TTL:
    15 min for live line status, longer for fares / journeys / geocoding).

London only: for a query whose location geocodes outside Greater London (or has
no nearby TfL station) we return an HONEST "TfL covers London only" result rather
than fabricating a fare.

NOTE (Tool.execute uses a pydantic model with extra='ignore'): every parameter
this tool reads MUST be declared in the ``parameters`` schema below, or it is
silently dropped before the function is called.
"""

import re
import time
import requests

from core.tool_system import Tool
from core import maps_service
from core.maps_service import TFL_APP_KEY
from core.cache_service import get_from_cache, set_to_cache, create_cache_key
# Single source of truth for zone Travelcard prices (real TfL 2025 table).
from core.tools.check_transport_cost import TFL_FARES_2025

TFL_BASE = "https://api.tfl.gov.uk"

# Greater-London bounding box (min_lat, max_lat, min_lng, max_lng). A generous box
# used only as a cheap "is this even London?" pre-filter before hitting TfL.
_LONDON_BBOX = (51.25, 51.72, -0.56, 0.34)

# Modes queried for line status and for nearest-station snapping.
_STATUS_MODES = "tube,overground,elizabeth-line,dlr"
_STATION_MODES = "tube,dlr,elizabeth-line,overground,national-rail"

# Cache TTLs (seconds).
_STATUS_TTL = 15 * 60          # live line status: 15 minutes
_FARE_TTL = 24 * 60 * 60       # fares change rarely: 1 day
_JOURNEY_TTL = 6 * 60 * 60     # journeys/duration: 6 hours
_GEO_TTL = 7 * 24 * 60 * 60    # geocode -> station: 1 week

# TfL line ids (tube + others) we recognise for a single-line status lookup.
_KNOWN_LINE_IDS = {
    'bakerloo', 'central', 'circle', 'district', 'hammersmith-city', 'jubilee',
    'metropolitan', 'northern', 'piccadilly', 'victoria', 'waterloo-city',
    'elizabeth', 'dlr',
    # Overground was split into named lines in 2024:
    'liberty', 'lioness', 'mildmay', 'suffragette', 'weaver', 'windrush',
}
_LINE_ALIASES = {
    'hammersmith and city': 'hammersmith-city', 'hammersmith & city': 'hammersmith-city',
    'waterloo and city': 'waterloo-city', 'waterloo & city': 'waterloo-city',
    'elizabeth line': 'elizabeth', 'the elizabeth line': 'elizabeth', 'crossrail': 'elizabeth',
    'docklands light railway': 'dlr',
}


# ─── low-level HTTP + cache (both monkeypatchable in tests) ──────────────────

def _tfl_get(path: str, params: dict | None = None, timeout: int = 15):
    """GET a TfL endpoint. Returns (status_code, parsed_json_or_None).

    Sends the optional free app_key (shared with maps_service) when configured.
    Never raises on a network/JSON error — returns (status, None) so callers can
    degrade honestly."""
    p = dict(params or {})
    if TFL_APP_KEY:
        p['app_key'] = TFL_APP_KEY
    try:
        resp = requests.get(f"{TFL_BASE}{path}", params=p, timeout=timeout)
        try:
            data = resp.json()
        except ValueError:
            data = None
        return resp.status_code, data
    except requests.exceptions.RequestException as e:
        print(f"  [TfL] request error {path}: {e}")
        return None, None


def _cache_get(key: str, ttl: int):
    """TTL-aware read over the shared (TTL-less) persistent cache."""
    entry = get_from_cache(key)
    if isinstance(entry, dict) and '_ts' in entry and 'v' in entry:
        if time.time() - entry['_ts'] <= ttl:
            return entry['v']
    return None


def _cache_set(key: str, value) -> None:
    set_to_cache(key, {'_ts': time.time(), 'v': value})


def _geocode(location: str):
    """Geocode via the shared free geocoder -> {'lat','lng','postcode'} or None."""
    try:
        return maps_service.geocode_address(location)
    except Exception as e:
        print(f"  [TfL] geocode error: {e}")
        return None


# ─── helpers ─────────────────────────────────────────────────────────────────

def _fmt_gbp(pence) -> str:
    return f"£{pence / 100:.2f}"


def _in_london(lat: float, lng: float) -> bool:
    lo_lat, hi_lat, lo_lng, hi_lng = _LONDON_BBOX
    return lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng


def _pick_disambiguation(disamb: dict):
    """Given a TfL ``*LocationDisambiguation`` block, return the best-matching
    option as (parameter_value, common_name) or None.

    TfL returns candidate stops (with a ``matchQuality``) when an input place is
    ambiguous; we pick the highest-quality one and surface which stop we chose."""
    if not isinstance(disamb, dict):
        return None
    options = disamb.get('disambiguationOptions') or []
    if not options:
        return None
    best = max(options, key=lambda o: o.get('matchQuality', 0))
    name = (best.get('place') or {}).get('commonName') or best.get('parameterValue')
    return best.get('parameterValue'), name


def _resolve_station(location: str) -> dict:
    """Resolve a free-text place/postcode/station to a fare-chargeable TfL station.

    Strategy (reuses the London-commute path's geocoder, then snaps to the nearest
    real station so the journey is fare-chargeable):
      1. geocode the text to coordinates;
      2. if the coordinates are outside Greater London -> {'in_london': False};
      3. otherwise snap to the nearest metro/rail station (Naptan id) -> a token
         TfL prices; note the matched station name in ``name``;
      4. if nothing is nearby, fall back to the raw coordinates (journey still
         works but may carry no fare).

    Returns a dict: {input, in_london, station(bool), token, name, coords}.
    ``token`` is what we hand to /Journey/JourneyResults; ``name`` is the
    human-readable stop we matched (surfaced to the user)."""
    location = (location or "").strip()
    if not location:
        return {"input": location, "in_london": None, "station": False, "token": None, "name": None}

    cache_key = create_cache_key('tfl_resolve_station_v2', location.lower())
    cached = _cache_get(cache_key, _GEO_TTL)
    if cached is not None:
        return cached

    result = {"input": location, "in_london": None, "station": False, "token": None,
              "name": None, "coords": None}
    geo = _geocode(location)
    if not (geo and geo.get('lat') is not None):
        # Scraped listing addresses often end in an outward-only postcode
        # ("Grays Inn Road, London, WC1X") which Nominatim rejects — retry without it.
        stripped = re.sub(r',?\s*[A-Z]{1,2}\d{1,2}[A-Z]?\s*$', '', location).strip(' ,')
        if stripped and stripped.lower() != location.lower():
            geo = _geocode(stripped)
    if geo and geo.get('lat') is not None:
        lat, lng = geo['lat'], geo['lng']
        result['coords'] = {'lat': lat, 'lng': lng}
        if not _in_london(lat, lng):
            result['in_london'] = False
            _cache_set(cache_key, result)
            return result
        result['in_london'] = True
        # Snap to the nearest fare-chargeable station.
        for radius in (1500, 3000):
            status, data = _tfl_get('/StopPoint', {
                'lat': lat, 'lon': lng, 'radius': radius,
                'stopTypes': 'NaptanMetroStation,NaptanRailStation',
                'modes': _STATION_MODES,
            })
            stops = (data or {}).get('stopPoints') if isinstance(data, dict) else None
            if stops:
                # Prefer metro (tube/DLR/Elizabeth) stations over National Rail ones:
                # the Journey Planner quirkily returns bus-only journeys (and bus
                # fares) when the origin token is a NaptanRailStation id.
                stops = sorted(stops, key=lambda s: (
                    0 if s.get('stopType') == 'NaptanMetroStation' else 1,
                    s.get('distance') or 0,
                ))
                top = stops[0]
                result['station'] = True
                result['token'] = top.get('id')
                result['name'] = top.get('commonName')
                _cache_set(cache_key, result)
                return result
        # In London but no nearby station -> use the raw coordinates.
        result['token'] = f"{lat},{lng}"
        result['name'] = geo.get('postcode') or location
        _cache_set(cache_key, result)
        return result

    # Geocoding failed: try a direct station-name search (handles bare names).
    status, data = _tfl_get(f"/StopPoint/Search/{location}", {'modes': _STATION_MODES})
    matches = (data or {}).get('matches') if isinstance(data, dict) else None
    if matches:
        top = matches[0]
        result.update({'in_london': True, 'station': True,
                       'token': top.get('id'), 'name': top.get('name')})
        _cache_set(cache_key, result)
    return result


def _station_fare(from_naptan: str, to_naptan: str) -> dict | None:
    """Adult pay-as-you-go single fares between two stations via TfL's Single Fare
    Finder (/Stoppoint/{from}/FareTo/{to}). Used when the journey response carries
    no fare block (TfL omits fares on future-dated journey plans).

    Returns {'peak_gbp','off_peak_gbp'} (either may be missing) or None."""
    if not from_naptan or not to_naptan or ',' in from_naptan or ',' in to_naptan:
        return None  # coordinates are not fare-addressable
    cache_key = create_cache_key('tfl_station_fare', from_naptan, to_naptan)
    cached = _cache_get(cache_key, _FARE_TTL)
    if cached is not None:
        return cached or None
    status, data = _tfl_get(f"/Stoppoint/{from_naptan}/FareTo/{to_naptan}")
    fares = {}
    if status == 200 and isinstance(data, list):
        for section in data:
            for row in (section.get('rows') or []):
                for ticket in (row.get('ticketsAvailable') or []):
                    if (ticket.get('passengerType') or '').lower() != 'adult':
                        continue
                    ttype = ((ticket.get('ticketType') or {}).get('type') or '').lower()
                    if 'pay as you go' not in ttype:
                        continue
                    time_type = ((ticket.get('ticketTime') or {}).get('type') or '').lower()
                    try:
                        cost = float(ticket.get('cost'))
                    except (TypeError, ValueError):
                        continue
                    if 'off' in time_type:
                        fares.setdefault('off_peak_gbp', cost)
                    else:  # 'peak' or 'anytime'
                        fares.setdefault('peak_gbp', cost)
    _cache_set(cache_key, fares)
    return fares or None


def _journey_time_params():
    """Journey-planning time: 'now' during normal service hours, else the next
    morning 09:00 (late at night the planner returns night-bus-only itineraries
    with bus fares, which would misrepresent "how much is the tube from X to Y").

    Returns (query_params, planned_for_note); both empty for 'now'."""
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Europe/London"))
    except Exception:
        return {}, None
    if 6 <= now.hour <= 21:
        return {}, None
    nxt = now if now.hour < 6 else now + timedelta(days=1)
    return ({"date": nxt.strftime("%Y%m%d"), "time": "0900", "timeIs": "Departing"},
            nxt.strftime("%Y-%m-%d") + " 09:00 (typical daytime service)")


def _extract_journey(resp_json: dict):
    """From a JourneyResults response pick (fastest_journey, fare_journey).

    ``fastest_journey`` drives duration/legs; ``fare_journey`` is the cheapest
    journey that actually carries a fare block (TfL only prices some journeys)."""
    journeys = [j for j in (resp_json or {}).get('journeys', []) if j.get('duration') is not None]
    if not journeys:
        return None, None
    fastest = min(journeys, key=lambda j: j['duration'])
    fared = [j for j in journeys if (j.get('fare') or {}).get('totalCost') is not None]
    fare_journey = min(fared, key=lambda j: j['fare']['totalCost']) if fared else None
    return fastest, fare_journey


def _summarise_legs(journey: dict):
    """Delegate to the maps_service leg summariser (shared route formatting)."""
    try:
        return maps_service._summarise_tfl_legs(journey)
    except Exception:
        return [], ""


def _parse_line_status(lines_json, line_filter: str | None):
    """Turn a /Line/.../Status response into a compact, groundable summary.

    Returns (lines_list, summary, any_disruption)."""
    out = []
    for L in lines_json or []:
        name = L.get('name')
        statuses = L.get('lineStatuses') or [{}]
        st = statuses[0]
        desc = st.get('statusSeverityDescription') or 'Unknown'
        severity = st.get('statusSeverity')
        reason = st.get('reason')
        disrupted = (desc.strip().lower() != 'good service')
        out.append({
            'name': name,
            'status': desc,
            'severity': severity,
            'reason': reason,
            'has_disruption': disrupted,
        })
    if line_filter:
        fl = line_filter.replace('-', ' ').lower()
        filtered = [l for l in out if l['name'] and fl in l['name'].lower()]
        if filtered:
            out = filtered
    out.sort(key=lambda l: (not l['has_disruption'], l['name'] or ''))
    any_disruption = any(l['has_disruption'] for l in out)
    if len(out) == 1:
        summary = f"{out[0]['name']} line: {out[0]['status']}"
    elif any_disruption:
        bad = [f"{l['name']} ({l['status']})" for l in out if l['has_disruption']]
        summary = "Disruptions: " + "; ".join(bad)
    else:
        summary = "Good Service on all lines"
    return out, summary, any_disruption


def _resolve_line_id(line: str | None):
    """Normalise a free-text line name to a TfL line id, or None for an all-line
    query (unknown names, 'overground', empty)."""
    s = (line or "").strip().lower()
    if not s:
        return None
    s = re.sub(r'\bline\b', '', s).strip()
    if s in _LINE_ALIASES:
        return _LINE_ALIASES[s]
    sid = s.replace(' & ', '-').replace(' and ', '-').replace(' ', '-')
    if sid in _KNOWN_LINE_IDS:
        return sid
    return None


# ─── capability implementations ──────────────────────────────────────────────

def _do_line_status(line: str | None) -> dict:
    line_id = _resolve_line_id(line)
    cache_key = create_cache_key('tfl_line_status', line_id or _STATUS_MODES)
    cached = _cache_get(cache_key, _STATUS_TTL)
    if cached is None:
        if line_id:
            status, data = _tfl_get(f"/Line/{line_id}/Status")
        else:
            status, data = _tfl_get(f"/Line/Mode/{_STATUS_MODES}/Status")
        if not isinstance(data, list):
            return {"success": False, "error": "Could not reach TfL line-status service. "
                    "Please try again shortly or check tfl.gov.uk/status."}
        cached = data
        _cache_set(cache_key, cached)

    # If the user named a line but it wasn't a known id, we fetched all lines;
    # filter by name so "are there delays on the victoria line" still narrows.
    name_filter = None if line_id else (line or None)
    lines, summary, any_disruption = _parse_line_status(cached, name_filter)
    return {
        "success": True,
        "query_type": "line_status",
        "coverage": "london",
        "line_filter": line or None,
        "lines": lines,
        "summary": summary,
        "any_disruption": any_disruption,
        "source": "TfL Unified API (live line status)",
    }


def _do_journey(from_location: str, to_location: str, want_fare: bool) -> dict:
    if not from_location or not to_location:
        # Single endpoint: we can at least answer the non-London honesty case.
        solo = from_location or to_location
        if solo:
            r = _resolve_station(solo)
            if r.get('in_london') is False:
                return _outside_london(solo)
        return {"success": False,
                "error": "I need both a start and a destination (a station, postcode or place) "
                         "to plan a London journey or work out the fare."}

    frm = _resolve_station(from_location)
    to = _resolve_station(to_location)

    if frm.get('in_london') is False:
        return _outside_london(from_location)
    if to.get('in_london') is False:
        return _outside_london(to_location)
    if not frm.get('token') or not to.get('token'):
        return {"success": False,
                "error": f"Couldn't locate {'the start' if not frm.get('token') else 'the destination'} "
                         "on the TfL network. Try a nearby station name or a postcode."}

    time_params, planned_for = _journey_time_params()
    cache_key = create_cache_key('tfl_journey', frm['token'], to['token'], planned_for or 'now')
    resp = _cache_get(cache_key, _JOURNEY_TTL if not want_fare else _FARE_TTL)
    if resp is None:
        status, data = _tfl_get(f"/Journey/JourneyResults/{frm['token']}/to/{to['token']}",
                                time_params, timeout=20)
        if status == 300 and isinstance(data, dict):
            # Ambiguous endpoint(s): pick the best candidate stop and retry once.
            f_tok, f_name = (_pick_disambiguation(data.get('fromLocationDisambiguation')) or (frm['token'], frm['name']))
            t_tok, t_name = (_pick_disambiguation(data.get('toLocationDisambiguation')) or (to['token'], to['name']))
            frm['token'], frm['name'] = f_tok, f_name or frm['name']
            to['token'], to['name'] = t_tok, t_name or to['name']
            status, data = _tfl_get(f"/Journey/JourneyResults/{frm['token']}/to/{to['token']}",
                                    time_params, timeout=20)
        if status == 200 and isinstance(data, dict) and data.get('journeys'):
            resp = data
            _cache_set(cache_key, resp)
        else:
            # No TfL route -> honest "London only".
            return _outside_london(f"{from_location} -> {to_location}")

    fastest, fare_journey = _extract_journey(resp)
    if fastest is None:
        return _outside_london(f"{from_location} -> {to_location}")

    legs, route_summary = _summarise_legs(fastest)
    duration = int(fastest['duration'])

    out = {
        "success": True,
        "query_type": "fare" if want_fare else "journey",
        "coverage": "london",
        "from": {"input": from_location, "resolved_station": frm.get('name'), "naptan": frm.get('token')},
        "to": {"input": to_location, "resolved_station": to.get('name'), "naptan": to.get('token')},
        "duration_minutes": duration,
        "route_summary": route_summary,
        "route_legs": legs,
        "source": "TfL Unified API (live Journey Planner)",
    }
    if planned_for:
        out["planned_for"] = planned_for
    # Note which stops we matched (satisfies the disambiguation "note in output").
    if frm.get('name') or to.get('name'):
        out["stations_used"] = f"{frm.get('name') or from_location} -> {to.get('name') or to_location}"

    fare_block = (fare_journey or fastest).get('fare') or {}
    total = fare_block.get('totalCost')
    if total is not None:
        out["fare_available"] = True
        out["fare_pence"] = int(total)
        out["fare_gbp"] = round(int(total) / 100, 2)      # numeric (critic-groundable)
        out["fare_display"] = _fmt_gbp(int(total))         # £-formatted
        out["fare_note"] = ("Adult pay-as-you-go single (contactless / Oyster). "
                            "For unlimited travel see a weekly/monthly Travelcard.")
        return out

    # No fare on the journey plan (TfL omits fares on future-dated plans): ask the
    # Single Fare Finder for the station-to-station pay-as-you-go fare instead.
    station_fare = _station_fare(frm.get('token'), to.get('token'))
    if station_fare and ('peak_gbp' in station_fare or 'off_peak_gbp' in station_fare):
        main = station_fare.get('peak_gbp', station_fare.get('off_peak_gbp'))
        out["fare_available"] = True
        out["fare_gbp"] = round(main, 2)                   # numeric (critic-groundable)
        out["fare_pence"] = int(round(main * 100))
        out["fare_display"] = f"£{main:.2f}"               # £-formatted
        if 'off_peak_gbp' in station_fare and 'peak_gbp' in station_fare:
            off = station_fare['off_peak_gbp']
            out["fare_off_peak_gbp"] = round(off, 2)
            out["fare_off_peak_display"] = f"£{off:.2f}"
            out["fare_note"] = (f"Adult pay-as-you-go single: £{main:.2f} peak / "
                                f"£{off:.2f} off-peak (contactless / Oyster, TfL Single Fare Finder).")
        else:
            out["fare_note"] = ("Adult pay-as-you-go single (contactless / Oyster, "
                                "TfL Single Fare Finder).")
    else:
        out["fare_available"] = False
        out["fare_note"] = ("TfL did not return a single fare for this exact routing "
                            "(often when it uses National Rail or coordinates rather than "
                            "Tube/DLR stations). See tfl.gov.uk/fares for a precise fare.")
    return out


def _do_travelcard(end_zone: int | None, from_location: str, to_location: str,
                   travel_type: str) -> dict:
    """Weekly/monthly Travelcard prices for Zone 1-N (reuses the shared 2025 table)."""
    zone = end_zone
    if not zone:
        # Try to infer the furthest zone from a named destination/origin.
        try:
            from core.tools.calculate_commute_cost import _get_zone_from_address
            zones = [z for z in (_get_zone_from_address(to_location or ""),
                                 _get_zone_from_address(from_location or "")) if z]
            zone = max(zones) if zones else None
        except Exception:
            zone = None
    if not zone:
        return {"success": False,
                "error": "Which London fare zone is the destination in (e.g. Zone 2), or which "
                         "station/area? Travelcards are priced Zone 1 to Zone N."}
    zone = max(2, min(6, int(zone)))
    user_type = "student" if "student" in (travel_type or "").lower() else "adult"
    prices = TFL_FARES_2025.get(user_type, {}).get(f"zone1-{zone}")
    if not prices:
        return {"success": False, "error": f"No Travelcard data for Zone 1-{zone}; see tfl.gov.uk/fares."}
    return {
        "success": True,
        "query_type": "travelcard",
        "coverage": "london",
        "zones": f"Zone 1-{zone}",
        "user_type": "18+ Student Oyster" if user_type == "student" else "Adult",
        "monthly_gbp": prices['monthly'],
        "monthly_display": f"£{prices['monthly']:.2f}",
        "weekly_gbp": prices['weekly'],
        "weekly_display": f"£{prices['weekly']:.2f}",
        "daily_cap_gbp": prices['daily_cap'],
        "daily_cap_display": f"£{prices['daily_cap']:.2f}",
        "note": "Student (30% off) applies to weekly/monthly Travelcards, NOT pay-as-you-go.",
        "source": "TfL 2025 fares",
    }


def _outside_london(place: str) -> dict:
    return {
        "success": True,
        "coverage": "outside_london",
        "query": place,
        "message": (f"TfL (Transport for London) only covers London, so I can't pull live "
                    f"fares, journeys or line status for '{place}'. For public transport there, "
                    f"check the local operator (e.g. Transport for Greater Manchester at tfgm.com, "
                    f"or National Rail at nationalrail.co.uk)."),
        "source": "TfL Unified API (coverage check)",
    }


# ─── auto query-type inference ───────────────────────────────────────────────

_STATUS_KWS = ['delay', 'delays', 'disruption', 'disrupted', 'suspended', 'part closure',
               'closure', 'line status', 'service status', 'good service', 'running ok',
               'running normally', 'is the ', 'any problems', 'severe', 'minor delays',
               '晚点', '延误', '故障', '停运']
_TRAVELCARD_KWS = ['travelcard', 'travel card', 'season ticket', 'monthly pass', 'monthly travel',
                   'weekly pass', 'oyster monthly', 'month pass', '月票', '周票']


def _infer_query_type(user_query: str, from_location: str, to_location: str, line: str) -> str:
    ql = (user_query or "").lower()
    if line or any(k in ql for k in _STATUS_KWS):
        # A status keyword OR an explicit line and no A->B endpoints -> status.
        if line or not (from_location and to_location):
            return "line_status"
    if any(k in ql for k in _TRAVELCARD_KWS):
        return "travelcard"
    if from_location or to_location:
        # Fare-flavoured wording emphasises the fare; otherwise it's a journey.
        if any(k in ql for k in ['fare', 'cost', 'how much', 'price', 'expensive', '多少钱', '票价']):
            return "fare"
        return "journey"
    if any(k in ql for k in _STATUS_KWS):
        return "line_status"
    return "journey"


# ─── tool entry point ────────────────────────────────────────────────────────

def get_transport_info_impl(
    query_type: str = "auto",
    from_location: str = "",
    to_location: str = "",
    line: str = "",
    end_zone: int = None,
    travel_type: str = "adult",
    user_query: str = "",
) -> dict:
    """Live TfL transport info: journey/fare, Travelcard prices, or line status."""
    from_location = (from_location or "").strip()
    to_location = (to_location or "").strip()
    line = (line or "").strip()

    qt = (query_type or "auto").strip().lower()
    if qt not in ("journey", "fare", "line_status", "travelcard"):
        qt = _infer_query_type(user_query, from_location, to_location, line)

    print(f"   🚇 [TfL] get_transport_info: type={qt} from={from_location!r} "
          f"to={to_location!r} line={line!r}")

    try:
        if qt == "line_status":
            return _do_line_status(line)
        if qt == "travelcard":
            return _do_travelcard(end_zone, from_location, to_location, travel_type)
        return _do_journey(from_location, to_location, want_fare=(qt == "fare"))
    except Exception as e:
        print(f"   ❌ [TfL] get_transport_info error: {e}")
        return {"success": False, "error": f"Transport lookup failed: {e}. See tfl.gov.uk."}


get_transport_info_tool = Tool(
    name="get_transport_info",
    description="""
Get LIVE London transport information from the official TfL Unified API.

**USE THIS TOOL FOR (real-time TfL data):**
- Journey planning: "how do I get from the flat to UCL", "how long from Kings Cross to Canary Wharf"
- Single fares: "how much is the tube from X to Y", "what's the fare from Brixton to Bank"
- Monthly / weekly Travelcard prices by zone: "what would a monthly travelcard cost"
- Line status / delays: "are there delays on the Victoria line", "is the Central line running"

Returns real durations, real pay-as-you-go fares (£), Travelcard prices and live line status.
London only: for non-London places it returns an honest "TfL covers London only" note.

**Parameters:**
- query_type: journey | fare | line_status | travelcard | auto (default auto — inferred from the question)
- from_location: start (station name, postcode, place or address) — for journey/fare
- to_location: destination (station name, postcode, place or address) — for journey/fare
- line: a tube/rail line name for a status check (e.g. "Victoria", "Central"); empty = all lines
- end_zone: furthest London zone for a Travelcard (e.g. 2..6)
- travel_type: "adult" or "student" (Travelcard discount)
- user_query: the original user question (helps auto-infer the query type)
""",
    func=get_transport_info_impl,
    parameters={
        "type": "object",
        "properties": {
            "query_type": {
                "type": "string",
                "enum": ["auto", "journey", "fare", "line_status", "travelcard"],
                "description": "What to look up. 'auto' infers it from user_query.",
                "default": "auto",
            },
            "from_location": {
                "type": "string",
                "description": "Journey start: station, postcode, place or address.",
                "default": "",
            },
            "to_location": {
                "type": "string",
                "description": "Journey destination: station, postcode, place or address.",
                "default": "",
            },
            "line": {
                "type": "string",
                "description": "Tube/rail line name for a status check (e.g. 'Victoria'). Empty = all lines.",
                "default": "",
            },
            "end_zone": {
                "type": "integer",
                "description": "Furthest London fare zone for a Travelcard (2-6).",
            },
            "travel_type": {
                "type": "string",
                "enum": ["adult", "student"],
                "description": "Passenger type for Travelcard pricing.",
                "default": "adult",
            },
            "user_query": {
                "type": "string",
                "description": "Original user question (used to auto-infer query_type).",
                "default": "",
            },
        },
        "required": [],
    },
    max_retries=2,
)
