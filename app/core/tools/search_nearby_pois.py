"""
Tool: Search Nearby POIs (使用 OpenStreetMap)
查询地址周边的餐厅、超市、便利店等设施
"""

import os
import time
import math
from typing import Optional, List, Dict
from core.tool_system import Tool
from core.maps_service import overpass_request, OverpassError
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


def geocode_address(address: str) -> Optional[tuple]:
    """将地址转换为经纬度，带有多级回退策略"""
    try:
        geolocator = Nominatim(user_agent="uk_rent_recommender_v1", timeout=10)
        
        # 尝试不同的地址格式
        address_variants = [
            address,  # 原始地址
        ]
        
        # 🆕 如果地址包含建筑名，尝试去掉建筑名只保留街道地址
        # 例如 "Tufnell House, 144 Huddleston Road, London N7 0EG, UK" 
        # → "144 Huddleston Road, London N7 0EG, UK"
        parts = address.split(',')
        if len(parts) > 2:
            # 去掉第一部分（通常是建筑名）
            simplified = ', '.join(parts[1:]).strip()
            address_variants.append(simplified)
            
            # 只保留街道和邮编
            if len(parts) > 3:
                street_postcode = f"{parts[1].strip()}, {parts[-2].strip()}, {parts[-1].strip()}"
                address_variants.append(street_postcode)
        
        # 提取邮编作为最后手段 (UK postcode format: XX## #XX or similar)
        import re
        postcode_match = re.search(r'[A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2}', address, re.IGNORECASE)
        if postcode_match:
            postcode = postcode_match.group()
            address_variants.append(f"{postcode}, London, UK")
            address_variants.append(postcode)
        
        # 依次尝试每个变体
        for variant in address_variants:
            print(f"🔍 [Geocode] 尝试: {variant[:60]}...")
            location = geolocator.geocode(variant)
            if location:
                print(f"✅ [Geocode] 成功! {location.latitude:.6f}, {location.longitude:.6f}")
                return (location.latitude, location.longitude)
            time.sleep(0.5)  # 避免请求过快
        
        print(f"❌ [Geocode] 所有变体都失败了")
        
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


def search_nearby_pois_impl(
    address: str,
    poi_type: str = "all",
    radius: int = 300,
    user_query: str = ""
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
        poi_type: POI 类型 (restaurant, chinese_restaurant, supermarket, convenience, cafe, pharmacy, gym, park, bus_stop, tube_station, bank, atm, all)
        radius: 搜索半径（米），默认 500m
        user_query: 用户原始查询（可选，用于智能推断 POI 类型）
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

        # 地理编码
        coords = geocode_address(address)
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

        results = {}

        # 确定要查询的 POI 类型
        # 🆕 优先使用从 user_query 推断的类型
        if inferred_types:
            types_to_query = inferred_types
        elif poi_type == "all":
            types_to_query = ["restaurant", "supermarket", "convenience", "cafe"]
        elif poi_type in POI_TYPES:
            types_to_query = [poi_type]
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
                'description': 'Type of POI: restaurant, chinese_restaurant, supermarket, convenience, cafe, pharmacy, gym, park, bus_stop, tube_station, bank, atm, or "all"',
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
            }
        },
        'required': ['address']
    },
    
    max_retries=2
)
