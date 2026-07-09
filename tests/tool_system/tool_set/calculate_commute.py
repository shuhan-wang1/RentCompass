# tools/calculate_commute.py

"""
通勤时间计算工具
"""

from tool_system.base import Tool


async def calculate_commute_impl(
    from_address: str,
    to_address: str,
    mode: str = "transit"
) -> dict:
    """
    计算通勤时间
    """
    from free_maps_service import calculate_travel_time
    
    print(f"   🚇 计算通勤:")
    print(f"      从: {from_address[:50]}...")
    print(f"      到: {to_address[:50]}...")
    print(f"      方式: {mode}")
    
    duration = calculate_travel_time(from_address, to_address, mode)
    
    if duration is None:
        return {
            'success': False,
            'error': '无法计算通勤时间（地址解析失败）'
        }
    
    return {
        'from_address': from_address,
        'to_address': to_address,
        'mode': mode,
        'duration_minutes': duration,
        'is_acceptable': duration <= 45
    }


calculate_commute_tool = Tool(
    name="calculate_commute",
    
    description="""
计算两个英国地址之间的通勤时间。

**功能:**
- 支持公共交通、骑行、步行三种方式
- 使用免费 API（基于距离估算）
- 返回分钟数

**何时使用:**
- 用户提到通勤时间要求
- 需要过滤房源（按通勤时间）
- 用户问"到XX要多久"

**何时不用:**
- 用户没提通勤（可跳过）
- 已经计算过该房源（避免重复）

**支持的出行方式:**
- transit: 公共交通（地铁、公交）
- cycling: 骑自行车
- walking: 步行
""",
    
    func=calculate_commute_impl,
    
    parameters={
        "type": "object",
        "properties": {
            "from_address": {
                "type": "string",
                "description": "起点地址（通常是房源地址）"
            },
            "to_address": {
                "type": "string",
                "description": "终点地址（通常是用户的工作地点或学校）"
            },
            "mode": {
                "type": "string",
                "enum": ["transit", "cycling", "walking"],
                "description": "出行方式。transit=公共交通，cycling=骑行，walking=步行",
                "default": "transit"
            }
        },
        "required": ["from_address", "to_address"]
    },
    
    max_retries=1,
    retry_on_error=False
)