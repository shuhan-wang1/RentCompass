"""
Tool: Search Nearby POIs (使用 OpenStreetMap)
查询地址周边的餐厅、超市、便利店等设施
"""

import os
import re
import threading
import time
import math
from typing import Optional, List, Dict
from core.tool_system import Tool
from core.maps_service import overpass_request, OverpassError, OverpassEmpty
from core.cache_service import get_from_cache, set_to_cache, create_cache_key
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut

# ONE search radius, in one place. There used to be three disagreeing defaults — this
# constant at 200 under a comment claiming 500, the impl signature at 300, and the tool
# schema at 500 (so the model's calls ran at 500 and nothing in the code said so).
#
# 300 m is deliberately tighter than the old 500: it is about a four-minute walk, which is
# what "有超市吗" actually means, and a smaller circle returns fewer elements per request.
# The ceiling stops the model asking for a 2 km circle, which would be both slow and a
# misleading answer to "nearby".
DEFAULT_RADIUS = int(os.getenv("POI_DEFAULT_RADIUS_M", "300"))
MAX_RADIUS = int(os.getenv("POI_MAX_RADIUS_M", "800"))


def clamp_radius(radius) -> int:
    """A usable metre radius: the default when missing/unparseable, capped at MAX_RADIUS."""
    try:
        r = int(float(radius))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_RADIUS
    if r <= 0:
        return DEFAULT_RADIUS
    return min(r, MAX_RADIUS)

# ─── Internal total-time budget (event-loop safe) ───────────────────
# This tool is registered as a PLAIN SYNC function, so Tool.execute offloads it to an
# executor thread (tool_system.py :279-284) and the fc-loop's asyncio.wait_for
# (agent_loop.execute_tools) is free to fire — the event loop is never blocked by the
# synchronous geocode / Overpass / sleep calls below. ALL requested POI types share ONE
# monotonic deadline derived from this budget so the total wall time stays bounded instead
# of running one up-to-30s Overpass request per type serially (the observed 43-99s tail).
#
# THE DEADLINE MUST BE STRICTLY SMALLER THAN THE WINDOW THAT KILLS IT.
# Two clocks race here and only one of them produces a usable result:
#   * FC_BATCH_TOOL_BUDGET_S (agent_loop._batch_tool_budget_s, default 20s) — the batch
#     window, which ABANDONS a straggler: the caller gets nothing at all.
#   * this budget — which stops issuing per-type requests and returns a PARTIAL result with
#     an honest "types skipped" note.
# The whole reason the second one exists is to win that race. It used to be hardcoded at
# exactly 20.0, i.e. a dead tie with the window, so the graceful path it was built for landed
# roughly half the time and depended on nothing but scheduler luck. It is now DERIVED from the
# window at call time (see ``poi_search_budget_s``), so raising FC_BATCH_TOOL_BUDGET_S raises
# this too and the ordering cannot be broken by retuning one knob.
FC_BATCH_TOOL_BUDGET_DEFAULT_S = 20.0

# The headroom between "stop issuing Overpass requests" and "the batch window abandons us".
# It is what the tool still has to do AFTER the last per-type request returns, none of which
# is free: the TfL StopPoint lookup in _resolve_nearest_station (a 12s-timeout HTTP call, only
# for a station query), the distance sort / dedupe / brand filter over every element, building
# the summary string, and then the executor-thread -> event-loop hop that carries the payload
# back — which is queued behind whatever else the batch's other tools are doing on that loop.
# Two numbers, whichever is larger, because the cost has both a fixed and a scaling part:
#   * POI_BUDGET_MARGIN_S — an absolute floor. 2.0s is the return path (serialization + the
#     loop hop under a loaded event loop), sized from the same observation that motivated
#     making these tools sync in the first place (four concurrent calls serializing to ~52s
#     against a 20s budget): the hop is not instantaneous when the loop is busy.
#   * POI_BUDGET_MARGIN_FRACTION — 15% of the window, so a LARGER window (more types queried,
#     bigger payload, more post-processing) gets proportionally more room instead of the same
#     flat 2s. At the default 20s window the fraction binds: 20 - 3.0 = 17.0s.
# POI_BUDGET_MIN_USABLE_FRACTION is the backstop for an absurdly small window (tests use 0.3s):
# never give the tool less than half the window, and never a non-positive deadline. Because it
# is a fraction < 1 the derived budget is strictly below the window for ANY positive window.
POI_BUDGET_MARGIN_S = float(os.getenv("POI_BUDGET_MARGIN_S", "2.0"))
POI_BUDGET_MARGIN_FRACTION = float(os.getenv("POI_BUDGET_MARGIN_FRACTION", "0.15"))
POI_BUDGET_MIN_USABLE_FRACTION = 0.5

# Explicit ops/test override, in seconds. ``None`` (the default) means "derive it from the
# batch window", which is what production wants. An explicit value may only LOWER the derived
# budget — a stale POI_SEARCH_BUDGET_S left at or above the window would otherwise reinstate
# exactly the tie this module now exists to avoid.
_POI_SEARCH_BUDGET_ENV = os.getenv("POI_SEARCH_BUDGET_S")
POI_SEARCH_BUDGET_S = float(_POI_SEARCH_BUDGET_ENV) if _POI_SEARCH_BUDGET_ENV else None
# Politeness pacing between consecutive Overpass mirror hits (seconds); runs inside the
# executor thread, so it never blocks the event loop.
POI_PACING_S = float(os.getenv("POI_PACING_S", "0.3"))
# Per-type Overpass result TTLs. A found set is stable for days; an EMPTY set gets minutes,
# because "nothing of this type nearby" and "a busy mirror answered 200 with nothing" are
# indistinguishable from inside one selector.
POI_RESULT_TTL_S = float(os.getenv("POI_RESULT_TTL_S", "259200"))        # 3 days
POI_EMPTY_RESULT_TTL_S = float(os.getenv("POI_EMPTY_RESULT_TTL_S", "900"))  # 15 min
# Per-request HTTP ceiling for one Overpass call when nothing else bounds it.
POI_OVERPASS_TIMEOUT_S = 3


