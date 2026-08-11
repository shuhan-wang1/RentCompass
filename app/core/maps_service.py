# maps_service.py - free providers (no Google Maps required):
#   geocoding   -> OSM Nominatim + Postcodes.io (no key)
#   travel time -> TfL Journey Planner (London public transport), haversine fallback
#   POIs        -> OpenStreetMap Overpass (no key)

import os
import re
import time
import requests
from datetime import datetime
import pandas as pd
from collections import Counter
import asyncio
import math
from typing import Optional
from .cache_service import get_from_cache, set_to_cache, create_cache_key

# Freshness contracts for facts that can change independently of the code.
GEOCODE_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
CRIME_CACHE_TTL_SECONDS = 24 * 60 * 60
_GEOCODE_CACHE_VERSION = "geocode-v3"
_CRIME_CACHE_VERSION = "crime-radius-v2"


def _read_versioned_cache(
    cache_key: str,
    *,
    ttl_seconds: int,
    version: str,
) -> tuple[str, object]:
    """Return (status, value), retaining old one-argument test doubles."""
    try:
        entry = get_from_cache(
            cache_key,
            ttl_seconds=ttl_seconds,
            version=version,
            with_status=True,
        )
        if hasattr(entry, "status"):
            return entry.status, entry.value
        return ("fresh" if entry is not None else "miss"), entry
    except TypeError:
        value = get_from_cache(cache_key)
        return ("fresh" if value is not None else "miss"), value


def _write_versioned_cache(
    cache_key: str,
    value,
    *,
    ttl_seconds: int,
    version: str,
    provenance: dict,
) -> None:
    """Write a cache envelope, with compatibility for two-argument fakes."""
    try:
        set_to_cache(
            cache_key,
            value,
            ttl_seconds=ttl_seconds,
            version=version,
            provenance=provenance,
        )
    except TypeError:
        set_to_cache(cache_key, value)


def _label_stale(value: object, warning: str) -> object:
    """Copy a stale mapping and make its degraded freshness impossible to miss."""
    if not isinstance(value, dict):
        return value
    labelled = dict(value)
    labelled.update({
        "cache_status": "stale",
        "possibly_outdated": True,
        "warning": warning,
    })
    return labelled


# Optional free TfL app key (register at api-portal.tfl.gov.uk) to raise rate limits;
# the Journey Planner also works without a key at low volume.
TFL_APP_KEY = os.getenv("TFL_APP_KEY", "")
# Nominatim / OSM require a descriptive User-Agent. overpass-api.de returns HTTP
# 406 for the default python-requests UA, so this header MUST be sent on every
# Overpass request too (see overpass_request below).
_OSM_HEADERS = {"User-Agent": "uk-rent-recommendation/1.0 (student-housing demo)"}
_UK_POSTCODE_RE = re.compile(r"([A-Z]{1,2}[0-9]{1,2}[A-Z]?[ ]?[0-9][A-Z]{2})", re.IGNORECASE)

# Public Overpass mirrors, tried in order. The first is the reference server;
# the rest are independent/alternate front-ends we fall back to when it is busy
# (504) or rate-limits us (429). This is the single Overpass HTTP entry point for
# the whole app (maps_service, amenity_map_generator, tools/search_nearby_pois).
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
]


class OverpassError(RuntimeError):
    """Raised when every Overpass mirror fails.

    Callers that show maps must catch this and degrade honestly (visible banner)
    rather than silently presenting a marker-only / empty result.
    """


class OverpassEmpty(OverpassError):
    """``expect_nonempty=True`` and every mirror we could reach answered a CLEAN empty 200
    (HTTP 200, no remark, no elements).

    A subclass, so existing ``except OverpassError`` handlers degrade exactly as before. It
    exists so a caller that can distinguish the two cases does not have to pay for a second
    walk of the mirror pool to find out which it was: ``.payload`` carries the empty response.
    "Nothing of this type is nearby" and "every mirror is rate-limiting us" are different
    answers, and the first one is legitimate — no gym within 300 m is a fact.
    """

    def __init__(self, message: str, payload: dict):
        super().__init__(message)
        self.payload = payload


# ─── Mirror health + request pacing ──────────────────────────────────────────
# Measured 2026-07-29, from the production host: overpass-api.de, lz4 and z all refused the
# connection within ~1s (DNS resolved fine), kumi.systems read-timed-out at 30s, and osm.ch
# answered HTTP 200 with elements=0 even for cafés within 1 km of Leicester Square. The public
# mirrors had started rate-limiting this IP after a day of POI traffic.
#
# Two costs made that outage far worse than it had to be. Every query re-walked the whole
# mirror pool from the top, so each POI type paid the same connection refusals and the same
# 30s read timeout again — one turn spent 82s and completed nothing. And nothing paced the
# requests, so the traffic that earned the rate-limit kept flowing at full speed.
#
# So: remember which mirrors just failed and skip them for a cooldown, put the last mirror
# that actually worked first, and never issue two Overpass requests closer together than
# OVERPASS_MIN_INTERVAL_S. Process-local and best-effort — it reduces load, it is not a quota.
OVERPASS_MIRROR_COOLDOWN_S = float(os.getenv("OVERPASS_MIRROR_COOLDOWN_S", "300"))
OVERPASS_MIN_INTERVAL_S = float(os.getenv("OVERPASS_MIN_INTERVAL_S", "1.0"))

import threading as _threading  # noqa: E402  (kept next to the state it guards)

_mirror_lock = _threading.Lock()
_mirror_penalty: dict = {}        # url -> monotonic instant it may be tried again
_mirror_preferred: list = []      # most-recently-successful first
_pace_lock = _threading.Lock()
_last_request_at = [0.0]          # list so the closure can rebind the value


def overpass_mirror_state_reset() -> None:
    """Forget every penalty and preference (tests; ops)."""
    with _mirror_lock:
        _mirror_penalty.clear()
        _mirror_preferred.clear()
    with _pace_lock:
        _last_request_at[0] = 0.0


def _mirrors_to_try() -> list:
    """Mirrors in the order worth trying: last-good first, then untried, then those whose
    cooldown has expired. A mirror still inside its cooldown is skipped entirely — unless
    that would leave nothing to try, in which case the whole (penalised) pool is returned,
    because refusing to ask at all is worse than asking a mirror that failed five minutes
    ago."""
    now = time.monotonic()
    with _mirror_lock:
        penalty = dict(_mirror_penalty)
        preferred = [u for u in _mirror_preferred if u in OVERPASS_MIRRORS]
    ordered = preferred + [u for u in OVERPASS_MIRRORS if u not in preferred]
    live = [u for u in ordered if penalty.get(u, 0.0) <= now]
    return live or ordered


