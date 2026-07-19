"""
Tool: Calculate Commute Cost
综合计算通勤成本的工具 - 整合通勤时间 + 交通费用 + 月度总成本

功能:
1. 计算从房源到目的地的通勤时间 (使用 Google Maps API)
2. 检查路线是否包含公共交通 (使用 Google Maps Directions API)
3. 根据起点和终点所在的 Zone 计算交通费用 (基于 travel_cost_data.csv)
4. 返回综合信息: 通勤时间、月度交通成本、建议购买哪种票

数据来源:
- 通勤时间: Google Maps API (实时数据)
- 路线详情: Google Maps Directions API (检查是否使用公共交通)
- 交通费用: travel_cost_data.csv (TfL 官方票价)
- Zone 判断: Google Maps Geocoding API
"""

from core.tool_system import Tool
from typing import Optional
import re
import csv
import os
import logging

logger = logging.getLogger(__name__)

# Load travel cost data from CSV
def _load_travel_cost_data():
    """Load TfL fare data from travel_cost_data.csv"""
    csv_path = os.path.join(os.path.dirname(__file__), 'travel_cost_data.csv')
    cost_data = {}

    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                zone_key = row['Zone'].lower().replace(' ', '')  # e.g., "zone1-2" or "zones1-2"
                # Normalize to consistent format
                zone_key = zone_key.replace('zones', 'zone')  # "zones1-2" -> "zone1-2"

                cost_data[zone_key] = {
                    'daily_peak': float(row['Pay as you go caps: Daily peak'].replace('£', '')),
                    'daily_off_peak': float(row['Pay as you go caps: Daily off-peak'].replace('£', '')),
                    'day_anytime': float(row['Travelcards: Day anytime'].replace('£', '')),
                    'day_off_peak': float(row['Travelcards: Day off-peak'].replace('£', ''))
                }
        logger.info("Loaded %s fare zones from CSV", len(cost_data))
        return cost_data
    except Exception as e:
        logger.exception("Failed to load travel_cost_data.csv: %s", e)
        return {}

# Cache the loaded data
_TRAVEL_COST_DATA = _load_travel_cost_data()


def _check_route_uses_transit(route_data: dict, requested_mode: str) -> bool:
    """
    Check if Google Maps route uses any public transport

    Args:
        route_data: Route data from Google Maps Directions API
        requested_mode: The mode requested by user ("transit", "walking", etc.)

    Returns:
        bool: True if route includes transit (bus, subway, train), False otherwise

    Logic:
    - If user requested "transit" mode, we assume they NEED public transport
    - Check if any step actually uses TRANSIT mode
    - If user requested transit but no TRANSIT steps found, it means:
      * Distance is very short (Google suggests walking)
      * OR route is not feasible by transit
    """
    if not route_data or 'legs' not in route_data:
        return False

    has_transit_step = False

    for leg in route_data.get('legs', []):
        for step in leg.get('steps', []):
            travel_mode = step.get('travel_mode', '').upper()
            # Check if this step uses public transit
            if travel_mode == 'TRANSIT':
                has_transit_step = True
                break
            # Also check transit_details if available
            if 'transit_details' in step:
                has_transit_step = True
                break

        if has_transit_step:
            break

    # If user requested transit mode, we should charge them even if Google suggests walking
    # because they likely need to use transit during busy times or bad weather
    if requested_mode == "transit":
        # If there ARE transit steps, definitely return True
        if has_transit_step:
            return True
        # If user requested transit but no transit steps found,
        # it means the distance is very short - check if it's under 20 minutes
        # If under 20 min walking, assume they can walk; otherwise assume they need transit
        duration_seconds = route_data.get('legs', [{}])[0].get('duration', {}).get('value', 0)
        duration_minutes = duration_seconds / 60

        # If walking time > 15 minutes, assume they will use transit during peak/bad weather
        if duration_minutes > 15:
            print(f"   ⚠️ User requested transit but Google suggests {duration_minutes:.0f} min walk - assuming transit needed for practical purposes")
            return True
        else:
            print(f"   ℹ️ Very short distance ({duration_minutes:.0f} min walk) - transit not needed")
            return False

    # For other modes (walking, bicycling, driving), only return True if there are actual transit steps
    return has_transit_step

