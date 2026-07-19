"""
Tool 2: Calculate Commute Tool
计算两个地址之间的通勤时间
"""

from core.tool_system import Tool
from typing import Optional


def calculate_commute_impl(
    from_address: str,
    to_address: str,
    mode: str = "transit"
) -> dict:
    """
    计算两个地址之间的通勤时间

    NOTE: this is a PLAIN SYNC function on purpose. calculate_travel_details performs
    SYNCHRONOUS network I/O (TfL Journey Planner HTTP + geopy geocoding). Registering it
    as sync means Tool.execute offloads it to an executor thread (tool_system.py :279-284),
    so the asyncio event loop stays responsive and the fc-loop's per-tool asyncio.wait_for /
    batch budget can actually fire. As an async def with these blocking calls inline, four
    concurrent calls serialized to ~52s despite a 20s batch budget (the confirmed bug).
    """
    try:
        from core.maps_service import calculate_travel_details

        print(f"   🚇 计算通勤:")
        print(f"      从: {from_address[:50]}...")
        print(f"      到: {to_address[:50]}...")
        print(f"      方式: {mode}")

        # 调用地图服务计算通勤时间 + 真实路线（TfL 线路明细）
        details = calculate_travel_details(from_address, to_address, mode)

        if not details or details.get('duration_minutes') is None:
            return {
                'success': False,
                'error': '无法计算通勤时间（地址解析失败）'
            }

        duration = details['duration_minutes']
        return {
            'from_address': from_address,
            'to_address': to_address,
            'mode': mode,
            'duration_minutes': duration,
            'is_acceptable': duration <= 45,  # 默认45分钟为可接受
            'duration_category': (
                'Short (< 20 min)' if duration < 20
                else 'Medium (20-45 min)' if duration <= 45
                else 'Long (> 45 min)'
            ),
            'route_summary': details.get('route_summary'),
            'route_legs': details.get('route_legs', []),
            'route_source': details.get('source'),
        }
    
    except Exception as e:
        print(f"   ❌ 通勤计算出错: {e}")
        raise


# 创建工具实例
calculate_commute_tool = Tool(
    name="calculate_commute",
    
    description="""Calculate commute time between two UK addresses (transit / cycling / walking) via the free TfL Journey Planner; returns duration, the route (lines/changes/walking legs) and an acceptability category. Use when the user gives a commute-time requirement or asks "how long to X"; skip if none, or already computed for that listing.
计算两个英国地址间的通勤时间与路线。""",
    
    func=calculate_commute_impl,
    
    parameters={
        'type': 'object',
        'properties': {
            'from_address': {
                'type': 'string',
                'description': '出发地址（房源地址）'
            },
            'to_address': {
                'type': 'string',
                'description': '目的地址（工作地点、学校等）'
            },
            'mode': {
                'type': 'string',
                'enum': ['transit', 'driving', 'walking', 'bicycling'],
                'description': '通勤方式: transit (地铁/公交/火车), driving (开车), walking (步行), bicycling (骑车). 注意：不要使用 tube/tubing/underground，请用 transit',
                'default': 'transit'
            }
        },
        'required': ['from_address', 'to_address']
    },
    
    max_retries=2
)