def _penalise_mirror(url: str, reason: str) -> None:
    with _mirror_lock:
        _mirror_penalty[url] = time.monotonic() + OVERPASS_MIRROR_COOLDOWN_S
        if url in _mirror_preferred:
            _mirror_preferred.remove(url)
    print(f"  [Overpass] mirror sidelined {OVERPASS_MIRROR_COOLDOWN_S:.0f}s: "
          f"{url.split('/')[2]} ({reason[:60]})")


def _reward_mirror(url: str) -> None:
    with _mirror_lock:
        _mirror_penalty.pop(url, None)
        if url in _mirror_preferred:
            _mirror_preferred.remove(url)
        _mirror_preferred.insert(0, url)


def _pace_request() -> None:
    """Block until at least OVERPASS_MIN_INTERVAL_S has passed since the last request."""
    if OVERPASS_MIN_INTERVAL_S <= 0:
        return
    with _pace_lock:
        wait = _last_request_at[0] + OVERPASS_MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at[0] = time.monotonic()


def overpass_request(query: str, timeout: int = 30, max_rounds: int = 2,
                     expect_nonempty: bool = False,
                     deadline: Optional[float] = None) -> dict:
    """POST an Overpass QL query and return the parsed JSON.

    Sends the descriptive ``_OSM_HEADERS`` User-Agent on every request (the
    reference server rejects the default python-requests UA with HTTP 406) and
    rotates through ``OVERPASS_MIRRORS`` on any non-200 / invalid-body / network
    error / server-side ``remark``, with exponential backoff between full rounds.
    Raises ``OverpassError`` only after every mirror has failed across all rounds.

    ``expect_nonempty`` controls how a HTTP 200 with an EMPTY ``elements`` list
    (and no remark) is classified. Overpass mirrors under load (observed on
    overpass.osm.ch and the reference server) sometimes answer such an empty 200
    instead of a 429/504. For a single-type query an empty list is a legitimate
    "none nearby", so the default (``False``) returns it. For a batched
    multi-selector query near a populated address zero is implausible, so the
    caller passes ``expect_nonempty=True`` and we treat the empty body as an
    outage of THAT mirror and rotate to the next one -- finding a healthy mirror
    that still has the data, instead of caching a silently-empty result. If every
    reachable mirror answers a clean empty 200, ``OverpassEmpty`` is raised with
    that payload attached, so the caller can tell "genuinely nothing there" from
    "the pool is down" WITHOUT walking the pool a second time.

    ``deadline`` is a ``time.monotonic()`` instant. No further mirror is tried and no
    backoff is slept once it passes; the walk raises instead. Without it, one query could
    outlive the budget that authorised it by minutes: five mirrors x two rounds, with a
    30s read timeout on any of them, is the shape that turned a rate-limited pool into an
    85-second tool call.
    """
    last_err = None
    clean_empty = None            # first clean empty 200 seen, if expect_nonempty

    def _expired() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    for round_idx in range(max_rounds):
        if _expired():
            break
        for url in _mirrors_to_try():
            if _expired():
                last_err = f"{last_err or 'no mirror answered'} (deadline reached)"
                break
            try:
                _pace_request()
                # Each ATTEMPT is clamped to what is left, not just the walk. A 30s read
                # timeout on two slow mirrors otherwise overshoots the deadline by minutes
                # even though no request was ISSUED late — measured at 29s against a 17s
                # budget before this clamp.
                attempt_timeout = timeout
                if deadline is not None:
                    attempt_timeout = max(1.0, min(float(timeout),
                                                   deadline - time.monotonic()))
                resp = requests.post(
                    url, data={"data": query}, headers=_OSM_HEADERS,
                    timeout=attempt_timeout
                )
                if resp.status_code != 200:
                    last_err = f"{url} -> HTTP {resp.status_code}"
                    _penalise_mirror(url, f"HTTP {resp.status_code}")
                    continue
                try:
                    payload = resp.json()
                except ValueError as e:
                    # 200 with an HTML/text error body (Overpass sometimes does this)
                    last_err = f"{url} -> invalid JSON body: {e}"
                    _penalise_mirror(url, "invalid JSON body")
                    continue
                # HTTP 200 does NOT guarantee a usable result. When a mirror
                # runtime-times-out, runs out of memory, or rate-limits us
                # mid-query it still answers 200 but embeds a top-level "remark"
                # (e.g. 'runtime error: Query timed out ...') and returns an empty
                # or partial "elements" list. A complete, healthy response carries
                # no remark, so treat any remark as a retryable failure and rotate
                # to the next mirror -- otherwise a busy-server body gets cached
                # and silently degrades every future map for that cell.
                remark = payload.get("remark") if isinstance(payload, dict) else None
                if remark:
                    last_err = f"{url} -> Overpass remark: {str(remark).strip()[:200]}"
                    _penalise_mirror(url, "remark")
                    continue
                # Empty-but-no-remark 200: only a failure when the caller expected
                # results (see docstring). Rotate to give a healthy mirror a chance.
                if expect_nonempty and not (payload.get("elements")
                                            if isinstance(payload, dict) else None):
                    last_err = f"{url} -> HTTP 200 but empty elements (expected results)"
                    if clean_empty is None:
                        clean_empty = payload
                    _penalise_mirror(url, "empty 200 where results were expected")
                    continue
                _reward_mirror(url)
                return payload
            except requests.exceptions.RequestException as e:
                last_err = f"{url} -> {e}"
                _penalise_mirror(url, type(e).__name__)
                continue
        # Exponential backoff before retrying the whole mirror pool.
        if round_idx < max_rounds - 1 and not _expired():
            time.sleep(1.0 * (2 ** round_idx))
    if clean_empty is not None:
        raise OverpassEmpty(
            f"Every reachable Overpass mirror answered an empty 200: {last_err}", clean_empty)
    raise OverpassError(f"All Overpass mirrors failed: {last_err}")

# Map common landmarks to specific addresses that Google Maps API can route to
LANDMARK_TO_ADDRESS = {
    'university college london': 'Gower Street, London WC1E 6BT',
    'ucl': 'Gower Street, London WC1E 6BT',
    'kings cross': 'Kings Cross Station, London N1 9AP',
    'kings cross station': 'Kings Cross Station, London N1 9AP',
    'euston': 'Euston Station, London NW1 2RT',
    'euston station': 'Euston Station, London NW1 2RT',
    'london bridge': 'London Bridge Station, London SE1 9SP',
}

