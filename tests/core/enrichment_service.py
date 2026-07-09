# enrichment.py - FIXED VERSION

import asyncio
from .maps_service import (
    calculate_travel_time,
    find_nearby_places, 
    get_crime_data_by_location,
    get_environmental_data
)
from .web_search import search_cost_of_living

async def enrich_property_data(property_dict: dict, criteria: dict) -> dict:
    """
    ✅ FIXED: 智能的条件化数据enrichment
    只在用户关心时才获取相关数据，基于 soft_preferences
    
    动态规则：
    - crime data: 仅当提到 "safe", "crime", "security" 时
    - amenities: 仅当提到具体设施或从 amenities_of_interest 提取
    - environmental: 仅当提到 "pollution", "air quality", "environment" 时
    - cost_of_living: 仅当提到 "cost", "living", "expensive" 时
    """
    enriched_prop = property_dict.copy()
    address = enriched_prop.get('Address')
    postcode = enriched_prop.get('postcode')
    destination = criteria.get('destination')
    
    # ✅ 从 soft_preferences 中智能提取用户关心的内容
    soft_prefs = criteria.get('soft_preferences', '').lower()
    amenities_of_interest = criteria.get('amenities_of_interest', [])

    if not address:
        return enriched_prop
        
    loop = asyncio.get_running_loop()
    
    # ✅ 动态决定需要调用的工具
    tasks = {}
    
    # 1. Crime data - 仅当用户关心安全时
    if any(keyword in soft_prefs for keyword in ['safe', 'crime', 'security', 'dangerous']):
        print(f"  -> [Enrichment] User cares about safety, fetching crime data for {address[:40]}")
        tasks['crime_data_summary'] = loop.run_in_executor(None, get_crime_data_by_location, address)
    else:
        print(f"  -> [Enrichment] Skipping crime data (not in soft_preferences)")
    
    # 2. Amenities - 仅当用户提到具体设施时
    amenities_to_find = []
    amenity_keywords = {
        'supermarket': ['supermarket', 'shop', 'store', 'grocery'],
        'park': ['park', 'green', 'garden', 'outdoor'],
        'gym': ['gym', 'fitness', 'sport', 'exercise'],
        'restaurant': ['restaurant', 'cafe', 'dining', 'food'],
        'school': ['school', 'education'],
        'hospital': ['hospital', 'medical', 'health']
    }
    
    for amenity, keywords in amenity_keywords.items():
        if any(kw in soft_prefs for kw in keywords) or amenity in amenities_of_interest:
            amenities_to_find.append(amenity)
    
    if amenities_to_find:
        print(f"  -> [Enrichment] User cares about amenities: {amenities_to_find}")
        tasks['amenities_nearby'] = loop.run_in_executor(None, find_nearby_places, address, amenities_to_find)
    else:
        print(f"  -> [Enrichment] Skipping amenities (not in soft_preferences)")
    
    # 3. Environmental data - 仅当用户关心空气质量时
    if any(keyword in soft_prefs for keyword in ['pollution', 'air', 'environment', 'green', 'clean']):
        print(f"  -> [Enrichment] User cares about environment, fetching environmental data")
        tasks['environmental_data'] = loop.run_in_executor(None, get_environmental_data, address)
    else:
        print(f"  -> [Enrichment] Skipping environmental data (not in soft_preferences)")
    
    # 4. Cost of living - 仅当用户关心生活成本时
    if any(keyword in soft_prefs for keyword in ['cost', 'living', 'expensive', 'cheap', 'affordable']):
        print(f"  -> [Enrichment] User cares about cost of living, fetching data")
        tasks['cost_of_living'] = loop.run_in_executor(None, search_cost_of_living, postcode or address)
    else:
        print(f"  -> [Enrichment] Skipping cost of living (not in soft_preferences)")
    
    # ✅ 如果没有任何工具需要调用，清除不需要的数据字段
    if not tasks:
        print(f"  -> [Enrichment] No additional data needed for {address[:40]}")
        # ✅ FIXED: 清除所有不需要的enrichment字段，避免旧数据被显示
        fields_to_remove = ['crime_data_summary', 'amenities_nearby', 'environmental_data', 'cost_of_living']
        for field in fields_to_remove:
            if field in enriched_prop:
                print(f"    -> [Enrichment] Removing '{field}' (user didn't ask about it)")
                del enriched_prop[field]
        return enriched_prop
    
    print(f"  -> [Enrichment] Fetching {len(tasks)} data sources for {address[:40]}")
    
    # 并发获取所有需要的数据
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    results_map = dict(zip(tasks.keys(), results))
    
    for key, value in results_map.items():
        if isinstance(value, Exception):
            print(f"    ⚠️  Failed to get '{key}': {value}")
            enriched_prop[key] = {"error": str(value)}
        else:
            # CRITICAL FIX: Keep crime data nested, don't flatten
            if key == 'crime_data_summary':
                enriched_prop['crime_data_summary'] = value  # ✅ Keep nested!
            elif isinstance(value, dict) and key not in ['amenities_nearby', 'environmental_data']:
                # For other dict results, we can flatten
                enriched_prop.update(value)
            else:
                enriched_prop[key] = value
            
    return enriched_prop