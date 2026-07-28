"""
Tool: Search Nearby POIs (使用 OpenStreetMap)
查询地址周边的餐厅、超市、便利店等设施
"""

import os
import threading
import time
import math
from typing import Optional, List, Dict
from core.tool_system import Tool
from core.maps_service import overpass_request, OverpassError
from core.cache_service import get_from_cache, set_to_cache, create_cache_key
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut

DEFAULT_RADIUS = 200  # 默认搜索半径 500m

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
POI_OVERPASS_TIMEOUT_S = 30


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
                print(f"⚡ [Geocode] 缓存命中: {coords[0]:.6f}, {coords[1]:.6f}")
            else:
                print(f"⚡ [Geocode] 缓存命中: 已知失败，跳过 ({address[:40]})")
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
            print(f"🔍 [Geocode] 尝试: {variant[:60]}...")
            # country_codes: every listing in this product is in the UK, so a same-name place
            # abroad is never the answer. (It does NOT settle London vs Stratford-upon-Avon —
            # both are GB; address_variants handles that by asking for London first.)
            location = geolocator.geocode(variant, country_codes="gb")
            if location:
                print(f"✅ [Geocode] 成功! {location.latitude:.6f}, {location.longitude:.6f}")
                coords = (location.latitude, location.longitude)
                if key:
                    _geocode_store(key, coords)
                return coords
            time.sleep(0.5)  # 避免请求过快

        print(f"❌ [Geocode] 所有变体都失败了 ({attempted} 个)")
        if key:
            _geocode_store(key, None)

    except GeocoderTimedOut:
        print(f"⏱️ 地理编码超时: {address}")
    except Exception as e:
        print(f"❌ 地理编码失败: {e}")
    return None


def query_osm_pois(lat: float, lon: float, poi_type: str, radius: int = DEFAULT_RADIUS,
                   origin_lat: float = None, origin_lon: float = None,
                   timeout: Optional[float] = None) -> List[Dict]:
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
            NOTE the residual: ``overpass_request`` rotates mirrors and retries
            (``max_rounds=2``), so the worst case is still a multiple of this — bounding that
            needs a deadline parameter inside ``overpass_request`` itself.
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
            print(f"  ⚡ [OSM POI] 缓存命中 {poi_type} @ {round(lat, 4)},{round(lon, 4)} "
                  f"({len(cached['pois'])} 个)")
            return list(cached["pois"])

    # 通过共享的 Overpass 客户端查询：始终带描述性 User-Agent（否则参考服务器返回 406），
    # 并在多个公共镜像之间轮换 + 指数退避重试。全部镜像失败才抛出 OverpassError。
    req_timeout = POI_OVERPASS_TIMEOUT_S if timeout is None else max(1, int(timeout))
    try:
        data = overpass_request(query, timeout=min(POI_OVERPASS_TIMEOUT_S, req_timeout))
    except OverpassError as e:
        # API 失败（如缺 User-Agent 导致的 406、超时、限流）不能伪装成"附近没有" —— 抛出让上层报错
        print(f"❌ OSM 查询失败 ({poi_type}): {e}")
        raise RuntimeError(f"Overpass API request failed for {poi_type}: {e}") from e

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
    # 500 m is a legitimate answer) but at a much shorter TTL, because an empty 200 from a busy
    # mirror looks identical here — the same reason the amenity map refuses to trust an
    # all-zero cached cell.
    try:
        set_to_cache(cache_key, {"fetched_at": time.time(), "pois": unique_pois})
    except Exception as e:
        print(f"  [OSM POI] 结果缓存写入失败（忽略）: {e}")

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
    if any(kw in query_lower for kw in ['restaurant', 'food', 'eat', 'dining']):
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
    except Exception as e:
        print(f"  [nearest station] lookup failed: {e}")
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


def _requested_types(poi_type: str) -> List[str]:
    """Parse ``poi_type`` as a comma/space separated LIST of known types, in order, deduped.

    One call per type was the other half of the budget burn: five calls meant five geocodes
    and five deadlines where one call can cover every type under a single geocode. Returns
    [] when nothing in the string is a known type, so the caller keeps its fuzzy matching."""
    if not poi_type:
        return []
    tokens = [t.strip().lower() for t in str(poi_type).replace('|', ',').split(',')]
    out: List[str] = []
    for token in tokens:
        for word in ([token] if token in POI_TYPES else token.split()):
            if word in POI_TYPES and word not in out:
                out.append(word)
    return out