def _normalize_address_for_routing(address: str) -> str:
    """Convert landmark names to specific addresses for routing"""
    if not address:
        return address
    
    address_lower = address.lower().strip()
    
    # Check if it's a known landmark
    for landmark, specific_address in LANDMARK_TO_ADDRESS.items():
        if landmark in address_lower:
            print(f"  -> Converted '{address}' to '{specific_address}'")
            return specific_address
    
    return address

def _free_geocode(address: str) -> dict | None:
    """Free geocoder: Postcodes.io for UK postcodes, else OSM Nominatim.

    Returns ``{'lat','lng','postcode'(optional),'geocoder','resolved_name','match_type',
    'place_rank'}`` or None. Cached.

    The last four are PRECISION metadata and are not decoration: "Hackney" and
    "20 Liverpool Road N1 0RW" both come back as a lat/lng pair, and every distance measured
    from the first one is a distance from a borough centroid. Callers that show distances
    need to be able to say which they got — see ``core.place_reference.reference_point``.
    """
    if not address or not isinstance(address, str):
        return None
    address = _normalize_address_for_routing(address)
    # v2: entries cached under the old key carry no precision metadata, and silently
    # serving those would make reference_point() report "unknown" forever.
    cache_key = create_cache_key('_free_geocode_v3', address)
    cache_status, cached = _read_versioned_cache(
        cache_key,
        ttl_seconds=GEOCODE_CACHE_TTL_SECONDS,
        version=_GEOCODE_CACHE_VERSION,
    )
    if cache_status == "fresh" and cached is not None:
        return cached

    result = None
    # 1) UK postcode -> Postcodes.io (most accurate, no key)
    m = _UK_POSTCODE_RE.search(address)
    if m:
        pc = m.group(1).upper().replace(' ', '')
        try:
            r = requests.get(f"https://api.postcodes.io/postcodes/{pc}", timeout=8)
            if r.status_code == 200:
                d = (r.json() or {}).get('result') or {}
                if d.get('latitude') is not None:
                    result = {'lat': d['latitude'], 'lng': d['longitude'],
                              'postcode': d.get('postcode'),
                              'geocoder': 'postcodes.io',
                              'resolved_name': d.get('postcode'),
                              'match_type': 'postcode',
                              'place_rank': None}
        except Exception as e:
            print(f"  [geocode] Postcodes.io error: {e}")

    # 2) Fall back to Nominatim (free, no key)
    if result is None:
        try:
            r = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={'q': address, 'format': 'json', 'countrycodes': 'gb',
                        'limit': 1, 'addressdetails': 1},
                headers=_OSM_HEADERS, timeout=10,
            )
            if r.status_code == 200:
                arr = r.json()
                if arr:
                    top = arr[0]
                    result = {'lat': float(top['lat']), 'lng': float(top['lon']),
                              'postcode': (top.get('address') or {}).get('postcode'),
                              'geocoder': 'nominatim',
                              'resolved_name': top.get('display_name'),
                              'match_type': (top.get('addresstype') or top.get('type')
                                             or top.get('class')),
                              'place_rank': top.get('place_rank')}
        except Exception as e:
            print(f"  [geocode] Nominatim error: {e}")

    if result is not None:
        _write_versioned_cache(
            cache_key,
            result,
            ttl_seconds=GEOCODE_CACHE_TTL_SECONDS,
            version=_GEOCODE_CACHE_VERSION,
            provenance={
                "provider": result.get("geocoder"),
            },
        )
    elif cache_status == "stale" and cached is not None:
        return _label_stale(
            cached,
            "Live geocoding refresh failed; coordinates are from an expired cache entry.",
        )
    return result


def _get_coordinates(address: str) -> dict | None:
    """Returns {'lat','lng'} for an address via the free geocoder, or None."""
    geo = _free_geocode(address)
    if not geo:
        return None
    return {'lat': geo['lat'], 'lng': geo['lng']}


def geocode_address(address: str) -> dict | None:
    """Public geocoder returning {'lat','lng','postcode'} (free providers).
    Used by callers that need the postcode (e.g. Transport Zone lookup)."""
    return _free_geocode(address)


def _tfl_travel_time(origin: dict, dest: dict, mode: str = "transit") -> int | None:
    """TfL Journey Planner duration in minutes between two {'lat','lng'} points.
    Returns None when TfL has no journey (e.g. outside London) or on error."""
    frm = f"{origin['lat']},{origin['lng']}"
    to = f"{dest['lat']},{dest['lng']}"
    params = {}
    if TFL_APP_KEY:
        params['app_key'] = TFL_APP_KEY
    if mode in ('walking', 'foot-walking'):
        params['mode'] = 'walking'
    elif mode in ('bicycling', 'cycling-regular', 'cycle'):
        params['mode'] = 'cycle'
    # transit / anything else: let TfL use all public-transport modes
    try:
        url = f"https://api.tfl.gov.uk/Journey/JourneyResults/{frm}/to/{to}"
        resp = requests.get(url, params=params, timeout=12)
        if resp.status_code != 200:
            return None
        journeys = resp.json().get('journeys') or []
        durations = [int(j['duration']) for j in journeys if j.get('duration') is not None]
        return min(durations) if durations else None
    except Exception as e:
        print(f"  [TfL] error: {e}")
        return None


def _tfl_journey(origin: dict, dest: dict, mode: str = "transit") -> dict | None:
    """TfL Journey Planner: returns the fastest journey dict (with legs), or None."""
    frm = f"{origin['lat']},{origin['lng']}"
    to = f"{dest['lat']},{dest['lng']}"
    params = {}
    if TFL_APP_KEY:
        params['app_key'] = TFL_APP_KEY
    if mode in ('walking', 'foot-walking'):
        params['mode'] = 'walking'
    elif mode in ('bicycling', 'cycling-regular', 'cycle'):
        params['mode'] = 'cycle'
    try:
        url = f"https://api.tfl.gov.uk/Journey/JourneyResults/{frm}/to/{to}"
        resp = requests.get(url, params=params, timeout=12)
        if resp.status_code != 200:
            return None
        journeys = [j for j in (resp.json().get('journeys') or []) if j.get('duration') is not None]
        if not journeys:
            return None
        return min(journeys, key=lambda j: j['duration'])
    except Exception as e:
        print(f"  [TfL] journey error: {e}")
        return None