def _batch_window_s() -> float:
    """The fc-loop batch window this tool has to finish inside, in seconds.

    Read from the loop's OWN accessor (``agent_loop._batch_tool_budget_s``) rather than from a
    second copy of the env parsing, so ops retuning FC_BATCH_TOOL_BUDGET_S moves both clocks
    together. Imported function-locally: ``core.agent_loop`` pulls in ``core.langgraph_agent``,
    which imports the tool modules, so a module-level import here would be a cycle. Falls back
    to the same env var, then to the same default, if that import is ever unavailable.
    """
    try:
        from core.agent_loop import _batch_tool_budget_s
        return float(_batch_tool_budget_s())
    except Exception:
        try:
            return float(os.getenv("FC_BATCH_TOOL_BUDGET_S", FC_BATCH_TOOL_BUDGET_DEFAULT_S))
        except (TypeError, ValueError):
            return FC_BATCH_TOOL_BUDGET_DEFAULT_S


def poi_search_budget_s() -> float:
    """This tool's own deadline — always STRICTLY below the window that would abandon it.

    Derived at call time from ``_batch_window_s()`` so the invariant survives a retune of
    FC_BATCH_TOOL_BUDGET_S. ``test_the_tool_deadline_is_strictly_inside_the_window_that_kills_it``
    pins the ordering across a sweep of window sizes, so this cannot regress to a tie.
    """
    window = _batch_window_s()
    if window <= 0:
        return 0.0
    margin = max(POI_BUDGET_MARGIN_S, window * POI_BUDGET_MARGIN_FRACTION)
    derived = max(window - margin, window * POI_BUDGET_MIN_USABLE_FRACTION)
    if POI_SEARCH_BUDGET_S is not None:
        # An override may only tighten the deadline, never push it back onto the window.
        return min(float(POI_SEARCH_BUDGET_S), derived)
    return derived

# 🆕 大品牌超市/便利店白名单（不区分大小写匹配）
# 包含各种店型：Express, Local, Metro, Extra, Superstore 等
MAJOR_SUPERMARKET_BRANDS = [
    # Tesco 系列
    'tesco', 'tesco express', 'tesco metro', 'tesco extra', 'tesco superstore',
    # Sainsbury's 系列
    'sainsbury', "sainsbury's", 'sainsburys', 'sainsbury\'s local', "sainsbury's local",
    # M&S 系列
    'm&s', 'marks & spencer', 'marks and spencer', 'm&s food', 'm&s foodhall', 'm & s',
    # 其他主要品牌
    'waitrose', 'asda', 'morrisons', 'lidl', 'aldi', 'co-op', 'coop', 'the co-operative',
    'iceland', 'farmfoods',
]

MAJOR_CONVENIENCE_BRANDS = [
    # Tesco/Sainsbury's 便利店
    'tesco express', "sainsbury's local", 'sainsburys local',
    # Waitrose 便利店形式
    'waitrose', 'waitrose local', 'Waitrose & Partners'
    # 连锁便利店
    'co-op', 'coop', 'nisa', 'spar', 'costcutter', 'londis', 'budgens', 'one stop',
    'premier', 'mace', 'best-one', 'bargain booze',
    # M&S Simply Food
    'm&s simply food', 'm&s food',
]


# ─── Coordinates we already own ──────────────────────────────────────
# UK bounding box — the same validity window amenity_map_generator.parse_geo_location
# applies. A coordinate outside it is a parse accident (swapped lat/lon, a stray "0, 0"),
# not a UK listing, and must not become a POI search centre.
_UK_LAT_RANGE = (50.0, 59.0)
_UK_LON_RANGE = (-8.0, 2.0)


def coords_in_uk(lat, lon) -> bool:
    """True when (lat, lon) are numbers inside the UK box."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return (_UK_LAT_RANGE[0] <= lat <= _UK_LAT_RANGE[1]
            and _UK_LON_RANGE[0] <= lon <= _UK_LON_RANGE[1])


def parse_geo_location(value) -> Optional[tuple]:
    """A listing's ``geo_location`` -> (lat, lon), or None.

    Accepts the two shapes the scrapers produce: the ``"lat, lon"`` string
    (scraping/normalize.py) and the ``{"lat":…, "lng"/"lon":…}`` dict. Deliberately a
    plain function here rather than a reuse of AmenityMapGenerator.parse_geo_location:
    that module imports folium at import time, and this one sits on the POI hot path."""
    if not value:
        return None
    try:
        if isinstance(value, str):
            parts = value.strip().split(',')
            if len(parts) == 2 and coords_in_uk(parts[0].strip(), parts[1].strip()):
                return (float(parts[0].strip()), float(parts[1].strip()))
        elif isinstance(value, dict):
            lat = value.get('lat', value.get('latitude'))
            lon = value.get('lng', value.get('lon', value.get('longitude')))
            if coords_in_uk(lat, lon):
                return (float(lat), float(lon))
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            if coords_in_uk(value[0], value[1]):
                return (float(value[0]), float(value[1]))
    except (TypeError, ValueError):
        return None
    return None


def _calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    使用 Haversine 公式计算两点之间的距离（米）
    """
    R = 6371000  # 地球半径（米）
    
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = math.sin(delta_phi / 2) ** 2 + \
        math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    return R * c


def _is_major_brand(name: str, brand: str, poi_type: str) -> bool:
    """
    检查是否是大品牌超市/便利店
    """
    # 只对超市和便利店应用品牌过滤
    if poi_type not in ['supermarket', 'convenience']:
        return True
    
    name_lower = name.lower() if name else ''
    brand_lower = brand.lower() if brand else ''
    combined = f"{name_lower} {brand_lower}"
    
    # 选择对应的品牌列表
    brands = MAJOR_SUPERMARKET_BRANDS if poi_type == 'supermarket' else MAJOR_CONVENIENCE_BRANDS
    
    # 检查是否匹配任何大品牌
    for major_brand in brands:
        if major_brand in combined:
            return True
    
    return False


