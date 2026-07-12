"""
Scrape-on-demand + persistent cache for the *customer-facing* search path.

The demo used to serve a hardcoded London/Colchester CSV for every query, so a
"Manchester, 1 bed" request came back with London flats. This module replaces
that with real, city-correct OnTheMarket listings fetched on demand and cached:

    resolve (location, beds, price band)
        -> look up a persistent SQLite store (TTL, default 12h)
        -> on hit & fresh:  serve cached rows            (warm, ~ms)
        -> on miss/stale:   scrape OnTheMarket live       (cold, a few seconds),
                            persist, serve
        -> stale-if-error:  if a live scrape fails but an older cached set exists
                            for the query, serve it flagged possibly-outdated
        -> nothing at all:  return an honest empty result (NEVER demo rows)

City-correctness is structural: the location resolves to a specific OnTheMarket
area/city slug, so a Manchester query hits the Manchester area page. A light
cross-contamination guard additionally drops any row whose address names a
*different* major UK city than the one requested.

The bundled fake CSV is only reachable behind SEARCH_ALLOW_DEMO_FALLBACK
(default OFF) for fully offline development.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

from . import onthemarket

# --------------------------------------------------------------------------
# Tunables (all env-overridable)
# --------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
REPO_ROOT = _THIS.parents[3]

# 12h TTL: rental listings turn over slowly enough that a half-day-old snapshot
# is still accurate, while keeping the cache warm across a demo session.
TTL_HOURS = float(os.getenv("SEARCH_CACHE_TTL_HOURS", "12"))
# Wall-clock budget for a single live scrape before we give up and fall back to
# a stale cache / honest-empty. The scraper is normally ~2-3s.
SCRAPE_BUDGET_S = float(os.getenv("SEARCH_SCRAPE_BUDGET_S", "60"))
# Offline-dev escape hatch. OFF by default: the customer path must never serve
# fake rows.
ALLOW_DEMO_FALLBACK = os.getenv("SEARCH_ALLOW_DEMO_FALLBACK", "").strip().lower() in {
    "1", "true", "yes", "on",
}
CACHE_PATH = Path(
    os.getenv("SEARCH_LISTING_CACHE_PATH", str(REPO_ROOT / ".runtime" / "listing_cache.sqlite3"))
)

# --------------------------------------------------------------------------
# Location -> OnTheMarket slug resolution
# --------------------------------------------------------------------------
# Major UK cities: OnTheMarket's plain area slug works for these (verified for
# london / manchester / leeds / edinburgh; the rest follow the same pattern).
CITY_SLUGS = {
    "london": "london", "manchester": "manchester", "birmingham": "birmingham",
    "leeds": "leeds", "liverpool": "liverpool", "bristol": "bristol",
    "sheffield": "sheffield", "nottingham": "nottingham", "leicester": "leicester",
    "coventry": "coventry", "newcastle": "newcastle-upon-tyne", "glasgow": "glasgow",
    "edinburgh": "edinburgh", "cardiff": "cardiff", "oxford": "oxford",
    "cambridge": "cambridge", "york": "york", "brighton": "brighton",
    "reading": "reading", "southampton": "southampton", "portsmouth": "portsmouth",
    "bath": "bath", "durham": "durham", "exeter": "exeter", "norwich": "norwich",
    "hull": "hull", "preston": "preston", "plymouth": "plymouth", "swansea": "swansea",
    "aberdeen": "aberdeen", "dundee": "dundee", "belfast": "belfast", "derby": "derby",
    "stoke": "stoke-on-trent", "wolverhampton": "wolverhampton", "sunderland": "sunderland",
    "salford": "salford", "loughborough": "loughborough", "lancaster": "lancaster",
}

# Landmarks / universities -> (slug, canonical_city). Checked before the plain
# city table so "oxford street" maps to London, not the city of Oxford.
LANDMARK_SLUGS = {
    "ucl": ("bloomsbury", "london"),
    "university college london": ("bloomsbury", "london"),
    "soas": ("bloomsbury", "london"),
    "birkbeck": ("bloomsbury", "london"),
    "kcl": ("holborn", "london"),
    "king's college": ("holborn", "london"),
    "kings college": ("holborn", "london"),
    "king's college london": ("holborn", "london"),
    "lse": ("holborn", "london"),
    "london school of economics": ("holborn", "london"),
    "imperial college": ("south-kensington", "london"),
    "imperial college london": ("south-kensington", "london"),
    "queen mary": ("mile-end", "london"),
    "qmul": ("mile-end", "london"),
    "city university": ("islington", "london"),
    "uel": ("stratford-london", "london"),
    "university of east london": ("stratford-london", "london"),
    "university of greenwich": ("greenwich", "london"),
    "canary wharf": ("canary-wharf", "london"),
    "london bridge": ("london-bridge", "london"),
    "kings cross": ("kings-cross", "london"),
    "king's cross": ("kings-cross", "london"),
    "st pancras": ("kings-cross", "london"),
    "camden": ("camden", "london"),
    "shoreditch": ("shoreditch", "london"),
    "elephant and castle": ("elephant-and-castle", "london"),
    "central london": ("london", "london"),
    "oxford street": ("london", "london"),
    "oxford circus": ("london", "london"),
    "soho": ("soho", "london"),
    "stratford": ("stratford-london", "london"),
    "wembley": ("wembley-park", "london"),
    "mile end": ("mile-end", "london"),
    "bloomsbury": ("bloomsbury", "london"),
}

# Cross-contamination guard: if the requested city is one of these, drop any row
# whose address names a *different* one of these.
_MAJOR_CITIES = set(CITY_SLUGS) | {"newcastle"}

# LANDMARK_SLUGS keys that are universities. classify_place() uses this to tell a
# university (whose legacy `location="UCL"` call should keep its commute annotation)
# from a plain area landmark (Camden, Shoreditch, ...). Every entry here also exists
# in LANDMARK_SLUGS above.
UNIVERSITY_KEYS = frozenset({
    "ucl", "university college london", "soas", "birkbeck",
    "kcl", "king's college", "kings college", "king's college london",
    "lse", "london school of economics",
    "imperial college", "imperial college london",
    "queen mary", "qmul", "city university",
    "uel", "university of east london", "university of greenwich",
})

# ==========================================================================
# Destination detection (classify_place tiers 1-3)
# --------------------------------------------------------------------------
# A place name can be a residential AREA (somewhere to live) or a DESTINATION
# the user commutes TO — a university/school or a workplace/employer. The search
# layer defaults destinations to commute-mode, so classify_place() labels each
# name and, for destinations, hands back a GEOCODABLE address for reliable
# commute routing. Three tiers, cheapest first, memoized so a name is classified
# at most once per process:
#   1) curated tables + keyword heuristics — instant, decisive, NO network
#   2) OSM/Nominatim place type            — only on a tier-1 miss
#   3) DeepSeek LLM                        — only when OSM is ambiguous
# ==========================================================================

# Curated geocodable addresses for the universities in UNIVERSITY_KEYS. Every
# UNIVERSITY_KEYS entry has one so a curated university hit returns a precise,
# routable address with zero network calls.
UNIVERSITY_ADDRESSES = {
    "ucl": "Gower Street, London WC1E 6BT",
    "university college london": "Gower Street, London WC1E 6BT",
    "soas": "Thornhaugh Street, Russell Square, London WC1H 0XG",
    "birkbeck": "Malet Street, Bloomsbury, London WC1E 7HX",
    "kcl": "Strand, London WC2R 2LS",
    "king's college": "Strand, London WC2R 2LS",
    "kings college": "Strand, London WC2R 2LS",
    "king's college london": "Strand, London WC2R 2LS",
    "lse": "Houghton Street, London WC2A 2AE",
    "london school of economics": "Houghton Street, London WC2A 2AE",
    "imperial college": "Exhibition Road, South Kensington, London SW7 2AZ",
    "imperial college london": "Exhibition Road, South Kensington, London SW7 2AZ",
    "queen mary": "Mile End Road, London E1 4NS",
    "qmul": "Mile End Road, London E1 4NS",
    "city university": "Northampton Square, London EC1V 0HB",
    "uel": "University Way, London E16 2RD",
    "university of east london": "University Way, London E16 2RD",
    "university of greenwich": "Old Royal Naval College, Park Row, London SE10 9LS",
}

# Curated NEW workplace names: major UK employers / office districts, keyed by a
# distinctive whole-word token, mapped to (geocodable_address, canonical_city).
# These are checked BEFORE the plain city-substring match so "Deloitte London"
# resolves to a workplace rather than the London *area*. Existing curated
# residential/landmark AREAS (Camden, Canary Wharf, ...) are deliberately absent
# here — they stay `area` so someone who wants to LIVE there is not surprised.
WORKPLACE_ADDRESSES = {
    # Big-4 accountancy / professional services
    "deloitte": ("1 New Street Square, London EC4A 3HQ", "london"),
    "pwc": ("1 Embankment Place, London WC2N 6RH", "london"),
    "pricewaterhousecoopers": ("1 Embankment Place, London WC2N 6RH", "london"),
    "kpmg": ("15 Canada Square, London E14 5GL", "london"),
    "ernst young": ("1 More London Place, London SE1 2AF", "london"),
    "ernst and young": ("1 More London Place, London SE1 2AF", "london"),
    "accenture": ("30 Fenchurch Street, London EC3M 3BD", "london"),
    # Major banks / finance
    "barclays": ("1 Churchill Place, London E14 5HP", "london"),
    "hsbc": ("8 Canada Square, London E14 5HQ", "london"),
    "natwest": ("250 Bishopsgate, London EC2M 4AA", "london"),
    "lloyds bank": ("25 Gresham Street, London EC2V 7HN", "london"),
    "santander": ("2 Triton Square, Regent's Place, London NW1 3AN", "london"),
    "jp morgan": ("25 Bank Street, London E14 5JP", "london"),
    "jpmorgan": ("25 Bank Street, London E14 5JP", "london"),
    "goldman sachs": ("Plumtree Court, 25 Shoe Lane, London EC4A 4AU", "london"),
    "morgan stanley": ("20 Bank Street, London E14 4AD", "london"),
    "citigroup": ("25 Canada Square, London E14 5LB", "london"),
    "citibank": ("25 Canada Square, London E14 5LB", "london"),
    "bank of america": ("2 King Edward Street, London EC1A 1HQ", "london"),
    "deutsche bank": ("21 Moorfields, London EC2Y 9DB", "london"),
    "bloomberg": ("3 Queen Victoria Street, London EC4N 4TQ", "london"),
    # Large tech offices
    "google": ("6 Pancras Square, London N1C 4AG", "london"),
    "amazon": ("1 Principal Place, Worship Street, London EC2A 2FA", "london"),
    "microsoft": ("2 Kingdom Street, London W2 6BD", "london"),
    "meta": ("10 Brock Street, Regent's Place, London NW1 3FG", "london"),
    "facebook": ("10 Brock Street, Regent's Place, London NW1 3FG", "london"),
    # Office districts
    "city of london": ("Bank, City of London, London EC3V 3LA", "london"),
    "square mile": ("Bank, City of London, London EC3V 3LA", "london"),
}
# Longest key first so multi-word employers ("bank of america") win over any
# shorter token they contain.
_WORKPLACE_NAME_KEYS = sorted(WORKPLACE_ADDRESSES, key=len, reverse=True)

# Keyword heuristics on the raw name. Education-ish -> university; employer-ish
# -> workplace. Only strong, low-false-positive whole-word tokens are decisive:
# bare "bank"/"tower"/"college"/"school" are intentionally NOT here (they collide
# with residential names like South Bank, Tower Hamlets, College Green) — OSM
# tier 2 classifies those accurately instead.
_EDU_KEYWORD_RE = re.compile(r"\b(universit(?:y|ies)|campus|polytechnic|institute)\b")
_EMPLOYER_KEYWORD_RE = re.compile(
    r"\b(hq|headquarters|ltd|plc|llp|inc|incorporated|corp|corporation|gmbh|"
    r"hospital|nhs|clinic|infirmary|offices?|company)\b"
)

# Tier-2 OSM (Nominatim jsonv2) type buckets. `category`/`type`/`addresstype`
# map onto our three kinds; anything else is "ambiguous" -> tier 3.
_OSM_TIMEOUT_S = float(os.getenv("CLASSIFY_OSM_TIMEOUT_S", "4"))
_OSM_PLACE_TYPES = {
    "city", "town", "suburb", "neighbourhood", "neighborhood", "quarter",
    "village", "hamlet", "residential", "locality", "borough", "city_block",
    "municipality", "allotments", "isolated_dwelling",
}
_OSM_EDU_TYPES = {"university", "college", "school"}
_OSM_WORK_AMENITY = {"hospital", "clinic"}
_OSM_WORK_BUILDING = {"office", "commercial", "industrial"}

# Per-process memo: normalized name -> full classify_place result. Curated hits
# populate it without ever touching the network, so repeat calls in the hot
# search path stay O(1).
_CLASSIFY_CACHE: dict = {}
_CLASSIFY_CACHE_MAX = 4096


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9'\s-]", " ", (text or "").lower()).strip()


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", (text or "").lower())
    s = re.sub(r"\s+", "-", s.strip())
    return re.sub(r"-+", "-", s).strip("-")


def _match_location(location: str) -> tuple[str, str | None, str | None, str | None]:
    """Shared resolution core for resolve_location + classify_place.

    Returns (slug, city, matched_key, source) where source is
    'landmark' | 'city' | None and matched_key is the LANDMARK/CITY key that
    matched (None when nothing matched -> slug is the slugified input). Exposing
    the matched key lets classify_place distinguish universities from plain areas
    without duplicating the (order-sensitive) match logic."""
    n = _norm(location)
    if not n:
        return "", None, None, None

    # 1) exact landmark, 2) exact city
    if n in LANDMARK_SLUGS:
        slug, city = LANDMARK_SLUGS[n]
        return slug, city, n, "landmark"
    if n in CITY_SLUGS:
        return CITY_SLUGS[n], n, n, "city"

    # 3) landmark substring (longest key first so specifics win)
    for key in sorted(LANDMARK_SLUGS, key=len, reverse=True):
        if key in n:
            slug, city = LANDMARK_SLUGS[key]
            return slug, city, key, "landmark"

    # 4) city substring, word-boundary (e.g. "University of Manchester" -> manchester)
    for key in sorted(CITY_SLUGS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", n):
            return CITY_SLUGS[key], key, key, "city"

    # 5) LAST-RESORT plain-substring fallback for glued typos ("axomanchester" ->
    #    manchester, "axocamden" -> camden). Ordered strictly AFTER the exact +
    #    word-boundary steps so normal inputs are unchanged; only keys >= 5 chars are
    #    eligible (shorter keys would false-positive), longest key first.
    _fallback_keys = sorted(
        (k for k in set(LANDMARK_SLUGS) | set(CITY_SLUGS) if len(k) >= 5),
        key=len, reverse=True,
    )
    for key in _fallback_keys:
        if key in n:
            if key in LANDMARK_SLUGS:
                slug, city = LANDMARK_SLUGS[key]
                return slug, city, key, "landmark"
            return CITY_SLUGS[key], key, key, "city"

    # 6) unknown -> slugify and let the scraper decide
    return _slugify(location), None, None, None


def resolve_location(location: str) -> tuple[str, str | None]:
    """Map a user destination (city, university, landmark) to an OnTheMarket area
    slug and, when known, its canonical city (for the contamination guard).

    Unknown locations are slugified and tried as-is; an unrecognised slug simply
    404s -> zero rows -> honest empty result (never wrong-city data)."""
    slug, city, _key, _source = _match_location(location)
    return slug, city


def is_destination(kind) -> bool:
    """True when `kind` denotes a commute DESTINATION (university or workplace).

    Accepts either a kind string ("university"/"workplace"/"area"/"unknown") or a
    full classify_place() result dict, so callers can pass whichever they have."""
    if isinstance(kind, dict):
        kind = kind.get("kind")
    return kind in {"university", "workplace"}


def _dest_result(kind: str, slug: str, city: str | None,
                 address: str | None, source: str) -> dict:
    """Assemble a DESTINATION result, guaranteeing a non-empty geocodable address."""
    return {
        "kind": kind, "slug": slug, "city": city,
        "address": (address or None), "source": source,
    }


def _match_workplace_name(n: str) -> str | None:
    """Return the curated WORKPLACE_ADDRESSES key whose distinctive token appears
    as a whole word in the normalized name `n`, else None (longest key first)."""
    for key in _WORKPLACE_NAME_KEYS:
        if re.search(rf"\b{re.escape(key)}\b", n):
            return key
    return None


def _osm_classify(name: str):
    """Tier 2: geocode `name` via OSM Nominatim and read its OSM type.

    Reuses maps_service's descriptive User-Agent (the reference server rejects the
    default python-requests UA) and asks for jsonv2 + addressdetails so we can read
    `category`/`type`/`addresstype`. Returns ``(kind, display_name)`` where kind is
    "university" | "workplace" | "area" | None. ``None`` with a non-None
    display_name means "OSM found a place but its type is ambiguous" (a tier-3
    signal); ``(None, None)`` means not found / lookup failed. NEVER raises."""
    try:
        import requests
        from core.maps_service import _OSM_HEADERS
    except Exception as e:  # maps_service/requests unavailable -> degrade quietly
        print(f"  [classify_place] OSM tier unavailable: {e}")
        return (None, None)
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": name, "format": "jsonv2", "countrycodes": "gb",
                    "limit": 1, "addressdetails": 1},
            headers=_OSM_HEADERS, timeout=_OSM_TIMEOUT_S,
        )
        if resp.status_code != 200:
            return (None, None)
        arr = resp.json()
    except Exception as e:
        print(f"  [classify_place] OSM lookup failed for '{name}': {e}")
        return (None, None)
    if not arr:
        return (None, None)
    top = arr[0] if isinstance(arr, list) else {}
    cat = str(top.get("category") or top.get("class") or "").lower()
    typ = str(top.get("type") or "").lower()
    addrtype = str(top.get("addresstype") or "").lower()
    display = top.get("display_name") or None

    if cat == "amenity" and typ in _OSM_EDU_TYPES:
        return ("university", display)
    if cat == "amenity" and typ in _OSM_WORK_AMENITY:
        return ("workplace", display)
    if cat == "office":
        return ("workplace", display)
    if cat == "building" and typ in _OSM_WORK_BUILDING:
        return ("workplace", display)
    if cat in ("place", "boundary") and (typ in _OSM_PLACE_TYPES
                                         or addrtype in _OSM_PLACE_TYPES):
        return ("area", display)
    if addrtype in _OSM_PLACE_TYPES:
        return ("area", display)
    # OSM found something, but not clearly place/edu/work -> ambiguous (tier 3).
    return (None, display)


def _llm_classify(name: str):
    """Tier 3: ask the configured LLM (DeepSeek) to classify an ambiguous UK place
    name for a rental context. Returns "university" | "workplace" | "area" |
    "unknown" | None. GRACEFUL: any failure / unavailable LLM -> None (never raises)."""
    try:
        from core.llm_interface import call_ollama, extract_first_json
        system_prompt = (
            "You classify a UK place name for a rental-search assistant. Decide whether "
            "a renter would LIVE there (a residential area) or COMMUTE there (a university "
            "or a workplace/employer). Reply with ONLY a JSON object, no prose."
        )
        prompt = (
            f'Place name: "{name}"\n\n'
            'Return exactly: {"category": "<one of: residential_area, university, '
            'workplace, unknown>"}'
        )
        resp = call_ollama(prompt, system_prompt, timeout=20)
        if not resp:
            return None
        data = extract_first_json(resp) or {}
        cat = str(data.get("category", "")).strip().lower()
        return {
            "residential_area": "area", "area": "area",
            "university": "university", "workplace": "workplace",
            "unknown": "unknown",
        }.get(cat)
    except Exception as e:
        print(f"  [classify_place] LLM tier failed for '{name}': {e}")
        return None


def _classify_uncached(name: str) -> dict:
    """The 3-tier decision for one name (see the module header). Memoized by
    classify_place(); this function itself does the network work when reached."""
    slug, city, matched_key, source = _match_location(name)
    n = _norm(name)

    # ---- Tier 1: curated tables + keyword heuristics (instant, no network) ----
    # 1a) curated university (legacy semantics preserved: kind stays "university").
    if source == "landmark" and matched_key in UNIVERSITY_KEYS:
        return _dest_result("university", slug, city,
                            UNIVERSITY_ADDRESSES.get(matched_key) or name, "curated")

    # 1b) education keyword -> university (e.g. "University of Warwick").
    if _EDU_KEYWORD_RE.search(n):
        return _dest_result("university", slug, city, name, "curated")

    # 1c) curated employer name -> workplace, with its precise address + city.
    wp_key = _match_workplace_name(n)
    if wp_key:
        addr, wp_city = WORKPLACE_ADDRESSES[wp_key]
        w_city = city or wp_city
        # Prefer a real residential slug: a city match already gives one; else fall
        # back to the curated city's slug so an area-less workplace search still works.
        w_slug = slug
        if source != "city" and wp_city:
            w_slug = CITY_SLUGS.get(wp_city, slug)
        return _dest_result("workplace", w_slug, w_city, addr or name, "curated")

    # 1d) employer keyword -> workplace (e.g. "Barclays HQ", "St Thomas' Hospital").
    if _EMPLOYER_KEYWORD_RE.search(n):
        return _dest_result("workplace", slug, city, name, "curated")

    # 1e) any other curated landmark/city -> area (Camden, Canary Wharf, Manchester).
    if matched_key is not None:
        return {"kind": "area", "slug": slug, "city": city,
                "address": None, "source": "curated"}

    # ---- Tier 2: OSM/Nominatim place type (only reached on a tier-1 miss) ----
    osm_kind, osm_display = _osm_classify(name)
    if osm_kind == "area":
        return {"kind": "area", "slug": slug, "city": city,
                "address": None, "source": "osm"}
    if osm_kind in ("university", "workplace"):
        return _dest_result(osm_kind, slug, city, osm_display or name, "osm")

    # ---- Tier 3: LLM — ONLY when OSM found a place of an ambiguous (non-place)
    # type. A plain residential/unknown name (place type, or nothing found) never
    # reaches the LLM, keeping cost bounded. ----
    if osm_display is not None:
        llm_kind = _llm_classify(name)
        if llm_kind in ("university", "workplace"):
            return _dest_result(llm_kind, slug, city, osm_display or name, "llm")
        if llm_kind == "area":
            return {"kind": "area", "slug": slug, "city": city,
                    "address": None, "source": "llm"}
        # LLM unavailable / said unknown, but OSM did find a real GB place:
        # treat it as an area (a place you can live near) rather than unknown.
        return {"kind": "area", "slug": slug, "city": city,
                "address": None, "source": "osm"}

    # Nothing matched anywhere and no destination signal -> honest unknown.
    return {"kind": "unknown", "slug": slug, "city": city,
            "address": None, "source": "fallback"}


def classify_place(name: str) -> dict:
    """Classify a place name for the search layer (memoized, 3-tier).

    Returns::

        {
          "kind":    "university" | "workplace" | "area" | "unknown",
          "slug":    str,          # residential/searchable OnTheMarket slug
          "city":    str | None,   # canonical city (for the contamination guard)
          "address": str | None,   # geocodable full address for a DESTINATION; None for area/unknown
          "source":  "curated" | "osm" | "llm" | "fallback",  # which tier decided (debug)
        }

    - "university" / "workplace": a commute DESTINATION. `address` is a geocodable
      full address so the search layer's commute routing is reliable; the caller
      should default such a name to commute-mode.
    - "area": a residential area/landmark/city (Camden, Canary Wharf, Manchester).
    - "unknown": nothing matched and no destination signal -> slug = slugified input.

    Tier 1 (curated tables + keyword heuristics) is instant and never touches the
    network; only a tier-1 miss consults OSM (tier 2), and only an ambiguous OSM
    result consults the LLM (tier 3). Results are memoized per normalized name."""
    key = _norm(name)
    cached = _CLASSIFY_CACHE.get(key)
    if cached is not None:
        return dict(cached)  # copy so callers can't mutate the shared cache entry
    result = _classify_uncached(name)
    if len(_CLASSIFY_CACHE) >= _CLASSIFY_CACHE_MAX:
        _CLASSIFY_CACHE.clear()  # bound memory in a long-lived server; names are few
    _CLASSIFY_CACHE[key] = result
    return dict(result)


# ==========================================================================
# Destination-in-message scan
# --------------------------------------------------------------------------
# classify_place() answers "is THIS name a destination?" but the customer path
# also needs "does the raw MESSAGE name a destination?" — because a bare-city
# area grab ("...Google office in London" -> area="London") short-circuits the
# per-name destination path, losing the workplace. extract_destination_from_text
# pulls conservative, proper-noun-anchored candidate phrases from the message and
# validates each through classify_place, returning the first that is a DESTINATION.
#
# Conservatism is structural: a candidate is only sent to classify_place when it
# carries a tier-1 destination signal (curated employer/university token, or an
# education/employer keyword). That guarantees (a) NO network call from this scan
# — every gated candidate is decided by classify_place's instant tier-1 tables —
# and (b) plain areas ("London", "Camden", "Shoreditch", "the park in Camden")
# never pass the gate, so they can never be mistaken for a commute destination.
# ==========================================================================

# Lowercase descriptor words that trail an employer/institution NAME and are
# themselves a destination signal ("Google office", "Vodafone HQ",
# "Warwick campus"). Every token here also trips the tier-1 employer/education
# keyword in classify_place, so a candidate carrying one stays network-free.
_DEST_DESC_RE = (
    r"office|offices|hq|headquarters|head\s+office|campus|university|college|"
    r"school|hospital|infirmary|clinic|plc|ltd|llp"
)

# One capitalized proper-noun token; apostrophes/hyphens/ampersands inside a token
# are kept ("King's", "Warwick", "O'Neill").
_PROPER_TOKEN = r"[A-Z][A-Za-z0-9'&.\-]*"

# A destination candidate = a capitalized proper-noun RUN (tokens joined by
# spaces or the connectors of/and/&, e.g. "Bank of America", "Imperial College",
# "Deloitte London"), optionally trailed by DESCRIPTOR words ("Google office"),
# optionally with an "in <City>" tail ("Amazon office in Manchester").
_DEST_CANDIDATE_RE = re.compile(
    rf"(?P<run>{_PROPER_TOKEN}(?:\s+(?:of\b|and\b|&|{_PROPER_TOKEN}))*)"
    rf"(?P<desc>(?:\s+(?:{_DEST_DESC_RE}))*)"
    rf"(?:\s+in\s+(?P<city>{_PROPER_TOKEN}(?:\s+{_PROPER_TOKEN})*))?"
)


def _dest_candidate_gate(cand: str) -> bool:
    """True only when ``cand`` carries a TIER-1 destination signal, so the follow-up
    classify_place() call is decided instantly from the curated tables / keyword
    heuristics and never touches the network. This is what keeps the message scan
    conservative (plain areas fail the gate) and offline-safe."""
    n = _norm(cand)
    if not n:
        return False
    if _match_workplace_name(n):
        return True
    if _EDU_KEYWORD_RE.search(n) or _EMPLOYER_KEYWORD_RE.search(n):
        return True
    _slug, _city, key, source = _match_location(cand)
    return source == "landmark" and key in UNIVERSITY_KEYS


def extract_destination_from_text(text: str) -> dict | None:
    """Scan a raw user message for a commute DESTINATION (university/workplace) and
    return the first confirmed one, else None.

    Returns the full classify_place() result dict augmented with ``"name"`` (the
    human-readable candidate phrase that matched, for gate/acknowledgment copy),
    e.g.::

        {"kind": "workplace", "slug": "london", "city": "london",
         "address": "6 Pancras Square, London N1C 4AG", "source": "curated",
         "name": "Google office in London"}

    Conservative by construction: candidates are proper-noun-anchored and must pass
    _dest_candidate_gate (a tier-1 destination signal) before classify_place is
    consulted, so a residential area / bare city ("London", "Camden", "the park in
    Shoreditch") yields None and never a false lock. NEVER raises."""
    if not text or not text.strip():
        return None
    seen: set[str] = set()
    try:
        for m in _DEST_CANDIDATE_RE.finditer(text):
            run = " ".join((m.group("run") or "").split())
            desc = " ".join((m.group("desc") or "").split())
            city = " ".join((m.group("city") or "").split())
            if not run:
                continue
            base = f"{run} {desc}".strip() if desc else run
            # Try the most descriptive phrasing first so the locked/acknowledged
            # destination name reads naturally, then fall back to the bare run.
            forms = []
            if city:
                forms.append(f"{base} in {city}")
            forms.append(base)
            if city and base != run:
                forms.append(f"{run} in {city}")
            if base != run:
                forms.append(run)
            for cand in forms:
                cand = " ".join(cand.split())
                key = cand.lower()
                if key in seen:
                    continue
                seen.add(key)
                if not _dest_candidate_gate(cand):
                    continue
                place = classify_place(cand)
                if is_destination(place):
                    result = dict(place)
                    result["name"] = cand
                    return result
    except Exception as e:  # never let a scan turn a search into a 500
        print(f"  [extract_destination_from_text] scan failed: {e}")
        return None
    return None


def _wrong_city(address: str, requested_city: str | None) -> bool:
    """True if `address` clearly belongs to a major UK city other than the one
    requested. Only fires when both sides are recognised major cities, so local
    suburbs (e.g. Feltham for a London search) are never dropped."""
    if not requested_city or requested_city not in _MAJOR_CITIES:
        return False
    addr = (address or "").lower()
    for city in _MAJOR_CITIES:
        if city == requested_city:
            continue
        if re.search(rf"\b{re.escape(city)}\b", addr):
            # ...unless the requested city is also named (shared area names).
            if not re.search(rf"\b{re.escape(requested_city)}\b", addr):
                return True
    return False


# --------------------------------------------------------------------------
# Persistent SQLite store (write-time timestamp -> real TTL + stale-if-error)
# --------------------------------------------------------------------------
class ListingCache:
    """Tiny per-query listing store. Rows are kept even once stale so a failed
    live scrape can still serve a stale-but-honest snapshot."""

    def __init__(self, path: Path = CACHE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS listings ("
                "key TEXT PRIMARY KEY, rows TEXT NOT NULL, fetched REAL NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def get(self, key: str) -> tuple[list[dict], float] | None:
        """Return (rows, fetched_epoch) or None if the key was never stored."""
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT rows, fetched FROM listings WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0]), float(row[1])
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def set(self, key: str, rows: list[dict]) -> None:
        payload = json.dumps(rows, ensure_ascii=False)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO listings(key, rows, fetched) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET rows=excluded.rows, fetched=excluded.fetched",
                (key, payload, time.time()),
            )