def _summarise_tfl_legs(journey: dict):
    """Turn a TfL journey into (structured legs, human-readable route summary)."""
    legs_out = []
    parts = []
    for leg in journey.get('legs', []):
        mode_name = (leg.get('mode') or {}).get('name', '') or ''
        dur = leg.get('duration')
        lines = [ro.get('name') for ro in (leg.get('routeOptions') or []) if ro.get('name')]
        summary = (leg.get('instruction') or {}).get('summary', '')
        dep = (leg.get('departurePoint') or {}).get('commonName', '')
        arr = (leg.get('arrivalPoint') or {}).get('commonName', '')
        legs_out.append({
            'mode': mode_name, 'lines': lines, 'duration_minutes': dur,
            'summary': summary, 'from': dep, 'to': arr,
        })
        if mode_name.lower() == 'walking':
            parts.append(f"Walk{(' to ' + arr) if arr else ''} ({dur} min)")
        else:
            line_txt = '/'.join(lines) if lines else mode_name
            dest_txt = (' to ' + arr) if arr else ''
            parts.append(f"{mode_name.capitalize()} {line_txt}{dest_txt} ({dur} min)")
    return legs_out, " -> ".join(parts)


def calculate_travel_details(origin_address: str, destination_address: str, mode: str = "transit") -> dict | None:
    """Travel time + route, with the BASIS of the number attached to the number.

    ``duration_minutes`` is set if and only if the TfL Journey Planner actually returned an
    itinerary. When it did not, the straight-line fallback figure goes into
    ``estimated_duration_minutes`` with a range and a caveat, and ``duration_minutes`` is
    ``None`` — a haversine guess must not arrive in a field that reads as a measured journey
    time. See ``core.commute_basis`` for why (short trips estimate 1.8x-6x low).

    Returns None only when the addresses could not be geocoded, i.e. a real failure.
    """
    from core.commute_basis import describe_estimate, describe_measured

    if not origin_address or not destination_address:
        return None
    origin_n = _normalize_address_for_routing(origin_address)
    dest_n = _normalize_address_for_routing(destination_address)
    o = _get_coordinates(origin_n)
    d = _get_coordinates(dest_n)
    if not o or not d:
        return None
    journey = _tfl_journey(o, d, mode)
    if journey is not None:
        legs, summary = _summarise_tfl_legs(journey)
        out = {
            'route_legs': legs,
            'route_summary': summary,
            'source': 'TfL Journey Planner',
            'estimated_duration_minutes': None,
        }
        out.update(describe_measured(int(journey['duration'])))
        return out

    est = straight_line_travel_estimate(origin_n, dest_n, mode)
    if est is None:
        return None
    out = {
        'duration_minutes': None,   # explicit: nothing measured this journey
        'route_legs': [],
        'route_summary': 'No route available: TfL returned no journey for this pair.',
        'source': 'estimate',
    }
    # ``mode`` is threaded through. The calibration is fitted on TfL's fastest itineraries,
    # i.e. public transport only (commute_basis.CALIBRATED_MODES), and up to 2.71 km the raw
    # cycling and transit formulas agree to within LEGACY_MINUTES_TOLERANCE — so from the
    # minutes alone nothing downstream can tell a cycling request apart from a transit one and
    # "cycle 0.8 km" would be answered with a transit-calibrated 14 minutes. Until now the only
    # thing preventing that was commute_basis.withdraw_uncalibrated_mode, applied in
    # calculate_commute and therefore protecting exactly ONE of this function's callers. The
    # producer knows the mode; passing it means the calibration can never be applied to a mode
    # it was not fitted on, whoever calls. withdraw_uncalibrated_mode stays as a second line of
    # defence for callers that pass their own payloads in.
    out.update(describe_estimate(est['minutes'], est.get('distance_km'), mode=mode))
    return out


def calculate_travel_basis(origin_address: str, destination_address: str,
                           mode: str = "transit") -> dict | None:
    """CACHED, basis-aware travel payload — the single producer both commute tools read.

    Same shape as ``calculate_travel_details`` (it IS ``calculate_travel_details``, memoised),
    so ``duration_minutes`` is populated only for a real TfL itinerary and a straight-line guess
    arrives in ``estimated_duration_minutes`` with its range, model, basis and caveat.

    WHY THIS EXISTS. ``calculate_travel_time`` returns a bare int and silently falls back to the
    raw straight-line formula; ``tools/calculate_commute_cost.py`` put that int into
    ``commute.duration_minutes`` and derived ``duration_category`` / ``is_acceptable`` / a
    monthly-hours figure from it. For a 0.47 km pair that meant ``calculate_commute`` answering
    "estimated 11 minutes (9-14), straight-line basis" while ``calculate_commute_cost`` stated
    "2 minutes" as fact — two numbers for one pair inside one turn, the wrong one undisclosed.
    The reason it was not simply switched to ``calculate_travel_details`` was that
    ``calculate_travel_time`` is the CACHED entry point and the commute path is latency-gated.
    So the cache moves here instead of the honesty moving out: one cached producer, one number.

    Returns None only when the addresses could not be geocoded, i.e. a real failure.
    """
    if not origin_address or not destination_address:
        return None
    origin_n = _normalize_address_for_routing(origin_address)
    dest_n = _normalize_address_for_routing(destination_address)
    cache_key = create_cache_key('calculate_travel_basis_v1', origin_n, dest_n, mode)
    cached_result = get_from_cache(cache_key)
    if cached_result is not None:
        print(f"  -> [Cache HIT] Travel basis for: {origin_address} ({mode})")
        return cached_result

    out = calculate_travel_details(origin_address, destination_address, mode)
    if out is None:
        return None
    set_to_cache(cache_key, out)
    return out