# POI 类型映射
POI_TYPES = {
    "restaurant": {
        "query": '["amenity"="restaurant"]',
        "icon": "🍽️",
        "name": "Restaurant"
    },
    "chinese_restaurant": {
        "query": '["amenity"="restaurant"]["cuisine"~"chinese|asian",i]',
        "icon": "🥢",
        "name": "Chinese Restaurant"
    },
    "supermarket": {
        "query": '["shop"="supermarket"]',
        "icon": "🛒",
        "name": "Supermarket"
    },
    "convenience": {
        "query": '["shop"="convenience"]',
        "icon": "🏪",
        "name": "Convenience Store"
    },
    "cafe": {
        "query": '["amenity"="cafe"]',
        "icon": "☕",
        "name": "Cafe"
    },
    "pharmacy": {
        "query": '["amenity"="pharmacy"]',
        "icon": "💊",
        "name": "Pharmacy"
    },
    "school": {
        "query": '["amenity"="school"]',
        "icon": "🏫",
        "name": "School"
    },
    "gp": {
        "query": '["amenity"~"^(doctors|clinic)$"]',
        "icon": "🩺",
        "name": "Doctors / Medical Clinic"
    },
    "gym": {
        "query": '["leisure"="fitness_centre"]',
        "icon": "🏋️",
        "name": "Gym"
    },
    "park": {
        "query": '["leisure"="park"]',
        "icon": "🌳",
        "name": "Park"
    },
    "bus_stop": {
        "query": '["highway"="bus_stop"]',
        "icon": "🚌",
        "name": "Bus Stop"
    },
    "tube_station": {
        "query": '["station"="subway"]',
        "icon": "🚇",
        "name": "Tube Station"
    },
    "bank": {
        "query": '["amenity"="bank"]',
        "icon": "🏦",
        "name": "Bank"
    },
    "atm": {
        "query": '["amenity"="atm"]',
        "icon": "💳",
        "name": "ATM"
    }
}


# ─── Geocode memo ────────────────────────────────────────────────────
# One turn used to geocode the SAME address string 5+ times: the model issues one POI call
# per type, and every call started the ladder from scratch. Nominatim is a remote, paced,
# rate-limited service, so those repeats were the bulk of a 25s per-call budget — the
# supermarket/convenience queries were then cut off and the answer said "no supermarkets
# found" about a street that has three. Failures are cached too (a shorter TTL): re-walking
# a ladder that just failed costs the same seconds and fails again.
GEOCODE_CACHE_TTL_S = float(os.getenv("GEOCODE_CACHE_TTL_S", "21600"))       # 6h
GEOCODE_NEGATIVE_TTL_S = float(os.getenv("GEOCODE_NEGATIVE_TTL_S", "600"))   # 10min
_geocode_cache: Dict[str, tuple] = {}   # key -> (expires_at, coords|None)
_geocode_cache_lock = threading.Lock()


def _geocode_cache_key(address: str) -> str:
    return " ".join(str(address or "").split()).strip().lower()


def geocode_cache_clear() -> None:
    """Drop every memoised geocode (tests; ops)."""
    with _geocode_cache_lock:
        _geocode_cache.clear()


def _geocode_cached(key: str):
    """(hit, coords) — ``hit`` False when absent or expired."""
    with _geocode_cache_lock:
        entry = _geocode_cache.get(key)
        if entry is None:
            return False, None
        expires_at, coords = entry
        if time.monotonic() >= expires_at:
            _geocode_cache.pop(key, None)
            return False, None
        return True, coords


def _geocode_store(key: str, coords: Optional[tuple]) -> None:
    ttl = GEOCODE_CACHE_TTL_S if coords else GEOCODE_NEGATIVE_TTL_S
    with _geocode_cache_lock:
        _geocode_cache[key] = (time.monotonic() + ttl, coords)


def _london_area_name(text: str) -> Optional[str]:
    """The canonical spelling when ``text`` is one of the curated LONDON area names, else
    None. Reads search_properties.LONDON_AREAS — one area table, one place. Lazy import: that
    module pulls heavier dependencies and this one sits on the POI hot path."""
    import re

    key = re.sub(r"[’']", "", " ".join(str(text or "").split()).lower())
    if not key:
        return None
    try:
        from core.tools.search_properties import LONDON_AREAS
    except Exception:
        return None
    return LONDON_AREAS.get(key)


def address_variants(address: str) -> List[str]:
    """The geocode ladder for one address string, most specific first, deduped.

    The listing strings this receives are OnTheMarket DISPLAY names, not postal addresses:
    ``"Rugby House 6 Great Ormond Street, Islington WC1N"`` has the house number inside the
    building name, one comma, and a borough that contradicts its own postcode. The previous
    ladder produced exactly ONE variant for it — the drop-the-building-name step required
    more than two comma parts, and the postcode step required a FULL postcode while this
    string carries only the outward code — so a single failed lookup was the whole attempt
    and the tool returned "could not find coordinates".

    Steps: the string as given; without its leading building-name part; from the first house
    number onward; the street words alone; a full postcode; the outward code + London.
    """
    import re

    raw = " ".join(str(address or "").split()).strip()
    if not raw:
        return []
    variants = []
    # A bare London area name goes to Nominatim as "<area>, London, UK" FIRST. "Stratford"
    # alone resolved to Stratford-upon-Avon (52.19, -1.71 — Warwickshire), and the POI answer
    # that followed described a town 150 km from the listing. Only names in the curated London
    # half of the area table get this: "Manchester" must stay Manchester.
    if ',' not in raw:
        canon = _london_area_name(raw)
        if canon:
            variants.append(f"{canon}, London, UK")
    variants.append(raw)
    parts = [p.strip() for p in raw.split(',') if p.strip()]

    # Drop the leading building-name part. >= 2 parts, not > 2: a one-comma display name is
    # the common shape and was precisely the one this step used to skip.
    if len(parts) >= 2:
        variants.append(', '.join(parts[1:]))
        if len(parts) > 3:
            variants.append(f"{parts[1]}, {parts[-2]}, {parts[-1]}")

    # The first part often reads "<building name> <number> <street>". Nominatim resolves
    # "6 Great Ormond Street, London" and "Great Ormond Street, London" but not the whole
    # blob, so offer both, keeping any trailing parts (postcode) for context.
    if parts:
        head, tail = parts[0], parts[1:]
        num = re.search(r'\b(\d+[A-Za-z]?(?:\s*-\s*\d+[A-Za-z]?)?)\s+(\S.*)$', head)
        if num:
            from_number = f"{num.group(1)} {num.group(2)}".strip()
            street_only = num.group(2).strip()
            for candidate in (from_number, street_only):
                variants.append(', '.join([candidate, *tail]) if tail else candidate)
                if 'london' not in raw.lower():
                    variants.append(f"{candidate}, London, UK")

    # Postcodes last: a full one is a precise fallback, an outward code is a district centre
    # (worse, but far better than nothing — and the model is told what it measured from).
    full_pc = re.search(r'\b[A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2}\b', raw, re.IGNORECASE)
    if full_pc:
        variants.append(f"{full_pc.group().strip()}, London, UK")
        variants.append(full_pc.group().strip())
    else:
        outward = re.search(r'\b([A-Z]{1,2}\d{1,2}[A-Z]?)\b(?!\s*\d[A-Z]{2})', raw)
        if outward:
            variants.append(f"{outward.group(1)}, London, UK")

    seen, ordered = set(), []
    for v in variants:
        v = " ".join(v.split()).strip(' ,')
        low = v.lower()
        if v and low not in seen and not _is_too_generic(v):
            seen.add(low)
            ordered.append(v)
    return ordered


