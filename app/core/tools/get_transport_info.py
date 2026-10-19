"""
Tool: Get Transport Info (live TfL Unified API)

Answers real London public-transport questions with LIVE data from the free
TfL Unified API (https://api.tfl.gov.uk, no key required at low volume):

  * journey planning between two places / postcodes / stations
    (duration + route legs + pay-as-you-go single FARE when available),
  * fare lookup between two stations,
  * monthly / weekly Travelcard prices by zone (shared official 2026 table),
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

import hashlib
import json
import math
import re
import requests
from datetime import datetime, timedelta, date as calendar_date
from zoneinfo import ZoneInfo

from core.tool_system import Tool
from core import maps_service
from core.maps_service import TFL_APP_KEY
from core.cache_service import get_from_cache, set_to_cache, create_cache_key
from uk_rent_agent.data.tfl_fares import get_zonal_fare

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
_LIVE_CACHE_VERSION = "tfl-live-api-v1"

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
        print(f"  [TfL] request error; path_chars={len(str(path))} error_type={type(e).__name__}")
        return None, None


def _cache_get(key: str, ttl: int):
    """TTL-aware read using the shared cache-envelope contract."""
    try:
        return get_from_cache(
            key,
            ttl_seconds=ttl,
            version=_LIVE_CACHE_VERSION,
        )
    except TypeError:
        # Compatibility with old one-argument cache fakes and legacy wrapped rows.
        entry = get_from_cache(key)
        if isinstance(entry, dict) and '_ts' in entry and 'v' in entry:
            return entry['v']
        return entry


def _cache_set(key: str, value) -> None:
    try:
        set_to_cache(
            key,
            value,
            version=_LIVE_CACHE_VERSION,
            provenance={"provider": "TfL Unified API"},
        )
    except TypeError:
        set_to_cache(key, value)


def _geocode(location: str):
    """Geocode via the shared free geocoder -> {'lat','lng','postcode'} or None."""
    try:
        return maps_service.geocode_address(location)
    except Exception as e:
        print(f"  [TfL] geocode error; location_chars={len(str(location))} error_type={type(e).__name__}")
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
    """Without a requested day/time, let TfL plan for now.

    A late-night request must not silently become a next-morning journey.
    """
    return {}, None


def _london_now():
    return datetime.now(ZoneInfo("Europe/London"))


_MONTHS = {name.lower(): month for month in range(1, 13)
           for name in (calendar_date(2000, month, 1).strftime('%B'),
                        calendar_date(2000, month, 1).strftime('%b'))}
_MONTH_PATTERN = '|'.join(sorted(_MONTHS, key=len, reverse=True))


def _requested_journey_time(date=None, time=None, time_is=None, user_query=""):
    """Resolve only declared calendar/time information in Europe/London.

    Explicit fields win over free text. Relative days and omitted years use the
    London clock; no date is invented for a clock time without a declared day.
    Invalid dates and ambiguous/nonexistent DST wall times fail before HTTP.
    """
    now = _london_now()
    query = (user_query or '').lower()
    resolved_date, resolved_time = None, None
    if date is not None:
        if not isinstance(date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
            raise ValueError('date must be a real ISO date (YYYY-MM-DD).')
        resolved_date = calendar_date.fromisoformat(date)
    else:
        iso = re.search(r'(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)', query)
        day_month = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(' + _MONTH_PATTERN +
                              r')\b(?:\s+(\d{4}))?', query)
        month_day = re.search(r'\b(' + _MONTH_PATTERN + r')\s+(\d{1,2})(?:st|nd|rd|th)?'
                              r'\b(?:,?\s+(\d{4}))?', query)
        if iso:
            resolved_date = calendar_date.fromisoformat(iso.group(1))
        elif day_month or month_day:
            match = day_month or month_day
            day = int(match.group(1 if day_month else 2))
            month = _MONTHS[match.group(2 if day_month else 1)]
            resolved_date = calendar_date(int(match.group(3) or now.year), month, day)
        declared_day = iso or day_month or month_day
        if declared_day and resolved_date:
            weekday = re.search(r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*,?\s*$',
                                query[:declared_day.start()])
            if weekday and weekday.group(1) != resolved_date.strftime('%A').lower():
                raise ValueError('The stated weekday does not match the calendar date.')
        relative = (now.date() + timedelta(days=1) if re.search(r'\btomorrow\b|明天|明早', query)
                    else now.date() if re.search(r'\btoday\b|今天', query) else None)
        if relative is not None:
            if resolved_date is not None and resolved_date != relative:
                raise ValueError('The stated calendar date and relative day disagree in Europe/London.')
            resolved_date = relative

    if time is not None:
        if not isinstance(time, str) or not re.fullmatch(r'\d{2}:\d{2}', time):
            raise ValueError('time must be a valid 24-hour clock time (HH:MM).')
        resolved_time = datetime.strptime(time, '%H:%M').time()
    else:
        clock = re.search(r'(?<!\d)(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)?(?!\d)', query)
        if not clock:
            clock = re.search(r'(?<!\d)(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)(?![a-z])', query)
            hour, minute, meridiem = ((int(clock.group(1)), 0, clock.group(2))
                                     if clock else (None, None, None))
        else:
            hour, minute, meridiem = int(clock.group(1)), int(clock.group(2)), clock.group(3)
        if hour is not None:
            if meridiem:
                if not 1 <= hour <= 12:
                    raise ValueError('The stated 12-hour clock time is invalid.')
                hour = hour % 12 + (12 if meridiem.startswith('p') else 0)
            resolved_time = datetime.strptime(f'{hour:02d}:{minute:02d}', '%H:%M').time()

    if time_is is not None and time_is not in ('departure', 'arrival'):
        raise ValueError('time_is must be departure or arrival.')
    mode = time_is
    if mode is None and resolved_time:
        mode = ('arrival' if re.search(r'\barriv(?:e|es|ing|al)\b|到达|抵达', query)
                else 'departure')
    if resolved_time and resolved_date is None:
        raise ValueError('A journey clock time needs a date or an explicit today/tomorrow.')
    if time_is is not None and resolved_time is None:
        raise ValueError('time_is requires a journey time.')
    if not resolved_date and not resolved_time:
        return None

    params, evidence = {}, {'timezone': 'Europe/London', 'time_basis': 'user_requested'}
    if resolved_date:
        params['date'] = resolved_date.strftime('%Y%m%d')
        evidence['date'] = resolved_date.isoformat()
    if resolved_time:
        wall = datetime.combine(resolved_date, resolved_time)
        zone = ZoneInfo('Europe/London')
        valid = {aware.utcoffset() for fold in (0, 1)
                 if (aware := wall.replace(tzinfo=zone, fold=fold)).astimezone(ZoneInfo('UTC'))
                 .astimezone(zone).replace(tzinfo=None) == wall}
        if len(valid) != 1:
            raise ValueError('The London time is ambiguous or nonexistent at the daylight-saving change.')
        params.update(time=resolved_time.strftime('%H%M'),
                      timeIs='Arriving' if mode == 'arrival' else 'Departing')
        evidence.update(time=resolved_time.strftime('%H:%M'), time_is=mode,
                        requested_at=wall.replace(tzinfo=zone).isoformat())
    return params, evidence


def _extract_journey(resp_json: dict):
    """From a JourneyResults response pick (fastest_journey, fare_journey).

    The two records remain distinct: a cheapest journey's fare must never be
    attached to the fastest journey's duration or legs."""
    journeys = [j for j in (resp_json or {}).get('journeys', []) if j.get('duration') is not None]
    if not journeys:
        return None, None
    fastest = min(journeys, key=lambda j: j['duration'])
    fared = [j for j in journeys if _journey_fare_pence(j) is not None]
    fare_journey = min(fared, key=_journey_fare_pence) if fared else None
    return fastest, fare_journey