def calculate_travel_time(origin_address: str, destination_address: str, mode: str = "transit") -> int | None:
    """BARE minutes for internal thresholding only — filters, sorts, fare heuristics.

    A bare int carries no basis, so NOTHING that shows a figure to a user may call this: use
    ``calculate_travel_basis`` and quote the basis with the number. The docstring is the weaker
    half of that rule; ``test_no_user_facing_caller_takes_a_bare_minutes_figure`` is the guard.

    It is now a thin view over ``calculate_travel_basis``, which is why the two commute tools
    can no longer disagree. On the estimate branch it returns ``best_estimate_minutes`` — the
    CALIBRATED figure inside the fitted domain, the raw formula outside it — the same split
    ``describe_estimate`` publishes and the same one ``commute.coord_commute_minutes`` already
    used for listing annotation. Before this change a 0.47 km pair was filtered at 2 minutes by
    this function and annotated at 11 by ``coord_commute_minutes``, inside one search.

    Unlike the basis payload this never withholds: a filter that drops a listing because the
    honest answer was "no number" would be a silent, invisible failure, whereas a thresholding
    figure is never asserted to anyone. Refusal belongs to the path that PUBLISHES the number.
    """
    from core.commute_basis import best_estimate_minutes, is_measured

    details = calculate_travel_basis(origin_address, destination_address, mode)
    if details is None:
        print(f"  [WARN] Could not geocode origin/destination for travel time")
        return None

    measured = details.get('duration_minutes')
    if measured is not None and is_measured(details.get('source')):
        print(f"  [OK] [TfL] {origin_address} -> {destination_address}: {measured} mins ({mode})")
        return int(measured)

    minutes = best_estimate_minutes(details.get('straight_line_km'), mode)
    if minutes is not None:
        print(f"  [OK] [estimate] {origin_address} -> {destination_address}: {minutes} mins "
              f"(TfL had no route; thresholding figure, not a quotable journey time)")
    return minutes

def find_nearby_places(address: str, amenities_of_interest: list[str], radius: int = 1500) -> dict:
    """Count nearby amenities using OpenStreetMap Overpass (FREE - no API key)."""
    cache_key = create_cache_key('find_nearby_places', address, tuple(sorted(amenities_of_interest)), radius)
    cached_result = get_from_cache(cache_key)
    if cached_result:
        print(f"  -> [Cache HIT] Nearby places for: {address}")
        return cached_result

    print(f"  -> [Overpass] Getting nearby places for: {address}")

    # Map common Google place types to our OSM amenity types.
    type_map = {
        'supermarket': 'supermarket', 'grocery_or_supermarket': 'supermarket',
        'park': 'park', 'gym': 'gym', 'restaurant': 'restaurant',
        'cafe': 'cafe', 'school': 'school', 'hospital': 'hospital',
        'library': 'library',
    }

    poi_summary = {}
    for place_type in amenities_of_interest:
        osm_type = type_map.get(place_type, place_type)
        places = get_nearby_places_osm(address, osm_type, radius_m=radius)
        poi_summary[f"{place_type}_in_{radius}m"] = len(places)

    set_to_cache(cache_key, poi_summary)
    return poi_summary


# crimes-street returns ~1-2k rows per month per point, so each extra month costs real
# seconds. Three is enough to establish a rate and to see a trend.
MONTHS_OF_CRIME_DATA = 3