# Words that place nothing on their own. Dropping the first comma part of
# "Caledonian Road, London" leaves "London", and geocoding that SUCCEEDS — with the centre
# of the city. A variant that silently relocates the search by ten kilometres is worse than
# one more failure, so the ladder never asks for one.
_GENERIC_PLACE_WORDS = {"london", "uk", "u.k.", "england", "greater", "gb", "united",
                        "kingdom", "city", "centre", "center"}


def _is_too_generic(variant: str) -> bool:
    """True when ``variant`` carries no locating token (no street word, number or postcode)."""
    import re

    if re.fullmatch(r'[A-Z]{1,2}\d{1,2}[A-Z]?(\s*\d[A-Z]{2})?', variant.strip(),
                    re.IGNORECASE):
        return False        # a postcode (outward or full) locates something
    words = [w for w in re.split(r'[\s,]+', variant.lower()) if w]
    return not [w for w in words if w not in _GENERIC_PLACE_WORDS]


def geocode_address(address: str, deadline: Optional[float] = None) -> Optional[tuple]:
    """将地址转换为经纬度，带有多级回退策略 + 进程内缓存。

    ``deadline`` is a ``time.monotonic()`` instant: no further variant is attempted once it
    passes, so the ladder cannot outlive the POI budget that authorised it (each attempt is
    a remote call plus pacing — five of them used to be able to eat a whole 25s window)."""
    key = _geocode_cache_key(address)
    if key:
        hit, coords = _geocode_cached(key)
        if hit:
            if coords:
                print("⚡ [Geocode] 缓存命中 outcome=coordinates")
            else:
                print(f"⚡ [Geocode] 缓存命中 outcome=known_failure address_chars={len(address)}")
            return coords
    try:
        geolocator = Nominatim(user_agent="uk_rent_recommender_v1", timeout=10)
        variants = address_variants(address)
        attempted = 0

        for variant in variants:
            if deadline is not None and time.monotonic() >= deadline:
                print(f"⏱️ [Geocode] 预算已用尽，剩余变体未尝试 ({len(variants) - attempted})")
                # Not a cacheable verdict: the ladder was cut short, not exhausted.
                return None
            attempted += 1
            print(f"🔍 [Geocode] 尝试 variant_chars={len(variant)}")
            # country_codes: every listing in this product is in the UK, so a same-name place
            # abroad is never the answer. (It does NOT settle London vs Stratford-upon-Avon —
            # both are GB; address_variants handles that by asking for London first.)
            remaining = deadline - time.monotonic() if deadline is not None else 20.0
            if remaining <= 0:
                return None
            location = geolocator.geocode(variant, country_codes="gb",
                                          timeout=min(10.0, remaining / 2))
            if location:
                print("✅ [Geocode] 成功 coordinates_resolved=True")
                coords = (location.latitude, location.longitude)
                if key:
                    _geocode_store(key, coords)
                return coords
            if deadline is not None and deadline - time.monotonic() <= 0.5:
                return None
            time.sleep(0.5)  # 避免请求过快

        print(f"❌ [Geocode] 所有变体都失败了 ({attempted} 个)")
        if key:
            _geocode_store(key, None)

    except GeocoderTimedOut:
        print(f"⏱️ 地理编码超时 address_chars={len(address)}")
    except Exception as exc:
        print(f"❌ 地理编码失败 exception_type={type(exc).__name__}")
    return None