def search_nearby_pois_impl(
    address: str,
    poi_type: str = "all",
    radius: int = 300,
    user_query: str = "",
    latitude: float = None,
    longitude: float = None
) -> dict:
    """
    使用 OpenStreetMap 搜索地址周边的 POI

    NOTE: this is a PLAIN SYNC function on purpose. It performs synchronous geocoding,
    Overpass HTTP requests and pacing sleeps; registering it as sync means Tool.execute runs
    it in an executor thread so the asyncio event loop stays responsive and the fc-loop's
    per-tool asyncio.wait_for can actually fire. All requested POI types share ONE
    time.monotonic() deadline (``poi_search_budget_s()``, derived to sit strictly inside the
    fc-loop batch window): once the deadline passes, the remaining types are returned as
    skipped with an honest note rather than silently overrunning.

    Args:
        address: 要搜索的地址
        poi_type: POI 类型，可传多个（逗号分隔）(restaurant, chinese_restaurant, supermarket, convenience, cafe, pharmacy, gym, park, bus_stop, tube_station, bank, atm, all)
        radius: 搜索半径（米），默认 500m
        user_query: 用户原始查询（可选，用于智能推断 POI 类型）
        latitude, longitude: 该地址的已知坐标（可选）。传了就跳过地理编码 —— 房源缓存里
            本来就有 geo_location，用它比拿展示名去 Nominatim 反推更准也更快。
    """
    budget_s = poi_search_budget_s()
    deadline = time.monotonic() + budget_s
    try:
        # 🆕 如果有 user_query，根据用户查询智能推断 POI 类型
        if user_query and poi_type == "all":
            inferred_types = _infer_poi_types_from_query(user_query)
            if inferred_types:
                print(f"🧠 [OSM POI] 根据用户查询推断 POI 类型: {inferred_types}")
        else:
            inferred_types = None

        print(f"🗺️ [OSM POI] 搜索: {poi_type} near {address[:50]}...")

        # Coordinates the caller already owns beat anything geocoding can recover from a
        # display name. The listing cache carries geo_location per listing; a listing string
        # like "Caledonian Road, London" otherwise geocodes to the middle of a 2 km road,
        # centring the radius on a point the tenant does not live at (and, for
        # "Rugby House 6 Great Ormond Street, Islington WC1N", on nothing at all).
        exact_coords = False
        if coords_in_uk(latitude, longitude):
            coords = (float(latitude), float(longitude))
            exact_coords = True
            print(f"📍 [OSM POI] 使用调用方提供的坐标，跳过地理编码: "
                  f"{coords[0]:.6f}, {coords[1]:.6f}")
        else:
            # 地理编码（受同一个 deadline 约束，不能超出授权它的预算）
            coords = geocode_address(address, deadline=deadline)
        if not coords:
            return {
                "success": False,
                "error": f"Could not find coordinates for address: {address}",
                "address": address,
                "pois": {},
            }

        lat, lon = coords
        print(f"📍 坐标: {lat:.6f}, {lon:.6f}")

        # What every distance below is measured FROM. This tool geocodes the query string,
        # so for an AREA question ("how is Hackney?") the origin is a borough centroid and
        # "Tesco 110m" means 110 m from a cartographic centre, not from a home. Network-free
        # (string classifier) so it costs the POI hot path nothing.
        from core.place_reference import query_reference
        ref = query_reference(address)
        if exact_coords:
            # The caller passed the listing's OWN coordinates, so the hedging a geocoded
            # string earns does not apply — say so, or the model repeats "this is an area
            # centre, not the property" about a point that IS the property.
            ref = dict(ref)
            ref["precision"] = "listing_coordinates"
            ref["is_specific_address"] = True
            ref["measured_from"] = (
                f"the listing's own coordinates for {address!r} ({lat:.5f}, {lon:.5f}), "
                f"supplied with the request rather than geocoded. Distances are "
                f"straight-line (as the crow flies), not walking distance.")

        results = {}

        # 确定要查询的 POI 类型
        # 🆕 优先使用从 user_query 推断的类型
        requested = _requested_types(poi_type) if poi_type != "all" else []
        if inferred_types:
            types_to_query = inferred_types
        elif poi_type == "all":
            types_to_query = ["restaurant", "supermarket", "convenience", "cafe"]
        elif requested:
            # One call, N types, ONE geocode and ONE deadline — the whole point of accepting
            # a list instead of making the model fan out a call per type.
            types_to_query = requested
        else:
            # 尝试智能匹配
            poi_type_lower = poi_type.lower()
            if "chinese" in poi_type_lower or "asian" in poi_type_lower:
                types_to_query = ["chinese_restaurant"]
            elif "restaurant" in poi_type_lower or "food" in poi_type_lower:
                types_to_query = ["restaurant", "chinese_restaurant"]
            elif "supermarket" in poi_type_lower or "tesco" in poi_type_lower or "sainsbury" in poi_type_lower:
                types_to_query = ["supermarket"]
            elif "store" in poi_type_lower or "shop" in poi_type_lower or "convenience" in poi_type_lower:
                types_to_query = ["convenience", "supermarket"]
            else:
                types_to_query = ["restaurant", "supermarket", "convenience"]

        print(f"🔍 [OSM POI] 将查询类型: {types_to_query}")

        # 查询每种类型（传递原点坐标用于距离计算）。共享一个 monotonic 截止时间：
        # 截止后不再发起任何 per-type 请求，把剩余类型如实标为 skipped。
        skipped: List[str] = []
        for idx, ptype in enumerate(types_to_query):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # No per-type request may be issued after the deadline.
                skipped = list(types_to_query[idx:])
                print(f"  ⏱️ [OSM POI] 预算已用尽，跳过剩余类型: {skipped}")
                break
            # Clamp THIS request to what is left, so the last one issued cannot outlive the
            # deadline that let it start and hand the batch window the win.
            pois = query_osm_pois(lat, lon, ptype, radius, origin_lat=lat, origin_lon=lon,
                                  timeout=remaining)
            if pois:
                results[ptype] = pois[:5]  # 每种类型最多 5 个
                print(f"  ✅ 找到 {len(pois)} 个 {POI_TYPES[ptype]['name']}")
            # 只有还有下一个类型且仍在预算内时才 pace，避免无谓地把时间推过截止点。
            if idx < len(types_to_query) - 1 and time.monotonic() < deadline:
                time.sleep(POI_PACING_S)

        note = _skipped_note(skipped, budget_s) if skipped else None

        # A station name is the one POI the model has repeatedly supplied from memory
        # (observed: the same WC1H property reported as "Covent Garden" in one turn and
        # "Russell Square" — the correct answer — in another). Nothing in this repo ever
        # produced "Covent Garden", so it was invented in the gap left by a tool that can
        # return an empty station list. When stations were asked for, the answer now comes
        # from TfL's own index, including the case where there is none.
        station_block = None
        if "tube_station" in types_to_query:
            station_block = _resolve_nearest_station(address, lat, lon)

        if not results:
            payload = {
                "success": True,
                "address": address,
                "reference_point": ref,
                "pois": {},
            }
            if station_block:
                payload.update(station_block)
            if skipped:
                payload["message"] = (
                    "No results were gathered within the time budget. " + note)
                payload["partial"] = True
                payload["skipped_types"] = skipped
                payload["note"] = note
            else:
                payload["message"] = (
                    f"No {poi_type} found within {radius}m of {ref['measured_from']}")
            return payload

        # 格式化输出
        formatted = []
        for ptype, pois in results.items():
            for poi in pois:
                # 🆕 添加距离显示
                distance_str = poi.get('distance_display', 'N/A')
                entry = f"{poi['icon']} {poi['name']} - {distance_str}"
                if poi.get('cuisine'):
                    entry += f" ({poi['cuisine']})"
                if poi.get('brand') and poi.get('brand').lower() not in poi.get('name', '').lower():
                    entry += f" [{poi['brand']}]"
                formatted.append(entry)

        # The reference point goes into the SUMMARY STRING, not only into a sibling field:
        # a sibling field is exactly what route_source was, and nothing read it.
        summary = (f"Found {sum(len(p) for p in results.values())} places within {radius}m, "
                   f"measured in a straight line from {ref['measured_from']}\n"
                   + "\n".join(formatted))
        if station_block and station_block.get("note"):
            summary = summary + "\n" + station_block["note"]
        if note:
            summary = summary + "\n" + note

        payload = {
            "success": True,
            "address": address,
            "radius_m": radius,
            "reference_point": ref,
            "summary": summary,
            "pois": results,
        }
        if station_block:
            payload.update(station_block)
        if skipped:
            payload["partial"] = True
            payload["skipped_types"] = skipped
            payload["note"] = note
        return payload

    except Exception as e:
        print(f"❌ [OSM POI] 错误: {e}")
        return {"success": False, "error": str(e), "address": address, "pois": {}}


# 创建工具实例
search_nearby_pois_tool = Tool(
    name="search_nearby_pois",
    
    description="""Find nearby places (restaurants, supermarkets, convenience stores, cafes, pharmacies, gyms, parks, bus stops, tube stations, banks, ATMs) around an address using OpenStreetMap. Use for any "what's nearby" / "is there a ... near" question. Do NOT confuse with check_safety, which is for crime/safety questions only.
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
                'description': 'Type(s) of POI, COMMA-SEPARATED for several at once (e.g. "supermarket,convenience,tube_station"). One call covering every type you need is much faster than one call per type. Known types: restaurant, chinese_restaurant, supermarket, convenience, cafe, pharmacy, gym, park, bus_stop, tube_station, bank, atm, or "all"',
                'default': 'all'
            },
            'radius': {
                'type': 'integer',
                'description': 'Search radius in meters',
                'default': 500
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
