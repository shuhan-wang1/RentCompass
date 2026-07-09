"""
Tool 3: Check Safety Tool
检查地区的安全指数
"""

from core.tool_system import Tool
from core.maps_service import get_crime_data_by_location
from core.location_service import resolve_location
from typing import Optional

async def check_safety_impl(
    address: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None
) -> dict:
    """
    检查地址附近的犯罪数据和安全指数
    """
    try:
        
        
        print(f"   🔒 检查安全性:")
        print(f"      地址: {address}")
        
        # 如果没有坐标，尝试解析地址
        if latitude is None or longitude is None:
            location = resolve_location(address)
            if location:
                latitude = location.get('latitude')
                longitude = location.get('longitude')
        
        if latitude is None or longitude is None:
            return {
                'success': False,
                'error': '无法获取位置坐标'
            }
        
        # 获取犯罪数据
        crime_data = get_crime_data_by_location(latitude, longitude)
        
        # 计算安全指数（0-100，越高越安全）
        if crime_data:
            crime_count = crime_data.get('total_crimes', 0)
            # 简化的安全指数计算
            safety_score = max(0, 100 - crime_count // 5)
        else:
            safety_score = 50  # 默认值
        
        safety_level = (
            'Very Safe' if safety_score >= 80
            else 'Safe' if safety_score >= 60
            else 'Moderate' if safety_score >= 40
            else 'Concerning'
        )
        
        return {
            'address': address,
            'latitude': latitude,
            'longitude': longitude,
            'safety_score': safety_score,
            'safety_level': safety_level,
            'crime_data': crime_data if crime_data else {},
            'recommendation': f"该地区安全等级为 {safety_level}，安全指数 {safety_score}/100"
        }
    
    except Exception as e:
        print(f"   ❌ 安全检查出错: {e}")
        raise


# 创建工具实例
check_safety_tool = Tool(
    name="check_safety",
    
    description="""
检查地址附近的安全情况和犯罪数据。

**功能:**
- 获取区域犯罪统计数据
- 计算安全指数（0-100）
- 提供安全等级评估

**何时使用:**
- 用户关心地区安全
- 需要对房源进行安全评估
- 比较不同房源的安全性

**何时不用:**
- 用户没有提到安全问题
- 已经知道该地区的安全情况

**返回内容:**
- safety_score: 安全指数（0-100）
- safety_level: 安全等级（Very Safe/Safe/Moderate/Concerning）
- crime_data: 犯罪统计数据
- recommendation: 建议
""",
    
    func=check_safety_impl,
    
    parameters={
        'type': 'object',
        'properties': {
            'address': {
                'type': 'string',
                'description': '要检查的地址'
            },
            'latitude': {
                'type': 'number',
                'description': '纬度（可选，如果不提供会自动解析地址）'
            },
            'longitude': {
                'type': 'number',
                'description': '经度（可选，如果不提供会自动解析地址）'
            }
        },
        'required': ['address']
    },
    
    max_retries=2
)