def query_osm_pois(lat: float, lon: float, poi_type: str, radius: int = DEFAULT_RADIUS,
                   origin_lat: float = None, origin_lon: float = None,
                   timeout: Optional[float] = None,
                   deadline: Optional[float] = None) -> List[Dict]:
    """从 OpenStreetMap 查询 POI

    Args:
        lat, lon: 搜索中心坐标
        poi_type: POI 类型
        radius: 搜索半径（米）
        origin_lat, origin_lon: 原点坐标（用于计算距离，如果不提供则使用搜索中心）
        timeout: per-request HTTP ceiling in seconds. The shared deadline is checked BEFORE a
            request is issued, so without this the LAST request issued could still run its full
            30s past the deadline and hand the batch window the win anyway. Passing the
            remaining budget keeps one request from outliving the budget that authorised it.
            The same deadline also bounds mirror failover and pacing inside
            ``overpass_request``; each category gets a share of the tool budget.
    """
    if poi_type not in POI_TYPES:
        return []
    
    # 使用搜索中心作为距离计算原点（如果没有单独提供）
    if origin_lat is None:
        origin_lat = lat
    if origin_lon is None:
        origin_lon = lon
    
    query_filter = POI_TYPES[poi_type]["query"]
    
    query = f"""
    [out:json][timeout:25];
    (
        node{query_filter}(around:{radius},{lat},{lon});
        way{query_filter}(around:{radius},{lat},{lon});
        relation{query_filter}(around:{radius},{lat},{lon});
    );
    out center body;
    """
    
    # Per-type result cache, keyed by the rounded centre + radius (the map generator caches
    # its own batched query the same way). Two things make this load-bearing rather than a
    # nicety: (1) when the harness kills the call at its per-call cap, the types that DID
    # complete are already paid for — the retry gets them for free instead of the whole turn
    # ending in "查询周边设施时超时了" with results that had been fetched and discarded;
    # (2) the model re-asks the same address with a narrower type list in a later batch, which
    # the in-batch merge cannot see.
    cache_key = create_cache_key("poi_type_v1", round(lat, 4), round(lon, 4),
                                 poi_type, int(radius))
    cached = get_from_cache(cache_key)
    if isinstance(cached, dict) and cached.get("pois") is not None:
        age = time.time() - float(cached.get("fetched_at") or 0)
        ttl = POI_RESULT_TTL_S if cached["pois"] else POI_EMPTY_RESULT_TTL_S
        if age < ttl:
            print(
                f"  ⚡ [OSM POI] 缓存命中 type={poi_type} "
                f"result_count={len(cached['pois'])}"
            )
            return list(cached["pois"])

    # 通过共享的 Overpass 客户端查询：始终带描述性 User-Agent（否则参考服务器返回 406），
    # 并在多个公共镜像之间轮换 + 指数退避重试。全部镜像失败才抛出 OverpassError。
    #
    # expect_nonempty=True: an empty HTTP 200 must not be read as "none nearby". A
    # rate-limited mirror answers exactly that — measured on osm.ch, which returned
    # elements=0 for cafés within 1 km of Leicester Square — and with the old default this
    # tool reported "附近没有超市" as fact about a street with three of them. So an empty body
    # sidelines that mirror and the next one is asked instead.
    #
    # A genuinely empty cell (no gym within 300 m) is real, though, and must stay reportable:
    # when EVERY mirror says empty, one confirming request settles it. Zero is only ever
    # stated after a mirror we have no reason to distrust says zero.
    req_timeout = POI_OVERPASS_TIMEOUT_S if timeout is None else max(0.001, float(timeout))
    eff_timeout = min(POI_OVERPASS_TIMEOUT_S, req_timeout)
    unconfirmed_empty = False
    try:
        data = overpass_request(query, timeout=eff_timeout, max_rounds=1, expect_nonempty=True,
                               deadline=deadline)
    except OverpassEmpty as e:
        # Every reachable mirror answered a clean empty 200. Report zero — no gym within
        # 300 m is a real answer — but do not cache it: under a rate-limit an empty body is
        # what a mirror hands back instead of a 429.
        data = e.payload
        unconfirmed_empty = True
        print(f"  ⚠️ [OSM POI] {poi_type}: 所有可达镜像都返回空结果 —— "
              f"按'确实没有'处理，但不写入缓存")
    except OverpassError as exc:
        # API 失败不能伪装成"附近没有"；异常原文可能包含镜像 URL。
        print(
            f"❌ OSM 查询失败 type={poi_type} "
            f"exception_type={type(exc).__name__}"
        )
        raise

    pois = []
    for element in data.get("elements", []):
        tags = element.get("tags", {})
        name = tags.get("name", "Unnamed")
        brand = tags.get("brand", "")

        # 跳过无名的
        if name == "Unnamed":
            continue

        # 🆕 对超市/便利店应用大品牌过滤
        if poi_type in ['supermarket', 'convenience']:
            if not _is_major_brand(name, brand, poi_type):
                continue

        # 获取POI坐标
        poi_lat = element.get("lat") or element.get("center", {}).get("lat")
        poi_lon = element.get("lon") or element.get("center", {}).get("lon")

        # 🆕 计算距离
        distance_m = None
        if poi_lat and poi_lon:
            distance_m = _calculate_distance(origin_lat, origin_lon, poi_lat, poi_lon)

        poi = {
            "osm_type": element.get('type'),
            "osm_id": element.get('id'),
            "source": "OpenStreetMap Overpass",
            "name": name,
            "type": POI_TYPES[poi_type]["name"],
            "icon": POI_TYPES[poi_type]["icon"],
            "lat": poi_lat,
            "lon": poi_lon,
            "distance_m": round(distance_m) if distance_m else None,  # 🆕 距离（米）
            "distance_display": _format_distance(distance_m) if distance_m else "N/A",  # 🆕 格式化距离
            "cuisine": tags.get("cuisine"),
            "brand": brand,
            "opening_hours": tags.get("opening_hours"),
            "school_level": tags.get('school:level') or tags.get('isced:level'),
            "healthcare": tags.get('healthcare'),
        }
        pois.append(poi)

    # 🆕 按距离排序
    pois.sort(key=lambda x: x.get('distance_m') or float('inf'))

    # 🆕 去重：同名店铺只保留最近的一个
    seen_names = set()
    unique_pois = []
    for poi in pois:
        # 使用名称的小写形式作为去重键
        name_key = poi.get('name', '').lower().strip()
        if name_key not in seen_names:
            seen_names.add(name_key)
            unique_pois.append(poi)

    # Cached only on a real answer from Overpass. An empty list is cached too (no gym within
    # 300 m is a legitimate answer) but at a much shorter TTL, because an empty 200 from a busy
    # mirror looks identical here — the same reason the amenity map refuses to trust an
    # all-zero cached cell. An empty result that NO mirror could confirm is not cached at all:
    # storing it would let one bad minute answer for the whole TTL.
    if not (unconfirmed_empty and not unique_pois):
        try:
            set_to_cache(cache_key, {"fetched_at": time.time(), "pois": unique_pois})
        except Exception as exc:
            print(
                "  [OSM POI] 结果缓存写入失败（忽略） "
                f"exception_type={type(exc).__name__}"
            )

    return unique_pois


def _format_distance(distance_m: float) -> str:
    """格式化距离显示"""
    if distance_m is None:
        return "N/A"
    if distance_m < 100:
        return f"{int(distance_m)}m"
    elif distance_m < 1000:
        return f"{int(round(distance_m, -1))}m"  # 四舍五入到10m
    else:
        return f"{distance_m/1000:.1f}km"