def _get_zone_from_address(address: str) -> Optional[int]:
    """
    从地址判断所在的 Transport Zone
    使用 Google Maps Geocoding API 获取地址的详细信息

    优先级:
    1. 如果地址中直接提到 postcode，使用 postcode 判断
    2. 否则使用 Google Maps API 获取 postcode，然后判断

    Returns:
        int: Zone 编号 (1-6)，如果无法判断则返回 None
    """
    try:
        # 先尝试从地址中提取 postcode (英国邮编格式: XX## #XX)
        postcode_pattern = r'([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})'
        match = re.search(postcode_pattern, address.upper())

        if match:
            postcode = match.group(1)
            print(f"   📮 从地址提取到 Postcode: {postcode}")
            zone = _get_zone_from_postcode(postcode)
            if zone:
                return zone

        # 如果无法从地址提取，使用免费地理编码（Postcodes.io/Nominatim）获取 postcode
        from core.maps_service import geocode_address

        geocode_result = geocode_address(address)
        if not geocode_result:
            print(f"   ❌ 无法 geocode 地址: {address}")
            return None

        postcode = geocode_result.get('postcode')
        if postcode:
            print(f"   📮 从地理编码获取 Postcode: {postcode}")
            return _get_zone_from_postcode(postcode)

        print(f"   ⚠️ 无法从 geocode 结果提取 postcode")
        return None

    except Exception as e:
        print(f"   ❌ Zone 判断出错: {e}")
        return None


def _get_zone_from_postcode(postcode: str) -> Optional[int]:
    """
    根据 postcode 判断所在的 Zone

    简化版规则 (基于伦敦主要邮编区域):
    - Zone 1: WC, EC, SW1, W1, SE1
    - Zone 2: N1-N7, E1-E3, SE5, SE8, SE10, SW3-SW10, W2-W6, NW1-NW3
    - Zone 3: N8-N12, E4-E8, SE11-SE16, SW11-SW15, W7-W9, NW4-NW6
    - Zone 4: N13-N16, E9-E12, SE17-SE22, SW16-SW19, W10-W12, NW7-NW9
    - Zone 5: N17-N20, E13-E15, SE23-SE26, SW20, W13-W14, NW10-NW11
    - Zone 6: N21-N22, E16-E18, SE27-SE28, UB (Uxbridge), HA (Harrow), EN (Enfield)

    注意: 这是简化版，实际 Zone 划分非常复杂
    """
    # 只取 outward district（空格前部分），避免把内码数字并进区号
    # 例: "N7 0EG" -> "N7"；无空格时去掉末尾 3 位内码: "N70EG" -> "N7"
    pc = postcode.upper().strip()
    postcode = pc.split(' ')[0] if ' ' in pc else (pc[:-3] if len(pc) > 3 else pc)

    # Zone 1 - 伦敦市中心
    if any(postcode.startswith(prefix) for prefix in ['WC', 'EC', 'SW1', 'W1', 'SE1']):
        return 1

    # 提取邮编的区域代码和数字
    match = re.match(r'([A-Z]+)(\d+)', postcode)
    if not match:
        return None

    area_code = match.group(1)  # 如: N, E, SE, SW, W, NW
    area_num = int(match.group(2))  # 如: 1, 7, 12

    # Zone 2
    zone2_ranges = {
        'N': (1, 7),
        'E': (1, 3),
        'SE': [5, 8, 10],
        'SW': (3, 10),
        'W': (2, 6),
        'NW': (1, 3)
    }

    # Zone 3
    zone3_ranges = {
        'N': (8, 12),
        'E': (4, 8),
        'SE': (11, 16),
        'SW': (11, 15),
        'W': (7, 9),
        'NW': (4, 6)
    }

    # Zone 4
    zone4_ranges = {
        'N': (13, 16),
        'E': (9, 12),
        'SE': (17, 22),
        'SW': (16, 19),
        'W': (10, 12),
        'NW': (7, 9)
    }

    # Zone 5
    zone5_ranges = {
        'N': (17, 20),
        'E': (13, 15),
        'SE': (23, 26),
        'SW': [20],
        'W': (13, 14),
        'NW': (10, 11)
    }

    # Zone 6
    zone6_ranges = {
        'N': (21, 22),
        'E': (16, 18),
        'SE': (27, 28),
    }

    # 特殊邮编区域 (通常在 Zone 5-6)
    if area_code in ['UB', 'HA', 'EN', 'TW', 'KT', 'CR', 'BR', 'DA', 'RM', 'IG']:
        return 6

    # 检查每个 Zone
    for zone_num, ranges in [(2, zone2_ranges), (3, zone3_ranges), (4, zone4_ranges),
                             (5, zone5_ranges), (6, zone6_ranges)]:
        if area_code in ranges:
            zone_range = ranges[area_code]
            if isinstance(zone_range, list):
                if area_num in zone_range:
                    return zone_num
            elif isinstance(zone_range, tuple):
                if zone_range[0] <= area_num <= zone_range[1]:
                    return zone_num

    # 默认: 如果无法判断，假设是外围 Zone 6
    return 6