_CACHE: ListingCache | None = None


def _cache() -> ListingCache:
    global _CACHE
    if _CACHE is None:
        try:
            candidate = ListingCache()
            with sqlite3.connect(candidate.path, timeout=1) as db:
                db.execute("BEGIN IMMEDIATE")
                db.rollback()
            _CACHE = candidate
        except (OSError, sqlite3.Error) as exc:
            fallback_path = Path(tempfile.gettempdir()) / "uk-rent-agent" / "listing_cache.sqlite3"
            print(
                f"  [on_demand] listing cache {CACHE_PATH} is not writable ({exc}); "
                f"using {fallback_path}"
            )
            _CACHE = ListingCache(fallback_path)
    return _CACHE

def _fallback_cache(exc: Exception) -> ListingCache:
    global _CACHE
    fallback_path = Path(tempfile.gettempdir()) / "uk-rent-agent" / "listing_cache.sqlite3"
    print(
        f"  [on_demand] listing cache {_CACHE.path if _CACHE else CACHE_PATH} failed during use ({exc}); "
        f"switching to {fallback_path}"
    )
    _CACHE = ListingCache(fallback_path)
    return _CACHE


def _query_key(slug: str, min_beds: int, max_beds: int, min_price: int, max_price: int) -> str:
    # Bucket the price band to 100s so near-identical budgets share a cache entry.
    lo = (int(min_price) // 100) * 100
    hi = ((int(max_price) + 99) // 100) * 100
    return f"otm|{slug}|b{min_beds}-{max_beds}|p{lo}-{hi}"


def _scrape_live(slug, min_beds, max_beds, min_price, max_price, limit, budget_s):
    """Run the OnTheMarket scrape under a wall-clock budget. Returns the row list
    on success, or None if it errored / timed out (-> caller does stale-if-error)."""
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(
        onthemarket.find_rich_onthemarket,
        slug, 1.0, int(min_price), int(max_price),
        limit, int(min_beds), int(max_beds),
    )
    try:
        rows = fut.result(timeout=budget_s)
        ex.shutdown(wait=False)
        return rows
    except FuturesTimeout:
        print(f"  [on_demand] scrape budget ({budget_s}s) exceeded for '{slug}'")
        ex.shutdown(wait=False)
        return None
    except Exception as e:  # network/parse failure -> stale-if-error
        print(f"  [on_demand] scrape error for '{slug}': {e}")
        ex.shutdown(wait=False)
        return None


def _clean(rows: list[dict], requested_city: str | None) -> list[dict]:
    """Drop placeholder/advert rows (no address/url/price) and wrong-city rows."""
    out = []
    for r in rows or []:
        if not (r.get("Address") and r.get("URL") and r.get("Price")):
            continue
        if _wrong_city(r.get("Address", ""), requested_city):
            continue
        out.append(r)
    return out


def get_listings(
    location: str,
    min_bedrooms: int = 0,
    max_bedrooms: int = 2,
    min_price: int = 100,
    max_price: int = 5000,
    limit: int = 15,
    *,
    force_refresh: bool = False,
    budget_s: float | None = None,
) -> dict:
    """Resolve a query to real, city-correct OnTheMarket listings via the
    scrape-on-demand + persistent-cache pipeline.

    Returns a dict::

        {
          "rows": [ <rich-schema dict>, ... ],   # possibly empty, never fake
          "meta": {
            "slug", "requested_location", "requested_city",
            "source":  hit|scraped|stale-cache|demo|none,
            "stale":   bool,          # rows may be outdated
            "count":   int,
            "elapsed_s": float,
            "message": str,           # honest text when rows is empty
          }
        }
    """
    t0 = time.time()
    slug, city = resolve_location(location)
    meta = {
        "slug": slug,
        "requested_location": location,
        "requested_city": city,
        "source": "none",
        "stale": False,
        "count": 0,
        "elapsed_s": 0.0,
        "message": "",
    }
    if not slug:
        meta["message"] = "No search location was provided."
        meta["elapsed_s"] = round(time.time() - t0, 2)
        return {"rows": [], "meta": meta}

    key = _query_key(slug, min_bedrooms, max_bedrooms, min_price, max_price)
    cached = _cache().get(key)
    budget_s = SCRAPE_BUDGET_S if budget_s is None else budget_s

    # 1) Fresh cache hit.
    if cached and not force_refresh:
        rows, fetched = cached
        age_h = (time.time() - fetched) / 3600.0
        if age_h < TTL_HOURS and rows:
            meta.update(source="hit", count=len(rows),
                        elapsed_s=round(time.time() - t0, 2))
            return {"rows": rows, "meta": meta}

    # 2) Cache miss / stale -> scrape live under budget.
    scraped = _scrape_live(slug, min_bedrooms, max_bedrooms,
                           min_price, max_price, limit, budget_s)
    if scraped is not None:
        rows = _clean(scraped, city)
        if rows:
            try:
                _cache().set(key, rows)
            except (OSError, sqlite3.Error) as exc:
                _fallback_cache(exc).set(key, rows)
            meta.update(source="scraped", count=len(rows),
                        elapsed_s=round(time.time() - t0, 2))
            return {"rows": rows, "meta": meta}

    # 3) Stale-if-error: a live scrape failed/empty but we have an older set.
    if cached:
        rows, _fetched = cached
        rows = _clean(rows, city)
        if rows:
            meta.update(source="stale-cache", stale=True, count=len(rows),
                        elapsed_s=round(time.time() - t0, 2),
                        message="Showing the most recent cached listings; a fresh "
                                "search could not be completed just now.")
            return {"rows": rows, "meta": meta}

    # 4) Offline-dev only: bundled fake CSV (default OFF).
    if ALLOW_DEMO_FALLBACK:
        demo = _demo_rows(city)
        if demo:
            meta.update(source="demo", stale=True, count=len(demo),
                        elapsed_s=round(time.time() - t0, 2),
                        message="Offline development mode: showing bundled demo data.")
            return {"rows": demo, "meta": meta}

    # 5) Nothing available -> honest empty result.
    meta.update(source="none", elapsed_s=round(time.time() - t0, 2),
                message=f"No live listings could be retrieved for '{location}' right now.")
    return {"rows": [], "meta": meta}


def _demo_rows(requested_city: str | None) -> list[dict]:
    """Offline-dev fallback only. Loads the bundled fake CSV and, when a city is
    known, keeps rows for that city so even the escape hatch stays city-plausible."""
    try:
        from .config import FAKE_CSV
        from .normalize import read_csv
        rows = read_csv(FAKE_CSV)
    except Exception as e:
        print(f"  [on_demand] demo fallback unavailable: {e}")
        return []
    if requested_city:
        matched = [r for r in rows if requested_city in str(r.get("Address", "")).lower()]
        rows = matched or []
    return rows