def _infer_poi_types_from_query(user_query: str) -> List[str]:
    """
    根据用户查询智能推断需要搜索的 POI 类型
    
    Args:
        user_query: 用户原始查询
        
    Returns:
        推断出的 POI 类型列表
    """
    query_lower = user_query.lower()
    inferred = []
    
    # 中国/亚洲餐厅
    if any(kw in user_query for kw in ['中国', '中餐', '中式', '亚洲', '火锅', '饺子', '面馆']):
        inferred.append('chinese_restaurant')
    if any(kw in query_lower for kw in ['chinese', 'asian', 'noodle', 'dim sum']):
        inferred.append('chinese_restaurant')
    
    # 普通餐厅
    if any(kw in user_query for kw in ['餐厅', '餐馆', '饭店', '吃饭', '吃的']):
        inferred.append('restaurant')
    if any(kw in query_lower for kw in ['restaurant', 'food', 'dining']) or re.search(r'\beat\b', query_lower):
        inferred.append('restaurant')
    
    # 超市
    if any(kw in user_query for kw in ['超市', '购物', '买菜', '杂货']):
        inferred.append('supermarket')
    if any(kw in query_lower for kw in ['supermarket', 'grocery', 'tesco', 'sainsbury', 'asda', 'lidl', 'aldi', 'waitrose']):
        inferred.append('supermarket')
    
    # 便利店
    if any(kw in user_query for kw in ['便利店', '便利']):
        inferred.append('convenience')
    if any(kw in query_lower for kw in ['convenience', 'corner shop']):
        inferred.append('convenience')
    
    # 咖啡厅
    if any(kw in user_query for kw in ['咖啡', '咖啡厅', '星巴克']):
        inferred.append('cafe')
    if any(kw in query_lower for kw in ['cafe', 'coffee', 'starbucks', 'costa']):
        inferred.append('cafe')
    
    # 药店
    if any(kw in user_query for kw in ['药店', '药房', '药']):
        inferred.append('pharmacy')
    if any(kw in query_lower for kw in ['pharmacy', 'chemist', 'boots']):
        inferred.append('pharmacy')
    
    # 健身房
    if re.search(r'\b(?:schools?|primary schools?|secondary schools?)\b', query_lower) or any(
            word in user_query for word in ('学校', '小学', '中学')):
        inferred.append('school')
    if re.search(r'\b(?:gp|gps|doctors?|medical cent(?:re|er)|clinic)\b', query_lower) or any(
            word in user_query for word in ('家庭医生', '全科医生', '医疗中心', '诊所')):
        inferred.append('gp')

    # 健身房
    if any(kw in user_query for kw in ['健身', '健身房', '运动']):
        inferred.append('gym')
    if any(kw in query_lower for kw in ['gym', 'fitness', 'workout']):
        inferred.append('gym')
    
    # 公园
    if any(kw in user_query for kw in ['公园', '绿地', '散步']):
        inferred.append('park')
    if any(kw in query_lower for kw in ['park', 'garden', 'green']):
        inferred.append('park')
    
    # 交通
    if any(kw in user_query for kw in ['地铁', '地铁站', '交通']):
        inferred.append('tube_station')
    if any(kw in query_lower for kw in ['tube', 'underground', 'metro', 'subway']):
        inferred.append('tube_station')
    if any(kw in user_query for kw in ['公交', '巴士', '公交站']):
        inferred.append('bus_stop')
    if any(kw in query_lower for kw in ['bus', 'bus stop']):
        inferred.append('bus_stop')
    
    # 银行/ATM
    if any(kw in user_query for kw in ['银行', '取钱', '取款']):
        inferred.extend(['bank', 'atm'])
    if any(kw in query_lower for kw in ['bank', 'atm', 'cash']):
        inferred.extend(['bank', 'atm'])
    
    # 便利性（综合查询）
    if any(kw in user_query for kw in ['便利', '便利性', '方便', '附近有什么', '周边']):
        # 综合查询，返回常用类型
        if not inferred:
            inferred = ['supermarket', 'convenience', 'restaurant', 'cafe']
    
    # 去重
    return list(dict.fromkeys(inferred))


def _resolve_nearest_station(address: str, lat: float, lon: float) -> dict:
    """The grounded ``nearest_station`` block for a station query.

    Uses TfL's StopPoint index (authoritative for London tube/rail, and it returns the
    measured distance) rather than the Overpass ``["station"="subway"]`` result, which drops
    unnamed elements and misses anything outside the caller-chosen radius — the model was
    then free to fill the silence. Never raises: a lookup failure degrades to an explicit
    "not established", which is a usable fact, unlike an absent key.
    """
    try:
        from core.place_reference import nearest_stations, STATION_SOURCE
        found = nearest_stations(lat, lon)
    except Exception as exc:
        print(
            "  [nearest station] lookup failed "
            f"exception_type={type(exc).__name__}"
        )
        found = None

    if found is None:
        return {"nearest_station": None, "other_stations_nearby": [],
                "note": (f"The nearest station to {address!r} could NOT be checked. Do not "
                         f"name a station.")}
    if not found:
        return {"nearest_station": None, "other_stations_nearby": [],
                "note": (f"TfL lists no tube/rail station near {address!r}. Say there is none "
                         f"nearby rather than naming one.")}
    top = found[0]
    return {
        "nearest_station": top,
        "other_stations_nearby": found[1:],
        "note": (f"Nearest station per {STATION_SOURCE}: {top['name']} "
                 f"({top['distance_m']}m straight-line). Name only stations from this result."),
    }


def _skipped_note(skipped: List[str], budget_s: float) -> str:
    """Honest partial-result note. This tool has no reply_language param, so the note is
    neutral English plus a short zh hint (mirrors how other tool notes stay bilingual).

    ``budget_s`` is passed in rather than read from the module: the budget is derived per call
    from the batch window now, so a note that quoted a module constant could name a different
    number from the deadline that actually fired.
    """
    names = [POI_TYPES[t]["name"] for t in skipped if t in POI_TYPES] or list(skipped)
    joined = ", ".join(names)
    return (f"Note: the {budget_s:.0f}s search budget was reached, so these were "
            f"not checked: {joined}. Returning partial results. "
            f"（部分结果：已达搜索时间上限，未查询：{joined}。）")


_POI_ALIASES = {
    'schools': 'school', 'primary school': 'school', 'primary schools': 'school',
    'secondary school': 'school', 'secondary schools': 'school',
    'doctor': 'gp', 'doctors': 'gp', 'gps': 'gp', 'medical centre': 'gp',
    'medical center': 'gp', 'medical_centre': 'gp', 'medical_center': 'gp',
    'clinic': 'gp', 'clinics': 'gp', 'general practitioner': 'gp',
}


def _type_plan(poi_type: str) -> tuple[List[str], List[str]]:
    """Keep unsupported explicit categories visible instead of replacing them with food."""
    known, unsupported = [], []
    for token in re.split(r'[,|;]', str(poi_type or '')):
        token = token.strip().lower()
        if not token:
            continue
        candidate = _POI_ALIASES.get(token, token)
        if candidate in POI_TYPES:
            matches = [candidate]
        else:
            # Preserve the legacy natural-language aliases (e.g. food or Tesco).
            matches = _infer_poi_types_from_query(token)
            if not matches:
                matches = [_POI_ALIASES.get(word, word) for word in token.split()
                           if _POI_ALIASES.get(word, word) in POI_TYPES]
        known.extend(t for t in matches if t not in known)
        if not matches and token not in unsupported:
            unsupported.append(token)
    return known, unsupported


