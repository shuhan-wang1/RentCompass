# maps_service.py - free providers (no Google Maps required):
#   geocoding   -> OSM Nominatim + Postcodes.io (no key)
#   travel time -> TfL Journey Planner (London public transport), haversine fallback
#   POIs        -> OpenStreetMap Overpass (no key)

import os
import re
import requests
from datetime import datetime
import pandas as pd
from collections import Counter
import asyncio
import math
from .cache_service import get_from_cache, set_to_cache, create_cache_key

# Optional free TfL app key (register at api-portal.tfl.gov.uk) to raise rate limits;
# the Journey Planner also works without a key at low volume.
TFL_APP_KEY = os.getenv("TFL_APP_KEY", "")
# Nominatim / OSM require a descriptive User-Agent.
_OSM_HEADERS = {"User-Agent": "uk-rent-recommendation/1.0 (student-housing demo)"}
_UK_POSTCODE_RE = re.compile(r"([A-Z]{1,2}[0-9]{1,2}[A-Z]?[ ]?[0-9][A-Z]{2})", re.IGNORECASE)

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
    Returns {'lat','lng','postcode'(optional)} or None. Cached."""
    if not address or not isinstance(address, str):
        return None
    address = _normalize_address_for_routing(address)
    cache_key = create_cache_key('_free_geocode', address)
    cached = get_from_cache(cache_key)
    if cached is not None:
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
                              'postcode': d.get('postcode')}
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
                              'postcode': (top.get('address') or {}).get('postcode')}
        except Exception as e:
            print(f"  [geocode] Nominatim error: {e}")

    if result is not None:
        set_to_cache(cache_key, result)
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
    """Like calculate_travel_time, but also returns the route (legs + line names).
    Returns {'duration_minutes','route_legs','route_summary','source'} or None."""
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
        return {
            'duration_minutes': int(journey['duration']),
            'route_legs': legs,
            'route_summary': summary,
            'source': 'TfL Journey Planner',
        }
    est = estimate_travel_time_simple(origin_n, dest_n, mode)
    if est is None:
        return None
    return {
        'duration_minutes': est,
        'route_legs': [],
        'route_summary': 'No detailed route available (estimated; outside TfL coverage).',
        'source': 'estimate',
    }


def calculate_travel_time(origin_address: str, destination_address: str, mode: str = "transit") -> int | None:
    """Travel time in minutes via TfL Journey Planner (London public transport),
    falling back to a straight-line estimate when TfL has no route. Free, no Google."""
    if not origin_address or not destination_address:
        return None

    origin_normalized = _normalize_address_for_routing(origin_address)
    destination_normalized = _normalize_address_for_routing(destination_address)

    cache_key = create_cache_key('calculate_travel_time', origin_normalized, destination_normalized, mode)
    cached_result = get_from_cache(cache_key)
    if cached_result is not None:
        print(f"  -> [Cache HIT] Travel time for: {origin_address} ({mode})")
        return cached_result

    origin_coords = _get_coordinates(origin_normalized)
    dest_coords = _get_coordinates(destination_normalized)
    if not origin_coords or not dest_coords:
        print(f"  [WARN] Could not geocode origin/destination for travel time")
        return None

    minutes = _tfl_travel_time(origin_coords, dest_coords, mode)
    if minutes is not None:
        print(f"  [OK] [TfL] {origin_address} -> {destination_normalized}: {minutes} mins ({mode})")
    else:
        minutes = estimate_travel_time_simple(origin_normalized, destination_normalized, mode)
        if minutes is not None:
            print(f"  [OK] [estimate] {origin_address} -> {destination_normalized}: {minutes} mins (TfL had no route)")

    if minutes is not None:
        set_to_cache(cache_key, minutes)
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


def get_crime_data_by_location(address: str) -> dict | None:
    """Get crime data from UK Police API with trend analysis"""
    cache_key = create_cache_key('get_crime_data_by_location', address)
    cached_result = get_from_cache(cache_key)
    if cached_result:
        print(f"  -> [Cache HIT] Crime data for: {address}")
        return cached_result

    print(f"  -> [API Call] Getting official crime data for: {address}")
    location = _get_coordinates(address)
    
    if not location:
        print(f"     ❌ Could not geocode address: {address}")
        return {"error": "Could not geocode address.", "total_crimes_6m": "Unknown"}
    
    print(f"     [OK] Coordinates: {location['lat']}, {location['lng']}")

    base_date = datetime.now().replace(day=1) - pd.DateOffset(months=2)
    dates_to_fetch = [(base_date - pd.DateOffset(months=i)).strftime('%Y-%m') for i in range(6)]
    
    all_crimes = []
    for date_str in dates_to_fetch:
        api_url = f"https://data.police.uk/api/crimes-at-location?date={date_str}&lat={location['lat']}&lng={location['lng']}"
        try:
            print(f"     -> Fetching {date_str}...")
            response = requests.get(api_url, timeout=5)
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
        print(f"     [WARN]  WARNING: No crime data found for any month!")
        summary = {
            "total_crimes_6m": "Unknown", 
            "crime_trend": "unknown", 
            "category_breakdown": "Crime data unavailable",
            "error": "No data returned from UK Police API"
        }
        set_to_cache(cache_key, summary)
        return summary

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
    
    summary = {
        "total_crimes_6m": len(all_crimes),
        "most_recent_month_count": counts[-1] if counts else 0,
        "crime_trend": crime_trend,
        "data_months": sorted_months,
        "category_breakdown": dict(category_counts.most_common(3))
    }
    set_to_cache(cache_key, summary)
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


def estimate_travel_time_simple(origin_address: str, destination_address: str, mode: str = "transit") -> int | None:
    """
    Simple distance-based travel time estimation (no API calls).
    Used for quick filtering in the first stage.
    More accurate results use calculate_travel_time().
    """
    if not origin_address or not destination_address:
        return None
    
    cache_key = create_cache_key('estimate_travel_time_simple', origin_address, destination_address, mode)
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
    
    set_to_cache(cache_key, total_minutes)
    return total_minutes


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
    url = "https://overpass-api.de/api/interpreter"
    
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
            import time as time_module
            time_module.sleep(0.5)
            response = requests.post(url, data={'data': query}, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
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
            import time as time_module
            time_module.sleep(1)
            response = requests.post(url, data={'data': query}, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
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
        List of nearby places with name, distance, address/location
    """
    cache_key = create_cache_key('get_nearby_places_osm', address, amenity_type, radius_m)
    cached_result = get_from_cache(cache_key)
    if cached_result:
        print(f"  -> [Cache HIT] OSM {amenity_type} data for: {address}")
        return cached_result
    
    print(f"  -> [Overpass API] Getting {amenity_type} locations near: {address}")
    
    # Get coordinates for the property
    location = _get_coordinates(address)
    if not location:
        print(f"     ❌ Could not geocode address: {address}")
        return []
    
    lat, lng = location['lat'], location['lng']
    print(f"     [OK] Coordinates: {lat:.4f}, {lng:.4f}")
    
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
    
    overpass_url = "https://overpass-api.de/api/interpreter"
    
    try:
        response = requests.post(overpass_url, data=overpass_query, timeout=10)
        response.raise_for_status()
        data = response.json()
        
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
        
    except requests.exceptions.Timeout:
        print(f"     [WARN] Overpass API timeout - try again later")
        return []
    except requests.exceptions.RequestException as e:
        print(f"     [WARN] Overpass API error: {e}")
        print(f"     [INFO] This may be due to rate limiting. Returning empty results.")
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