def _journey_fare_pence(journey: dict):
    value = (journey.get('fare') or {}).get('totalCost')
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if math.isfinite(number) and number >= 0 and number.is_integer() else None


def _journey_evidence(journey: dict) -> dict:
    """One indivisible itinerary: time, legs and price share the same identity."""
    legs, summary = _summarise_legs(journey)
    identity = hashlib.sha256(json.dumps(
        journey, sort_keys=True, separators=(',', ':'), default=str,
    ).encode()).hexdigest()[:16]
    total = _journey_fare_pence(journey)
    out = {
        'itinerary_id': f'tfl:{identity}',
        'duration_minutes': int(journey['duration']),
        'changes_count': maps_service._tfl_changes_count(journey),
        'route_summary': summary,
        'route_legs': legs,
        'source': 'TfL Unified API Journey Planner',
        'fare_available': total is not None,
        'fare_basis': 'journey_itinerary' if total is not None else None,
    }
    if total is not None:
        out.update(fare_pence=total, fare_gbp=round(total / 100, 2),
                   fare_display=_fmt_gbp(total), fare_itinerary_id=out['itinerary_id'])
    return out


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


def _do_journey(from_location: str, to_location: str, want_fare: bool,
                requested_time=None) -> dict:
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

    if requested_time is None:
        time_params, planned_for = _journey_time_params()
        time_evidence = None
    else:
        time_params, time_evidence = requested_time
        planned_for = time_evidence.get('requested_at') or time_evidence.get('date')
    cache_key = create_cache_key('tfl_journey', frm['token'], to['token'],
                                 json.dumps(time_params, sort_keys=True))
    resp = _cache_get(cache_key, _JOURNEY_TTL if not want_fare else _FARE_TTL)
    from_cache = resp is not None
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
            return {'success': False, 'coverage': 'london',
                    'error': 'TfL could not verify this journey for the requested time.',
                    'service_status': 'unavailable' if status != 200 else 'no_route',
                    'retryable': status != 200, 'journey_time': time_evidence,
                    'source': 'TfL Unified API Journey Planner'}

    fastest, fare_journey = _extract_journey(resp)
    if fastest is None:
        return {'success': False, 'coverage': 'london', 'service_status': 'invalid_response',
                'error': 'TfL returned no usable journey duration.', 'retryable': True}

    selected = _journey_evidence(fastest)
    cheapest = _journey_evidence(fare_journey) if fare_journey is not None else None

    out = {
        "success": True,
        "query_type": "fare" if want_fare else "journey",
        "coverage": "london",
        "from": {"input": from_location, "resolved_station": frm.get('name'), "naptan": frm.get('token')},
        "to": {"input": to_location, "resolved_station": to.get('name'), "naptan": to.get('token')},
        "duration_minutes": selected['duration_minutes'],
        "changes_count": selected['changes_count'],
        "route_summary": selected['route_summary'],
        "route_legs": selected['route_legs'],
        "selected_itinerary_id": selected['itinerary_id'],
        "fastest_journey": selected,
        "cheapest_journey": cheapest,
        "source": "TfL Unified API Journey Planner",
        "from_cache": from_cache,
        "cache_status": "fresh" if from_cache else "miss",
    }
    if planned_for:
        out["planned_for"] = planned_for
    if time_evidence:
        out['journey_time'] = time_evidence
    # Note which stops we matched (satisfies the disambiguation "note in output").
    if frm.get('name') or to.get('name'):
        out["stations_used"] = f"{frm.get('name') or from_location} -> {to.get('name') or to_location}"

    total = _journey_fare_pence(fastest)
    if total is not None:
        out["fare_available"] = True
        out["fare_pence"] = int(total)
        out["fare_gbp"] = round(int(total) / 100, 2)      # numeric (critic-groundable)
        out["fare_display"] = _fmt_gbp(int(total))         # £-formatted
        out["fare_basis"] = "journey_itinerary"
        out["fare_itinerary_id"] = selected['itinerary_id']
        out["fare_matches_itinerary"] = True
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
        out["fare_basis"] = "station_pair"
        out["fare_itinerary_id"] = None
        out["fare_matches_itinerary"] = False
        out["station_fare"] = {
            **station_fare,
            "from_naptan": frm.get('token'), "to_naptan": to.get('token'),
            "source": "TfL Single Fare Finder", "fare_basis": "station_pair",
            "note": "Station-pair single fares; not verified for the selected journey's route or timing.",
        }
        if 'off_peak_gbp' in station_fare and 'peak_gbp' in station_fare:
            off = station_fare['off_peak_gbp']
            out["fare_off_peak_gbp"] = round(off, 2)
            out["fare_off_peak_display"] = f"£{off:.2f}"
            out["fare_note"] = (f"Adult pay-as-you-go single: £{main:.2f} peak / "
                                f"£{off:.2f} off-peak (contactless / Oyster, TfL Single Fare Finder).")
        else:
            out["fare_note"] = ("Adult pay-as-you-go single (contactless / Oyster, "
                                "TfL Single Fare Finder).")
        out["fare_note"] += (" This is a station-pair fare, not a fare confirmed for "
                             "the selected itinerary; do not combine it with that "
                             "itinerary's duration as a verified time/price offer.")
    else:
        out["fare_available"] = False
        out["fare_basis"] = None
        out["fare_itinerary_id"] = None
        out["fare_matches_itinerary"] = False
        out["fare_note"] = ("TfL did not return a single fare for this exact routing "
                            "(often when it uses National Rail or coordinates rather than "
                            "Tube/DLR stations). See tfl.gov.uk/fares for a precise fare.")
    return out