def _requested_types(poi_type: str) -> List[str]:
    # Kept as the legacy strict-token parser for callers that use [] to request
    # their own fuzzy matching. The execution planner below handles aliases.
    out = []
    for token in re.split(r'[,|;]', str(poi_type or '').lower()):
        token = token.strip()
        candidate = _POI_ALIASES.get(token, token)
        for word in ([candidate] if candidate in POI_TYPES else token.split()):
            word = _POI_ALIASES.get(word, word)
            if word in POI_TYPES and word not in out:
                out.append(word)
    return out


def _user_radius_m(query: str):
    match = re.search(r'(\d+(?:\.\d+)?)\s*(kilomet(?:re|er)s?|km|公里|千米|met(?:re|er)s?|米|m)(?![a-z])',
                      query or '', re.I)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    return int(value * (1000 if unit.startswith(('k', '公里', '千米')) else 1))


def search_nearby_pois_impl(
    address: str,
    poi_type: str = "all",
    radius: int = DEFAULT_RADIUS,
    user_query: str = "",
    latitude: float = None,
    longitude: float = None,
) -> dict:
    """One bounded lookup with explicit execution, failure and spatial evidence.

    A missing category is never an empty answer. Explicit poi_type controls what
    runs; categories mentioned only in user_query are reported as unqueried.
    Schools/clinics are OSM records, not proof of admissions, GP registration or
    wheelchair access. Existing per-type cache/retries and shared deadline apply.
    """
    budget_s = poi_search_budget_s()
    started_at = time.monotonic()
    deadline = started_at + budget_s
    try:
        requested_radius = int(float(radius))
    except (TypeError, ValueError, OverflowError):
        requested_radius = None
    radius = clamp_radius(radius)
    inferred = _infer_poi_types_from_query(user_query or '')
    if str(poi_type).lower() == 'all':
        types = inferred or ['restaurant', 'supermarket', 'convenience', 'cafe']
        unsupported = []
        unqueried = []
    else:
        types, unsupported = _type_plan(poi_type)
        unqueried = [t for t in inferred if t not in types]
    desired_radius = _user_radius_m(user_query)
    executed, completed, empty, failed, skipped = [], [], [], [], []
    category_status = {t: {'status': 'not_executed'} for t in types}
    category_status.update({t: {'status': 'unsupported'} for t in unsupported})
    category_status.update({t: {'status': 'not_requested'} for t in unqueried})
    payload = {
        'success': False, 'address': address, 'pois': {},
        'source': 'OpenStreetMap Overpass',
        'requested_radius_m': requested_radius, 'radius_m': radius,
        'user_requested_radius_m': desired_radius,
        'radius_clamped': requested_radius is not None and requested_radius != radius,
        'requested_types': types, 'executed_types': executed,
        'completed_types': completed, 'empty_types': empty, 'failed_types': failed,
        'skipped_types': skipped, 'unsupported_types': unsupported,
        'unqueried_types': unqueried, 'category_status': category_status,
        'reference_point': None,
        'budget_seconds': budget_s,
    }
    notes = []
    if requested_radius is not None and requested_radius != radius:
        notes.append(f'Requested radius {requested_radius}m; actual search radius {radius}m '
                     f'(configured maximum {MAX_RADIUS}m).')
    if desired_radius is not None and desired_radius != radius:
        notes.append(f'The user asked for {desired_radius}m; only {radius}m was searched.')
    if unsupported:
        notes.append('Unsupported categories, NOT queried: ' + ', '.join(unsupported) + '.')
    if unqueried:
        notes.append('Mentioned categories NOT requested or queried: ' + ', '.join(unqueried) + '.')
    if not types:
        payload.update(error='No supported POI category was requested.',
                       search_status='unsupported', retryable=False, note=' '.join(notes))
        return payload
    try:
        exact = coords_in_uk(latitude, longitude)
        coords = ((float(latitude), float(longitude)) if exact else
                  geocode_address(address, deadline=min(deadline, time.monotonic() + budget_s / 3)))
        if not coords:
            skipped.extend(types)
            for pending in types:
                category_status[pending] = {'status': 'skipped', 'reason': 'geocoding_unavailable',
                                            'radius_m': radius}
            payload.update(error='Could not find coordinates for the requested address.',
                           search_status='not_executed', retryable=True, partial=True,
                           service_status='unavailable', unknown_types=list(types),
                           elapsed_seconds=round(time.monotonic() - started_at, 3))
            return payload
        lat, lon = coords
        from core.place_reference import query_reference
        ref = dict(query_reference(address))
        ref.update(lat=lat, lon=lon,
                   coordinate_source='caller_supplied' if exact else 'address_geocoding')
        if exact:
            ref.update(precision='listing_coordinates', is_specific_address=True,
                       coordinate_identity_verified=False,
                       measured_from=(f'the supplied coordinates for {address!r}, supplied with the request '
                                      'rather than geocoded. Their property identity is not independently '
                                      'verified. Distances are straight-line, not walking distance.'))
        else:
            # The ladder can fall back to a street/postcode; string specificity
            # does not establish the precision of the geocoding hit it actually used.
            ref.update(query_precision_hint=ref.get('precision'),
                       precision='area' if ref.get('precision') == 'area' else 'unknown',
                       is_specific_address=False)
        payload['reference_point'] = ref
        results = payload['pois']
        for index, ptype in enumerate(types):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                skipped.extend(types[index:])
                for pending in types[index:]:
                    category_status[pending] = {'status': 'skipped', 'reason': 'time_budget',
                                                'radius_m': radius}
                break
            executed.append(ptype)
            try:
                # Reserve a fair share for every remaining category. A failing
                # school lookup must leave GP a chance, and already completed
                # categories are returned even if a later provider fails.
                category_deadline = min(deadline, time.monotonic() + remaining / (len(types) - index))
                pois = query_osm_pois(lat, lon, ptype, radius, origin_lat=lat, origin_lon=lon,
                                      timeout=remaining, deadline=category_deadline)
                completed.append(ptype)
                category_status[ptype] = {'status': 'found' if pois else 'empty',
                                          'result_count': min(len(pois), 5),
                                          'radius_m': radius, 'source': 'OpenStreetMap Overpass'}
                if pois:
                    results[ptype] = pois[:5]
                else:
                    empty.append(ptype)
            except Exception as exc:
                failed.append(ptype)
                category_status[ptype] = {'status': 'failed', 'error': 'POI lookup failed',
                                          'error_type': type(exc).__name__, 'retryable': True,
                                          'service_status': getattr(exc, 'service_status', 'unavailable'),
                                          'attempts': getattr(exc, 'attempts', None),
                                          'radius_m': radius, 'source': 'OpenStreetMap Overpass'}
            if index < len(types) - 1 and time.monotonic() < deadline:
                time.sleep(min(POI_PACING_S, max(0.0, deadline - time.monotonic())))
        if skipped:
            notes.append(_skipped_note(skipped, budget_s))
        if failed:
            notes.append('Lookup FAILED (not an empty result): ' + ', '.join(failed) + '.')
        if 'school' in types or 'gp' in types:
            notes.append('School/clinic map records do not verify primary-school phase, '
                         'school places, GP registration, catchment or wheelchair access.')
        # Nearest-station evidence is a separate TfL lookup with its own radius.
        if 'tube_station' in completed:
            if deadline - time.monotonic() >= 12:
                station = _resolve_nearest_station(address, lat, lon)
                station['nearest_station_radius_m'] = 1500
                payload.update(station)
                if station.get('note'):
                    notes.append(station['note'])
            else:
                payload.update(nearest_station=None, other_stations_nearby=[])
                notes.append('Nearest-station lookup was NOT executed: insufficient time budget.')
        partial = bool(failed or skipped or unsupported or unqueried or
                       (requested_radius is not None and requested_radius != radius) or
                       (desired_radius is not None and desired_radius != radius))
        payload.update(success=bool(completed), partial=partial,
                       search_status='partial' if partial and completed else
                                     'complete' if completed else 'failed',
                       service_status='degraded' if completed and (failed or skipped) else
                                      'unavailable' if failed or skipped else 'healthy',
                       unknown_types=list(dict.fromkeys(failed + skipped + unsupported + unqueried)),
                       elapsed_seconds=round(time.monotonic() - started_at, 3))
        if not completed:
            payload['error'] = 'Nearby POI lookup failed'
            payload['failure_note'] = 'No requested category completed; do not report this as no nearby places.'
            payload['retryable'] = bool(failed or skipped)
        formatted = []
        for pois in results.values():
            for place in pois:
                formatted.append(f"{place.get('icon', '')} {place.get('name', 'Unnamed')} - "
                                 f"{place.get('distance_display', 'N/A')}")
        count = sum(len(places) for places in results.values())
        summary = (f'Found {count} places in completed categories within the actual {radius}m '
                   f'straight-line search radius, measured from {ref["measured_from"]}\n' +
                   '\n'.join(formatted))
        if not count:
            payload['message'] = ('No places returned for completed categories: ' +
                                  ', '.join(completed) + f' within {radius}m of {ref["measured_from"]}. '
                                  'Unexecuted or failed categories are unknown.'
                                  if completed else payload['error'])
        if notes:
            summary += '\n' + ' '.join(notes)
            payload['note'] = ' '.join(notes)
        payload['summary'] = summary
        return payload
    except Exception as exc:
        print(f'Nearby POI lookup failed; exception_type={type(exc).__name__}')
        payload.update(error='Nearby POI lookup failed', search_status='failed', retryable=True)
        return payload