def get_crime_data_by_location(address: str) -> dict | None:
    """Get crime data from UK Police API with trend analysis"""
    cache_key = create_cache_key('get_crime_data_by_location_v2', address)
    cache_status, cached_result = _read_versioned_cache(
        cache_key,
        ttl_seconds=CRIME_CACHE_TTL_SECONDS,
        version=_CRIME_CACHE_VERSION,
    )
    if cache_status == "fresh" and cached_result:
        print(f"  -> [Cache HIT] Crime data for: {address}")
        return cached_result

    print(f"  -> [API Call] Getting official crime data for: {address}")
    location = _get_coordinates(address)
    
    if not location:
        # Also deliberately uncached, for the same reason as the empty-result branch below.
        print(f"     ❌ Could not geocode address: {address}")
        if cache_status == "stale" and cached_result:
            return _label_stale(
                cached_result,
                "Live crime-data refresh could not geocode the address; "
                "figures are from an expired cache entry.",
            )
        return {"error": "Could not geocode address.", "total_crimes_6m": "Unknown"}
    
    print(f"     [OK] Coordinates: {location['lat']}, {location['lng']}")

    # ENDPOINT, not a detail. `crimes-at-location` returns crimes at ONE pre-defined street
    # anchor -- it answers "what happened at this exact spot". Using it to describe an area
    # under-reports by roughly three orders of magnitude: it gave Hackney Central 9 crimes in
    # six months, which the old scoring turned into "96/100, very safe", while
    # `crimes-street/all-crime` returns 1,657 for ONE month at the same coordinates.
    # `crimes-street/all-crime` covers the ~1 mile radius that people mean by "around here".
    base_date = datetime.now().replace(day=1) - pd.DateOffset(months=2)
    dates_to_fetch = [(base_date - pd.DateOffset(months=i)).strftime('%Y-%m') for i in range(MONTHS_OF_CRIME_DATA)]

    all_crimes = []
    for date_str in dates_to_fetch:
        api_url = (f"https://data.police.uk/api/crimes-street/all-crime"
                   f"?date={date_str}&lat={location['lat']}&lng={location['lng']}")
        try:
            print(f"     -> Fetching {date_str}...")
            response = requests.get(api_url, timeout=15)
            response.raise_for_status()
            crimes = response.json()
            
            if crimes:
                print(f"       [OK] Found {len(crimes)} crimes in {date_str}")
                all_crimes.extend(crimes)
            else:
                print(f"       • No crimes in {date_str}")
        except requests.exceptions.Timeout:
            print(f"       [WARN]  Timeout for {date_str}")
            continue
        except requests.exceptions.RequestException as e:
            print(f"       ❌ API error for {date_str}: {e}")
            continue

    if not all_crimes:
        # DO NOT CACHE A FAILURE. data.police.uk returns intermittent 500/502s (observed
        # repeatedly on 2026-07-26), and caching the empty result freezes "no safety data"
        # for the whole TTL on an area that is perfectly queryable a minute later. Observed
        # live: Hackney Central held an `error` entry while Richmond, fetched seconds apart,
        # cached fine. Returning without persisting means the next request simply retries.
        print(f"     [WARN]  WARNING: No crime data found for any month (NOT cached — will retry)")
        if cache_status == "stale" and cached_result:
            return _label_stale(
                cached_result,
                "UK Police API refresh returned no usable data; figures are "
                "from an expired cache entry.",
            )
        return {
            "total_crimes_6m": "Unknown",
            "crime_trend": "unknown",
            "category_breakdown": "Crime data unavailable",
            "error": "No data returned from UK Police API",
        }

    print(f"     [OK] TOTAL: {len(all_crimes)} crimes across 6 months")
    
    crimes_by_month = Counter(crime['month'] for crime in all_crimes)
    sorted_months = sorted(crimes_by_month.keys())
    counts = [crimes_by_month[m] for m in sorted_months]
    
    crime_trend = "stable"
    if len(counts) > 3:
        first_half_avg = sum(counts[:len(counts)//2]) / (len(counts)//2)
        second_half_avg = sum(counts[len(counts)//2:]) / (len(counts)//2)
        if second_half_avg > first_half_avg * 1.2:
            crime_trend = "increasing"
        elif second_half_avg < first_half_avg * 0.8:
            crime_trend = "decreasing"

    category_counts = Counter(crime['category'].replace('-', ' ').title() for crime in all_crimes)
    
    months_seen = len(sorted_months) or 1
    summary = {
        # total_crimes_6m is kept under its historical name for callers, but it is now the
        # total over MONTHS_OF_CRIME_DATA months of RADIUS data, not six months of
        # single-anchor data. months_covered says which, so nothing has to guess.
        "total_crimes_6m": len(all_crimes),
        "months_covered": months_seen,
        "crimes_per_month": round(len(all_crimes) / months_seen, 1),
        "radius_miles": 1.0,
        "most_recent_month_count": counts[-1] if counts else 0,
        "crime_trend": crime_trend,
        "data_months": sorted_months,
        "category_breakdown": dict(category_counts.most_common(3))
    }
    _write_versioned_cache(
        cache_key,
        summary,
        ttl_seconds=CRIME_CACHE_TTL_SECONDS,
        version=_CRIME_CACHE_VERSION,
        provenance={
            "provider": "UK Police API",
            "endpoint": "crimes-street/all-crime",
            "data_months": sorted_months,
            "radius_miles": 1.0,
        },
    )
    return summary


def get_environmental_data(address: str) -> dict:
    """Get environmental data (parks, air quality estimate)"""
    cache_key = create_cache_key('get_environmental_data', address)
    cached_result = get_from_cache(cache_key)
    if cached_result:
        print(f"  -> [Cache HIT] Environmental data for: {address}")
        return cached_result
    
    print(f"  -> [Overpass] Getting environmental data for: {address}")

    parks = get_nearby_places_osm(address, 'park', radius_m=1000)
    parks_in_1km = len(parks)

    air_quality = "good"
    if parks_in_1km == 0:
        air_quality = "moderate"
    elif parks_in_1km >= 3:
        air_quality = "excellent"

    summary = {
        "air_quality_estimate": air_quality,
        "nearby_parks_1km": parks_in_1km,
    }
    set_to_cache(cache_key, summary)
    return summary


def straight_line_travel_estimate(origin_address: str, destination_address: str,
                                  mode: str = "transit") -> dict | None:
    """Distance-based travel-time GUESS. Returns ``{'minutes', 'distance_km'}`` or None.

    This is NOT a journey time, and callers must not present it as one — see
    ``core.commute_basis`` for what it is worth when measured against real TfL journeys
    (short trips come out 1.8x-6x low). ``distance_km`` is returned alongside the minutes
    precisely so the basis can be stated instead of being dropped on the floor.
    """
    if not origin_address or not destination_address:
        return None

    # v2: the cached value used to be a bare int; the shape changed, so the key must too.
    cache_key = create_cache_key('straight_line_travel_estimate_v2',
                                 origin_address, destination_address, mode)
    cached_result = get_from_cache(cache_key)
    if cached_result is not None:
        return cached_result

    # Try to get coordinates
    origin_coords = _get_coordinates(origin_address)
    dest_coords = _get_coordinates(destination_address)

    if not origin_coords or not dest_coords:
        return None

    # Calculate straight-line distance using Haversine formula
    R = 6371  # Earth radius in kilometers

    lat1_rad = math.radians(origin_coords['lat'])
    lat2_rad = math.radians(dest_coords['lat'])
    dlat = math.radians(dest_coords['lat'] - origin_coords['lat'])
    dlng = math.radians(dest_coords['lng'] - origin_coords['lng'])

    a = math.sin(dlat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlng/2)**2
    c = 2 * math.asin(math.sqrt(a))

    distance_km = R * c

    # Apply realistic multiplier (actual route is ~1.3x straight line)
    actual_distance = distance_km * 1.3

    # Calculate time based on mode
    if mode in ['transit', 'driving']:
        speed = 20  # km/h average
        base_time = (actual_distance / speed) * 60
        wait_time = min(10, distance_km * 2)
        total_minutes = int(base_time + wait_time)
    elif mode in ['bicycling', 'cycling-regular']:
        speed = 15  # km/h
        total_minutes = int((actual_distance / speed) * 60)
    elif mode in ['walking', 'foot-walking']:
        speed = 5  # km/h
        total_minutes = int((actual_distance / speed) * 60)
    else:
        speed = 20
        total_minutes = int((actual_distance / speed) * 60 + 5)

    result = {'minutes': total_minutes, 'distance_km': round(distance_km, 2)}
    set_to_cache(cache_key, result)
    return result


def estimate_travel_time_simple(origin_address: str, destination_address: str, mode: str = "transit") -> int | None:
    """RAW, UNCALIBRATED minutes from ``straight_line_travel_estimate``. No in-repo callers.

    This is the pre-calibration formula's output — the one that reads 2 minutes for a journey
    TfL measures at 12. ``calculate_travel_time`` used to return exactly this and no longer
    does: a bare thresholding figure now comes from ``commute_basis.best_estimate_minutes``,
    which is calibrated inside the fitted domain. Kept only as the named reference for what the
    raw formula says (``scripts/sample_commute_calibration.py`` documents it as such).

    DO NOT wire this back into a product path. Nothing that reaches a user may take minutes
    from here; ``test_no_user_facing_caller_takes_a_bare_minutes_figure`` enforces that in
    source rather than leaving it as a warning in a docstring.
    """
    est = straight_line_travel_estimate(origin_address, destination_address, mode)
    return None if est is None else est['minutes']


def get_nearby_supermarkets_detailed(address: str, radius: int = 2000, 
                                     chains: list[str] | None = None) -> list[dict]:
    """
    多源超市搜索 - 智能级联搜索策略
    
    搜索顺序：
    1. OSM品牌查询（brand=Lidl等）- 精准
    2. OSM通用超市查询（shop=supermarket等）- 通用
    3. 网页搜索回退 - 最后手段
    
    参数：
    - address: 搜索的地址
    - radius: 搜索半径（米）
    - chains: 目标超市品牌列表，如['Lidl', 'Aldi']，默认['Lidl', 'Aldi', 'Sainsbury', 'Tesco']
    
    返回：超市列表，按距离排序
    """
    if chains is None:
        chains = ['Lidl', 'Aldi', 'Sainsbury', 'Tesco']
    
    cache_key = create_cache_key('supermarkets_detailed_v2_multi', address, radius, tuple(chains))
    cached_result = get_from_cache(cache_key)
    if cached_result:
        print(f"    -> [缓存] 找到超市缓存: {address}")
        return cached_result
    
    print(f"    [SEARCH] [多源搜索] 搜索超市: {chains} near {address}")
    
    location = _get_coordinates(address)
    if not location:
        print(f"    -> [多源搜索] 无法地理编码: {address}")
        return []
    
    results = []
    
    # ===== 方法1：OSM品牌查询 =====
    print(f"      方法1: OSM品牌查询...")

    for chain in chains:
        query = f"""
        [out:json][timeout:10];
        (
          node["brand"="{chain}"]["shop"="supermarket"](around:{radius},{location['lat']},{location['lng']});
          node["brand"="{chain}"](around:{radius},{location['lat']},{location['lng']});
          way["brand"="{chain}"]["shop"="supermarket"](around:{radius},{location['lat']},{location['lng']});
          way["brand"="{chain}"](around:{radius},{location['lat']},{location['lng']});
        );
        out center;
        """
        
        try:
            data = overpass_request(query, timeout=15)
            brand_results = _parse_osm_elements(data.get('elements', []), location, chain)
            results.extend(brand_results)
            print(f"        [OK] {chain}: 找到 {len(brand_results)} 家")
        except Exception as e:
            print(f"        [WARN]  {chain} 搜索出错: {e}")
    
    # ===== 方法2：通用超市搜索 =====
    if len(results) < 3:
        print(f"      方法2: OSM通用超市搜索 (已找到 {len(results)} 家，需要补充)...")
        query = f"""
        [out:json][timeout:15];
        (
          node["shop"="supermarket"](around:{radius},{location['lat']},{location['lng']});
          way["shop"="supermarket"](around:{radius},{location['lat']},{location['lng']});
          node["shop"="convenience"](around:{radius},{location['lat']},{location['lng']});
          way["shop"="convenience"](around:{radius},{location['lat']},{location['lng']});
        );
        out center;
        """
        
        try:
            data = overpass_request(query, timeout=20)
            generic_results = _parse_osm_elements(data.get('elements', []), location, 'generic')

            # 去重：只添加新的超市
            existing_names = {r.get('name', '').lower() for r in results}
            new_results = [r for r in generic_results if r.get('name', '').lower() not in existing_names]
            results.extend(new_results)
            print(f"        [OK] 通用搜索: 找到 {len(new_results)} 家新超市")
        except Exception as e:
            print(f"        [WARN]  通用搜索出错: {e}")
    
    # ===== 方法3：网页搜索回退 =====
    if not results:
        print(f"      方法3: 网页搜索回退...")
        try:
            from .web_search import get_search_snippets
            
            for chain in chains[:2]:  # 只搜索前两个品牌
                try:
                    query_text = f"{chain} supermarket near {address} London"
                    snippets = get_search_snippets(query_text, max_results=2)
                    
                    for snippet in snippets:
                        title = snippet.get('title', '')
                        if chain.lower() in title.lower():
                            web_result = {
                                'name': title,
                                'type': 'supermarket',
                                'address': snippet.get('snippet', 'Web result'),
                                'distance_m': None,
                                'source': 'web_search',
                                'url': snippet.get('link', '')
                            }
                            results.append(web_result)
                except Exception as e:
                    print(f"        [WARN]  {chain} 网页搜索出错: {e}")
        except Exception as e:
            print(f"        [WARN]  网页搜索模块不可用: {e}")
    
    # ===== 最终处理 =====
    # 去重、排序和限制数量
    results = _deduplicate_supermarkets(results)
    results.sort(key=lambda x: x.get('distance_m', 999999) if x.get('distance_m') else 999999)
    results = results[:10]  # 最多返回10个
    
    print(f"    [OK] [多源搜索] 总共找到 {len(results)} 家超市")
    set_to_cache(cache_key, results)
    return results


def _parse_osm_elements(elements: list, location: dict, source: str = 'osm') -> list[dict]:
    """
    解析OSM API返回的元素列表
    """
    supermarkets = []
    
    for element in elements:
        tags = element.get('tags', {})
        
        # 获取坐标
        if element['type'] == 'node':
            shop_lat = element['lat']
            shop_lng = element['lon']
        else:  # way (building)
            center = element.get('center', {})
            shop_lat = center.get('lat')
            shop_lng = center.get('lon')
        
        if not shop_lat or not shop_lng:
            continue
        
        # 计算距离（Haversine公式）
        lat1_rad = math.radians(location['lat'])
        lat2_rad = math.radians(shop_lat)
        dlat = math.radians(shop_lat - location['lat'])
        dlng = math.radians(shop_lng - location['lng'])
        
        a = math.sin(dlat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlng/2)**2
        c = 2 * math.asin(math.sqrt(a))
        distance_m = int(6371000 * c)
        
        # 提取信息
        name = tags.get('name', 'Unnamed Shop')
        shop_type = tags.get('shop', 'supermarket')
        street = tags.get('addr:street', '')
        housenumber = tags.get('addr:housenumber', '')
        brand = tags.get('brand', '')
        
        supermarkets.append({
            'name': name,
            'type': shop_type,
            'address': f"{housenumber} {street}".strip() or "Address not available",
            'distance_m': distance_m,
            'lat': shop_lat,
            'lng': shop_lng,
            'brand': brand,
            'source': source
        })
    
    return supermarkets


def _deduplicate_supermarkets(results: list[dict]) -> list[dict]:
    """
    去重超市结果：按名称和距离
    优先级：osm_brand > osm_generic > web_search
    """
    seen_names = set()
    dedup_results = []
    
    # 优先级排序
    priority_map = {'osm': 0, 'generic': 1, 'web_search': 2}
    results_sorted = sorted(
        results,
        key=lambda x: (
            priority_map.get(x.get('source', 'web_search'), 999),
            x.get('distance_m', 999999) if x.get('distance_m') else 999999
        )
    )
    
    for result in results_sorted:
        name_lower = result.get('name', '').lower()
        if name_lower not in seen_names:
            seen_names.add(name_lower)
            dedup_results.append(result)
    
    return dedup_results


def get_nearby_places_osm(address: str, amenity_type: str, radius_m: int = 1500) -> list[dict]:
    """
    Get nearby places using OpenStreetMap Overpass API (FREE - no API key needed)

    Args:
        address: Property address
        amenity_type: Type of amenity (gym, park, restaurant, hospital, library, school)
        radius_m: Search radius in meters (default 1500m = 1.5km)

    Returns:
        List of nearby places with name, distance, address/location. Every place also
        carries ``measured_from`` / ``distance_basis`` / ``reference_precision`` naming the
        point the distance was measured FROM. That is not decoration: this function
        geocodes whatever string it is given, so for an AREA query ("how is Hackney?")
        ``distance_m`` is measured from the borough centroid. "Tesco 110m" then means 110 m
        from a cartographic centre, not from any home the reader could take, and the
        reference travels with each row so no consumer can render the number without it.
    """
    # v2: rows cached under the old key have no reference-point fields.
    cache_key = create_cache_key('get_nearby_places_osm_v2', address, amenity_type, radius_m)
    cached_result = get_from_cache(cache_key)
    if cached_result:
        print(f"  -> [Cache HIT] OSM {amenity_type} data for: {address}")
        return cached_result

    print(f"  -> [Overpass API] Getting {amenity_type} locations near: {address}")

    # Get coordinates for the property, together with what the geocoder actually matched.
    from core.place_reference import reference_point
    ref = reference_point(address)
    if ref.get("error") or ref.get("lat") is None:
        print(f"     ❌ Could not geocode address: {address}")
        return []

    lat, lng = ref['lat'], ref['lng']
    measured_from = ref.get('measured_from')
    ref_precision = ref.get('precision')
    print(f"     [OK] Coordinates: {lat:.4f}, {lng:.4f} ({ref_precision})")
    
    # Map amenity types to OSM tags
    osm_amenity_map = {
        'gym': [('leisure', 'fitness_centre'), ('leisure', 'sports_centre'), ('sport', 'gym')],
        'park': [('leisure', 'park')],
        'restaurant': [('amenity', 'restaurant')],
        'cafe': [('amenity', 'cafe'), ('amenity', 'coffee_shop')],
        'hospital': [('amenity', 'hospital'), ('amenity', 'clinic')],
        'library': [('amenity', 'library')],
        'school': [('amenity', 'school')],
        'supermarket': [('shop', 'supermarket')],
    }
    
    osm_tags = osm_amenity_map.get(amenity_type, [])
    if not osm_tags:
        print(f"     ERROR: Unknown amenity type: {amenity_type}")
        return []
    
    # Calculate bounding box (approximate)
    # 1 degree latitude ≈ 111 km, 1 degree longitude ≈ 111 km * cos(latitude)
    lat_offset = (radius_m / 1000) / 111.0
    lng_offset = (radius_m / 1000) / (111.0 * math.cos(math.radians(lat)))
    
    south = lat - lat_offset
    west = lng - lng_offset
    north = lat + lat_offset
    east = lng + lng_offset
    
    # Build Overpass API query - proper syntax
    # Combine multiple tags with separate queries
    tag_queries = []
    for key, value in osm_tags:
        tag_queries.append(f'node["{key}"="{value}"]({south:.6f},{west:.6f},{north:.6f},{east:.6f});')
        tag_queries.append(f'way["{key}"="{value}"]({south:.6f},{west:.6f},{north:.6f},{east:.6f});')
    
    queries_part = '\n  '.join(tag_queries)
    
    overpass_query = f"""[out:json];
(
  {queries_part}
);
out center;"""
    
    print(f"     [OK] Using Overpass API for {amenity_type} search")

    try:
        data = overpass_request(overpass_query, timeout=25)

        places = []

        for element in data.get('elements', []):
            # Get coordinates - Overpass uses 'lon', not 'lng'
            if 'center' in element:
                place_lat = element['center']['lat']
                place_lon = element['center']['lon']
            elif 'lat' in element and 'lon' in element:
                place_lat = element['lat']
                place_lon = element['lon']
            else:
                continue
            
            # Calculate distance
            distance_m = calculate_distance_m(lat, lng, place_lat, place_lon)
            
            if distance_m > radius_m:
                continue
            
            # Get name
            tags = element.get('tags', {})
            name = tags.get('name', 'Unknown ' + amenity_type)
            
            # Get cuisine type for restaurants (NEW: 添加菜系识别)
            cuisine = tags.get('cuisine', None)
            
            # Get address if available
            address_parts = []
            if 'street' in tags:
                address_parts.append(tags['street'])
            if 'housenumber' in tags:
                address_parts.insert(0, tags['housenumber'])
            if 'postcode' in tags:
                address_parts.append(tags['postcode'])
            
            place_address = ', '.join(address_parts) if address_parts else f"({place_lat:.4f}, {place_lon:.4f})"
            
            place_data = {
                'name': name,
                'type': amenity_type,
                'distance_m': round(distance_m),
                # The reference point rides with every distance, so a consumer cannot
                # render "110m" without also having what it is 110m FROM.
                'distance_basis': 'straight_line',
                'measured_from': measured_from,
                'reference_precision': ref_precision,
                'address': place_address,
                'lat': place_lat,
                'lon': place_lon,  # Use 'lon' to match Overpass API conventions
                'source': 'osm'
            }
            
            # Add cuisine info for restaurants (NEW: 只在餐厅类型时添加)
            if amenity_type == 'restaurant' and cuisine:
                place_data['cuisine'] = cuisine
            
            places.append(place_data)
        
        # Sort by distance
        places.sort(key=lambda x: x['distance_m'])
        
        print(f"     [OK] Found {len(places)} {amenity_type} locations within {radius_m}m")
        set_to_cache(cache_key, places)
        return places

    except OverpassError as e:
        # Every mirror failed. This single-type helper feeds agent tools that
        # expect a list, so we preserve that contract and return [] here; the
        # map pipeline (fetch_all_amenities) propagates OverpassError instead so
        # the user gets an honest "data unavailable" banner.
        print(f"     [WARN] Overpass unavailable for {amenity_type}: {e}")
        return []
    except Exception as e:
        print(f"     [WARN] Error processing Overpass data: {e}")
        return []


def calculate_distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate distance between two coordinates in meters using Haversine formula"""
    R = 6371000  # Earth's radius in meters
    
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lng = math.radians(lng2 - lng1)
    
    a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lng / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    return R * c