async def calculate_commute_cost_impl(
    from_address: str,
    to_address: str,
    travel_type: str = "student",
    mode: str = "transit"
) -> dict:
    """
    计算综合通勤成本（时间 + 费用）

    Args:
        from_address: 起点地址 (房源地址)
        to_address: 终点地址 (学校/工作地点)
        travel_type: 乘客类型 ('student' 或 'adult')
        mode: 通勤方式 ('transit', 'driving', 'walking', 'bicycling')

    Returns:
        dict: 包含通勤时间、Zone 信息、月度交通费用等
    """
    try:
        print(f"\n   🚇 计算综合通勤成本:")
        print(f"      从: {from_address[:60]}...")
        print(f"      到: {to_address[:60]}...")
        print(f"      类型: {travel_type}, 方式: {mode}")

        # Step 1: 通勤时间 (TfL Journey Planner，免费；非伦敦自动回退直线估算)
        from core.maps_service import calculate_travel_time

        duration_minutes = calculate_travel_time(from_address, to_address, mode)
        if duration_minutes is None:
            return {
                'success': False,
                'error': '无法计算路线（地址解析失败或路线不可达）'
            }
        print(f"   ✅ Route found: {duration_minutes} mins")

        # Step 2: 是否需要公共交通票价
        # TfL/估算没有逐步路线信息，用启发式：transit 模式且时长 > 15 分钟视为需要公共交通
        uses_transit = (mode == "transit" and duration_minutes is not None and duration_minutes > 15)
        print(f"   🚌 Route uses public transport (heuristic): {uses_transit}")

        transport_cost_info = None

        # Only calculate transport cost if route actually uses public transit
        if uses_transit:
            # 获取起点和终点的 Zone
            from_zone = _get_zone_from_address(from_address)
            to_zone = _get_zone_from_address(to_address)

            if from_zone and to_zone:
                # 确定 Zone 范围
                min_zone = min(from_zone, to_zone)
                max_zone = max(from_zone, to_zone)

                # 构建 zone key (e.g., "zone1-2", "zone2-4", "zone1-1" for same zone)
                if min_zone == max_zone:
                    # Same zone travel
                    zone_key = f"zone{min_zone}only" if min_zone == 1 else f"zone{min_zone}only"
                    # Normalize to match CSV format: "Zone 1 only" -> "zone1only"
                    zone_key = zone_key.replace('only', '')
                    # Try different formats
                    possible_keys = [
                        f"zone{min_zone}only",
                        f"zone{min_zone}",
                    ]
                    prices = None
                    for key in possible_keys:
                        prices = _TRAVEL_COST_DATA.get(key)
                        if prices:
                            zone_key = key
                            break
                elif min_zone == 1:
                    zone_key = f"zone1-{max_zone}"
                else:
                    zone_key = f"zone{min_zone}-{max_zone}"

                print(f"   📍 Looking up fares for: {zone_key} (Zone {min_zone} to Zone {max_zone})")

                # 从 CSV 数据获取价格 (if not already fetched above for same-zone)
                if 'prices' not in locals() or prices is None:
                    prices = _TRAVEL_COST_DATA.get(zone_key)

                if prices:
                    # 计算月度成本: 假设每月工作 22 天，使用 daily peak cap
                    monthly_cost = prices['daily_peak'] * 22

                    # 学生使用 18+ Student Oyster 可享受 30% 折扣 (仅限 Travelcard)
                    # 但 Pay As You Go 没有折扣
                    is_student = "student" in travel_type.lower()

                    transport_cost_info = {
                        "from_zone": from_zone,
                        "to_zone": to_zone,
                        "recommended_pass": zone_key.replace('zone', 'Zone '),
                        "user_type": "18+ Student Oyster (Pay As You Go)" if is_student else "Adult Oyster",
                        "monthly_cost": monthly_cost,
                        "daily_cap": prices['daily_peak'],
                        "daily_off_peak_cap": prices['daily_off_peak'],
                        "currency": "GBP",
                        "note": "Cost calculated using Pay As You Go daily cap (£{:.2f}) × 22 working days. Student Oyster cards do NOT get discounts on Pay As You Go - discount only applies to Travelcards.".format(prices['daily_peak'])
                    }
                else:
                    transport_cost_info = {
                        "error": f"No fare data for {zone_key}. Please check tfl.gov.uk for accurate pricing."
                    }
            else:
                transport_cost_info = {
                    "error": "Unable to determine zones for one or both addresses. Cannot calculate transport cost.",
                    "from_zone": from_zone,
                    "to_zone": to_zone
                }

        # Step 3: 组装完整结果
        result = {
            'success': True,
            'from_address': from_address,
            'to_address': to_address,
            'mode': mode,
            'uses_public_transport': uses_transit,
            'commute': {
                'duration_minutes': duration_minutes,
                'duration_category': (
                    'Short (< 20 min)' if duration_minutes < 20
                    else 'Medium (20-45 min)' if duration_minutes <= 45
                    else 'Long (> 45 min)'
                ),
                'is_acceptable': duration_minutes <= 45
            }
        }

        # 添加交通费用信息（如果路线使用公共交通且计算成功）
        if transport_cost_info:
            result['transport_cost'] = transport_cost_info

            # 如果成功计算了月度费用，添加到结果摘要
            if 'monthly_cost' in transport_cost_info:
                result['summary'] = {
                    'commute_time': f"{duration_minutes} minutes",
                    'monthly_transport_cost': f"£{transport_cost_info['monthly_cost']:.2f}",
                    'recommended_pass': transport_cost_info.get('recommended_pass', 'N/A'),
                    'uses_public_transport': 'Yes' if uses_transit else 'No',
                    'total_commuting_cost_per_month': f"£{transport_cost_info['monthly_cost']:.2f} transport + {duration_minutes * 2} min/day × 22 workdays = ~{duration_minutes * 44 / 60:.1f} hours/month"
                }
        elif not uses_transit:
            # 如果不使用公共交通，明确说明无交通成本
            result['summary'] = {
                'commute_time': f"{duration_minutes} minutes",
                'monthly_transport_cost': '£0.00',
                'uses_public_transport': 'No',
                'note': 'Route does not require public transport (walking/cycling only)'
            }

        return result

    except Exception as e:
        print(f"   ❌ 通勤成本计算出错: {e}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }


# 创建工具实例
calculate_commute_cost_tool = Tool(
    name="calculate_commute_cost",

    description="""Calculate the combined commute cost (time + monthly fare) from a listing to a destination via Google Maps; only charges a fare when the route uses public transport (walking/cycling or same-zone = £0). Fares come from TfL 2025 official prices (daily cap x 22 days). Use when the user asks a property's commute cost or monthly transport spend.
计算房源到目的地的通勤时间与月度交通费用（票价来源 TfL）。""",

    func=calculate_commute_cost_impl,

    parameters={
        'type': 'object',
        'properties': {
            'from_address': {
                'type': 'string',
                'description': '起点地址（房源完整地址，包括门牌号、街道名、城市、邮编）'
            },
            'to_address': {
                'type': 'string',
                'description': '终点地址（学校/工作地点完整地址）'
            },
            'travel_type': {
                'type': 'string',
                'enum': ['student', 'adult'],
                'description': '乘客类型。学生使用 18+ Student Oyster Card 可享受 Travelcard 30% 折扣',
                'default': 'student'
            },
            'mode': {
                'type': 'string',
                'enum': ['transit', 'driving', 'walking', 'bicycling'],
                'description': '通勤方式。注意：只有 transit (公共交通) 才会计算交通费用',
                'default': 'transit'
            }
        },
        'required': ['from_address', 'to_address']
    },

    max_retries=2
)