# 创建工具实例
search_nearby_pois_tool = Tool(
    name="search_nearby_pois",
    
    description="""Find nearby places (restaurants, supermarkets, convenience stores, cafes, pharmacies, schools, GP/doctors/medical clinics, gyms, parks, bus stops, tube stations, banks, ATMs) around an address using OpenStreetMap. Use for any "what's nearby" / "is there a ... near" question. Do NOT confuse with check_safety, which is for crime/safety questions only. requested_types/executed_types/completed_types/empty_types/failed_types/skipped_types/unsupported_types/unqueried_types distinguish an empty completed lookup from something NOT checked. Never claim no school/GP from a missing category. Quote the actual radius_m and requested_radius_m/user_requested_radius_m when they differ. School and clinic records do not verify admissions, registrations, primary-school phase or wheelchair access.
DISTANCES HAVE A REFERENCE POINT: the result's `reference_point.measured_from` says what the metres are measured from. For an AREA query it is the area's geocoded centre, not a home — quote the distance together with that reference, never as "X metres from the property". Ask with poi_type=tube_station to get a `nearest_station` field resolved from TfL; if it is null the nearest station is NOT established and you must not name one.
搜索某地址附近的设施（餐厅/超市/交通站点等）。""",
    
    func=search_nearby_pois_impl,
    
    parameters={
        'type': 'object',
        'properties': {
            'address': {
                'type': 'string',
                'description': 'The address to search around'
            },
            'poi_type': {
                'type': 'string',
                'description': 'Type(s) of POI, COMMA-SEPARATED for several at once (e.g. "school,gp,supermarket"). One call covering every type you need is much faster than one call per type. Known types: restaurant, chinese_restaurant, supermarket, convenience, cafe, pharmacy, school, gp (doctors/medical clinics), gym, park, bus_stop, tube_station, bank, atm, or "all". Unsupported categories are returned explicitly, never replaced with unrelated categories.',
                'default': 'all'
            },
            'radius': {
                'type': 'integer',
                'description': f'Search radius in meters (default {DEFAULT_RADIUS}, max {MAX_RADIUS}). Keep it tight: "nearby" means walking distance, and a wider circle is slower for no better answer.',
                'default': DEFAULT_RADIUS
            },
            'user_query': {
                'type': 'string',
                'description': 'Original user query for smart POI type inference',
                'default': ''
            },
            'latitude': {
                'type': 'number',
                'description': "The listing's own latitude, if you have it (get_property_details returns geo_location). Passing it skips geocoding, so the radius is centred on the property itself instead of a street or district centre."
            },
            'longitude': {
                'type': 'number',
                'description': "The listing's own longitude (see latitude)."
            }
        },
        'required': ['address']
    },
    
    max_retries=2
)
