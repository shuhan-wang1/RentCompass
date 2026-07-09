"""
Tool 2: Calculate Commute Tool
计算两个地址之间的通勤时间
"""

from core.tool_system import Tool
from typing import Optional


async def calculate_commute_impl(
    from_address: str,
    to_address: str,
    mode: str = "transit"
) -> dict:
    """
    计算两个地址之间的通勤时间
    """
    try:
        from core.maps_service import calculate_travel_time
        
        print(f"   🚇 计算通勤:")
        print(f"      从: {from_address[:50]}...")
        print(f"      到: {to_address[:50]}...")
        print(f"      方式: {mode}")
        
        # 调用地图服务计算通勤时间
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
            'is_acceptable': duration <= 45,  # 默认45分钟为可接受
            'duration_category': (
                'Short (< 20 min)' if duration < 20
                else 'Medium (20-45 min)' if duration <= 45
                else 'Long (> 45 min)'
            )
        }
    
    except Exception as e:
        print(f"   ❌ 通勤计算出错: {e}")
        raise


# 创建工具实例
calculate_commute_tool = Tool(
    name="calculate_commute",
    
    description="""
计算两个英国地址之间的通勤时间。

**功能:**
- 支持公共交通、骑行、步行三种方式
- 使用 Google Maps API（需要有 API 密钥）
- 返回分钟数和通勤分类

**何时使用:**
- 用户提到通勤时间要求
- 需要过滤房源（按通勤时间）
- 用户问"到XX要多久"

**何时不用:**
- 用户没提通勤要求（可跳过）
- 已经计算过该房源（避免重复）

**返回内容:**
- duration_minutes: 通勤时间（分钟）
- is_acceptable: 是否在可接受范围内
- duration_category: 通勤时间分类
""",
    
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
                'description': '通勤方式',
                'default': 'transit'
            }
        },
        'required': ['from_address', 'to_address']
    },
    
    max_retries=2
)