def _do_travelcard(end_zone: int | None, from_location: str, to_location: str,
                   travel_type: str) -> dict:
    """Weekly/monthly Travelcard prices for Zone 1-N from the shared fare table."""
    zone = end_zone
    endpoint_zones = {}
    zone_basis = 'explicit_zone_table' if zone else 'postcode_district_estimate'
    if not zone:
        # Try to infer the furthest zone from a named destination/origin.
        try:
            from core.tools.calculate_commute_cost import _get_zone_from_address
            for name, location in (('from', from_location), ('to', to_location)):
                if location:
                    endpoint_zones[name] = _get_zone_from_address(location)
            unknown = [name for name, value in endpoint_zones.items() if value is None]
            if unknown:
                return {
                    'success': False, 'query_type': 'travelcard',
                    'coverage_verified': False, 'zone_basis': 'not_established',
                    'endpoint_zones': endpoint_zones,
                    'unknown_zone_locations': [from_location if name == 'from' else to_location
                                               for name in unknown],
                    'error': ('Unable to establish zones for every supplied endpoint. '
                              'No Travelcard is claimed to cover this journey; obtain the '
                              'unknown station fare/extension or request a hypothetical zone table.'),
                }
            zone = max(endpoint_zones.values()) if endpoint_zones else None
        except Exception:
            zone = None
    if not zone:
        return {"success": False,
                "error": "Which London fare zone is the destination in (e.g. Zone 2), or which "
                         "station/area? Travelcards are priced Zone 1 to Zone N."}
    zone = int(zone)
    if not 1 <= zone <= 6:
        return {'success': False, 'query_type': 'travelcard', 'coverage_verified': False,
                'error': 'The available Travelcard table covers Zones 1-6 only; no extension price is known.'}
    zone = max(2, zone)
    user_type = "student" if "student" in (travel_type or "").lower() else "adult"
    try:
        prices = get_zonal_fare(1, zone, user_type)
    except (TypeError, ValueError):
        return {"success": False, "error": f"No Travelcard data for Zone 1-{zone}; see tfl.gov.uk/fares."}
    return {
        "success": True,
        "query_type": "travelcard",
        "coverage": "london",
        'coverage_scope': 'zonal_price_table_only',
        'coverage_verified': False,
        'zone_basis': zone_basis,
        'endpoint_zones': endpoint_zones,
        'coverage_note': ('This is a price for the specified or approximately inferred zones, '
                          'not verified coverage of the named stations or full journey. '
                          'Out-of-zone stations require separately verified extensions/fares.'),
        "zones": f"Zone 1-{zone}",
        "user_type": "18+ Student Oyster" if user_type == "student" else "Adult",
        "monthly_gbp": prices['monthly'],
        "monthly_display": f"£{prices['monthly']:.2f}",
        "weekly_gbp": prices['weekly'],
        "weekly_display": f"£{prices['weekly']:.2f}",
        "daily_cap_gbp": prices['daily_cap'],
        "daily_cap_display": f"£{prices['daily_cap']:.2f}",
        "note": "Student (30% off) applies to weekly/monthly Travelcards, NOT pay-as-you-go.",
        "source": prices["source"],
        "source_url": prices["source_url"],
        "effective_date": prices["effective_date"],
        "fare_edition": prices["edition"],
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
    date: str = None,
    time: str = None,
    time_is: str = None,
) -> dict:
    """Live TfL transport info: journey/fare, Travelcard prices, or line status."""
    from_location = (from_location or "").strip()
    to_location = (to_location or "").strip()
    line = (line or "").strip()

    qt = (query_type or "auto").strip().lower()
    if qt not in ("journey", "fare", "line_status", "travelcard"):
        qt = _infer_query_type(user_query, from_location, to_location, line)

    print(
        f"   🚇 [TfL] get_transport_info: type={qt} "
        f"from_chars={len(from_location)} to_chars={len(to_location)} line_chars={len(line)}"
    )

    try:
        if qt == "line_status":
            return _do_line_status(line)
        if qt == "travelcard":
            return _do_travelcard(end_zone, from_location, to_location, travel_type)
        requested_time = _requested_journey_time(date, time, time_is, user_query)
        return _do_journey(from_location, to_location, want_fare=(qt == "fare"),
                           requested_time=requested_time)
    except ValueError as exc:
        return {'success': False, 'error': str(exc), 'error_code': 'invalid_journey_time',
                'retryable': False, 'timezone': 'Europe/London'}
    except Exception as e:
        print(f"   ❌ [TfL] get_transport_info error_type={type(e).__name__}")
        return {"success": False, "error": "Transport lookup failed. See tfl.gov.uk."}


get_transport_info_tool = Tool(
    name="get_transport_info",
    description="""Get LIVE London transport info from the official TfL Unified API: journey planning, single fares, weekly/monthly Travelcard prices by zone, and line status/delays. Returns real durations, pay-as-you-go fares (£), Travelcard prices and live line status. London only — for non-London places it returns an honest "TfL covers London only" note.
查询伦敦公共交通实时信息（行程/票价/Travelcard/线路状态，数据来源 TfL）。""",
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
                "minimum": 2,
                "maximum": 6,
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
            "date": {
                "type": "string", "format": "date",
                "description": "Requested journey date, YYYY-MM-DD, in Europe/London. Omit unless stated by the user.",
            },
            "time": {
                "type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$",
                "description": "Requested London local journey time, HH:MM (24-hour); requires a date.",
            },
            "time_is": {
                "type": "string", "enum": ["departure", "arrival"],
                "description": "Whether the requested time is departure or arrival (arrive by).",
            },
        },
        "required": [],
    },
    max_retries=2,
)
